"""Fixed database routing, authenticated native config, and real TCP lifetimes."""

from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable

import pytest
from psycopg.conninfo import conninfo_to_dict

from potbot import native_postgres
from potbot import wsl_postgres as relay
from potbot.domain.errors import CapabilityUnavailableError

_DSN = "postgresql://potbot_app_test:p%40ss%27word@127.0.0.1:55436/potbot?sslmode=disable"


@pytest.fixture
def managed(tmp_path):
    parent = tmp_path / ".potbot" / "native-postgres"
    data = parent / "data"
    data.mkdir(parents=True)
    (parent / "packages").mkdir()
    major = 16 if sys.platform == "win32" else 18
    (data / "PG_VERSION").write_text(str(major), encoding="ascii")
    (parent / "managed.json").write_text(json.dumps({
        "schema": native_postgres.VERSION,
        "port": native_postgres.PORT,
        "platform": native_postgres._platform(),
        "postgres_major": major,
    }), encoding="ascii")
    (data / "postgresql.conf").write_text("# hba_file = 'ConfigDir/pg_hba.conf'\n", encoding="ascii")
    (data / "postmaster.opts").write_text(
        f'"postgres" "-D" "{data}" "-c" "listen_addresses=127.0.0.1" "-c" "port=55436"',
        encoding="utf-8",
    )
    (data / "pg_hba.conf").write_text(
        "local all all trust\n"
        "host all all 127.0.0.1/32 scram-sha-256\n"
        "host replication all ::1/128 scram-sha-256\n"
        "host all all 127.0.0.1 255.255.255.255 scram-sha-256\n"
        "host all all 0.0.0.0/0 reject\n",
        encoding="ascii",
    )
    return tmp_path, data


def test_only_verified_native_cluster_is_forwarded(managed):
    root, _ = managed
    assert relay.managed_loopback_target(_DSN, root=root) == ("127.0.0.1", 55436)


def test_remote_single_host_needs_no_local_installation(monkeypatch, tmp_path):
    monkeypatch.setattr(native_postgres, "_manifest", lambda *a: pytest.fail("remote DB"))
    dsn = "postgresql://app:secret@database.example:5432/project?sslmode=verify-full"
    assert relay.managed_loopback_target(dsn, root=tmp_path) is None


@pytest.mark.parametrize("dsn", [
    "host=127.0.0.1 port=5432 dbname=potbot user=app",
    "host=127.0.0.1 port=55436 dbname=postgres user=app",
    "host=127.0.0.1 port=55436 dbname=potbot user=potbot_admin",
    "host=127.0.0.1 port=55436 dbname=potbot",
    "host=127.0.0.2 port=55436 dbname=potbot user=app",
    "host=127.0.0.1 hostaddr=192.168.1.3 port=55436 dbname=potbot user=app",
    "host=localhost hostaddr=127.0.0.2 port=55436 dbname=potbot user=app",
    "host=localhost hostaddr=::1 port=55436 dbname=potbot user=app",
    "host=database.example hostaddr=127.0.0.1 port=55436 dbname=potbot user=app",
    "host=127.0.0.1,192.168.1.3 port=55436 dbname=potbot user=app",
    "host=127.0.0.1 port=55436,55437 dbname=potbot user=app",
    "host=/tmp port=55436 dbname=potbot user=app",
    "service=production host=127.0.0.1 port=55436 dbname=potbot user=app",
])
def test_unsupported_targets_are_rejected_without_echoing_credentials(managed, dsn):
    root, _ = managed
    with pytest.raises(CapabilityUnavailableError) as error:
        relay.managed_loopback_target(dsn + " password=private-password", root=root)
    assert "private-password" not in str(error.value)


def test_native_target_without_owned_marker_is_rejected(tmp_path):
    with pytest.raises(CapabilityUnavailableError):
        relay.managed_loopback_target(_DSN, root=tmp_path)


@pytest.mark.parametrize("entry", [
    "host all all 127.0.0.1/32 trust",
    "host all all 127.0.0.1/32 md5",
    "host all all 127.0.0.1/32 password",
    "include 'another.conf'",
    "include_if_exists 'another.conf'",
])
def test_native_hba_requires_scram_and_no_includes(managed, entry):
    root, data = managed
    (data / "pg_hba.conf").write_text(entry, encoding="ascii")
    with pytest.raises(CapabilityUnavailableError, match="authentication configuration"):
        relay.managed_loopback_target(_DSN, root=root)


@pytest.mark.parametrize("name,entry", [
    ("postgresql.conf", "hba_file = '/another/file'"),
    ("postgresql.conf", "include 'another.conf'"),
    ("postgresql.auto.conf", "include_dir 'another-directory'"),
    ("postmaster.opts", '"postgres" "-c" "config_file=another.conf"'),
])
def test_effective_hba_cannot_be_redirected(managed, name, entry):
    root, data = managed
    (data / name).write_text(entry, encoding="ascii")
    with pytest.raises(CapabilityUnavailableError, match="authentication configuration"):
        relay.managed_loopback_target(_DSN, root=root)


@pytest.mark.skipif(os.name != "posix", reason="creating symlinks requires Windows privileges")
def test_symlinked_hba_is_not_accepted(managed):
    root, data = managed
    original = data / "pg_hba.conf"
    replacement = data / "other.conf"
    original.rename(replacement)
    original.symlink_to(replacement)
    with pytest.raises(CapabilityUnavailableError):
        relay.managed_loopback_target(_DSN, root=root)


def test_rewrite_preserves_password_options_and_original_tls_hostname():
    dsn = (
        "host=localhost port=55436 dbname=potbot user=potbot_app_test "
        "password='quote\\\' and space' sslmode=verify-full sslrootcert=/private/ca.pem "
        "options='-c statement_timeout=30000' application_name=potbot"
    )
    expected = conninfo_to_dict(dsn)
    actual = conninfo_to_dict(relay.rewrite_for_relay(dsn, "172.27.32.1", 49152))
    assert actual == {**expected, "hostaddr": "172.27.32.1", "port": "49152"}
    uri = conninfo_to_dict(relay.rewrite_for_relay(_DSN, "172.27.32.1", 49152))
    assert uri["password"] == "p@ss'word"


@pytest.mark.parametrize("host,guest,target", [
    ("0.0.0.0", "172.27.44.68", ("127.0.0.1", 55436)),
    ("127.0.0.1", "172.27.44.68", ("127.0.0.1", 55436)),
    ("169.254.1.1", "172.27.44.68", ("127.0.0.1", 55436)),
    ("192.0.2.1", "172.27.44.68", ("127.0.0.1", 55436)),
    ("240.0.0.1", "172.27.44.68", ("127.0.0.1", 55436)),
    ("172.27.32.1", "172.27.32.1", ("127.0.0.1", 55436)),
    ("172.27.32.1", "172.27.44.68", ("127.0.0.1", 22)),
    ("172.27.32.1", "172.27.44.68", ("192.168.1.3", 55436)),
])
def test_relay_cannot_be_used_as_an_arbitrary_service_proxy(host, guest, target):
    with pytest.raises(CapabilityUnavailableError):
        relay.PostgresRelay(host, guest, target=target)


def _read_all(channel: socket.socket) -> bytes:
    result = bytearray()
    while block := channel.recv(65536):
        result.extend(block)
    return bytes(result)


class _Backend:
    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self.handler = handler
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.accepted = threading.Event()
        self.count = 0
        self.errors: list[BaseException] = []
        self.workers: list[threading.Thread] = []
        self.channels: list[socket.socket] = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.channels.append(client)
            self.count += 1
            self.accepted.set()

            def work(channel=client):
                with channel:
                    try:
                        channel.settimeout(5)
                        self.handler(channel)
                    except BaseException as error:
                        self.errors.append(error)

            worker = threading.Thread(target=work, daemon=True)
            self.workers.append(worker)
            worker.start()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)
        for channel in self.channels:
            relay._close_socket(channel)
        for worker in self.workers:
            worker.join(timeout=2)


@pytest.fixture
def tcp(monkeypatch):
    backends = []
    # Production constructors require the private WSL route and fixed native
    # port. Tests substitute loopback aliases/ephemeral backend ports only.
    monkeypatch.setattr(relay, "_private_ipv4", lambda value: value)

    def create(handler):
        backend = _Backend(handler)
        backends.append(backend)
        monkeypatch.setattr(relay, "_NATIVE_PORT", backend.port)
        return backend, relay.PostgresRelay(
            "127.0.0.1", "127.0.0.2", target=("127.0.0.1", backend.port),
        )

    yield create
    for backend in backends:
        backend.close()


def _connect(forwarder):
    channel = socket.socket()
    channel.settimeout(5)
    channel.bind(("127.0.0.2", 0))
    channel.connect((forwarder.host, forwarder.port))
    return channel


def test_real_tcp_concurrent_large_requests_and_half_closed_responses(tcp):
    def handle(channel):
        payload = _read_all(channel)
        channel.sendall(payload[::-1])

    backend, forwarder = tcp(handle)
    with forwarder:
        def request(number):
            payload = bytes(range(256)) * 2048 + bytes([number])
            with _connect(forwarder) as channel:
                channel.sendall(payload)
                channel.shutdown(socket.SHUT_WR)
                assert _read_all(channel) == payload[::-1]

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(request, range(6)))
    assert backend.count == 6
    assert backend.errors == []


def test_server_half_close_does_not_discard_later_client_data(tcp):
    received = []
    complete = threading.Event()

    def handle(channel):
        channel.sendall(b"server greeting")
        channel.shutdown(socket.SHUT_WR)
        received.append(_read_all(channel))
        complete.set()

    backend, forwarder = tcp(handle)
    with forwarder, _connect(forwarder) as channel:
        assert _read_all(channel) == b"server greeting"
        channel.sendall(b"client after server EOF")
        channel.shutdown(socket.SHUT_WR)
        assert complete.wait(3)
    assert received == [b"client after server EOF"]
    assert backend.errors == []


def test_other_peer_is_closed_before_connecting_to_backend(tcp):
    backend, forwarder = tcp(lambda channel: _read_all(channel))
    with forwarder, socket.create_connection((forwarder.host, forwarder.port), timeout=2) as channel:
        try:
            assert channel.recv(1) == b""
        except ConnectionResetError:
            pass
        assert not backend.accepted.wait(0.2)


def test_context_exit_closes_listener_and_idle_streams(tcp):
    backend, forwarder = tcp(lambda channel: _read_all(channel))
    with forwarder:
        port = forwarder.port
        channel = _connect(forwarder)
        assert backend.accepted.wait(2)
    with channel:
        assert channel.recv(1) == b""
    assert forwarder._acceptor is not None and not forwarder._acceptor.is_alive()
    assert not forwarder._workers
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_idle_initial_client_is_closed_within_bound(tcp, monkeypatch):
    monkeypatch.setattr(relay, "_FIRST_BYTE_TIMEOUT", 0.1)
    backend, forwarder = tcp(lambda channel: _read_all(channel))
    with forwarder, _connect(forwarder) as channel:
        assert backend.accepted.wait(2)
        assert channel.recv(1) == b""


def test_connection_limit_does_not_open_an_extra_backend(tcp, monkeypatch):
    monkeypatch.setattr(relay, "_MAX_CONNECTIONS", 2)
    backend, forwarder = tcp(lambda channel: _read_all(channel))
    with forwarder, _connect(forwarder) as first, _connect(forwarder) as second:
        deadline = time.monotonic() + 2
        while backend.count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.count == 2
        with _connect(forwarder) as rejected:
            assert rejected.recv(1) == b""
        assert backend.count == 2
        first.shutdown(socket.SHUT_WR)
        second.shutdown(socket.SHUT_WR)
