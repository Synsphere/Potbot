"""A bounded, fixed-destination TCP path to Potbot's authenticated native database.

PostgreSQL remains on Windows loopback and authenticates every connection. The
relay accepts only the current owned WSL guest and never interprets or records
SQL, passwords or protocol bytes. It is scoped to the Windows core supervisor.
"""

from __future__ import annotations

import ipaddress
import os
import re
import selectors
import shlex
import socket
import stat
import threading
import time
from pathlib import Path
from types import TracebackType

from potbot.domain.errors import CapabilityUnavailableError
from potbot.native_postgres import PORT as _NATIVE_PORT

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_MAX_CONNECTIONS = 32
_BUFFER_LIMIT = 64 * 1024
_CONNECT_TIMEOUT = 3.0
_FIRST_BYTE_TIMEOUT = 10.0
_IDLE_TIMEOUT = 600.0
_PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_CONFIG_ERROR = "The managed PostgreSQL authentication configuration could not be verified."


def _parameters(dsn: str) -> dict[str, str]:
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    try:
        result = {
            key: str(value) for key, value in conninfo_to_dict(dsn).items() if value is not None
        }
    except (ProgrammingError, TypeError, ValueError):
        raise CapabilityUnavailableError("The application database connection is invalid.") from None
    host = result.get("host", "")
    if (
        not host
        or host.startswith(("/", "@"))
        or "service" in result
        or any("," in result.get(name, "") for name in ("host", "hostaddr", "port"))
    ):
        raise CapabilityUnavailableError(
            "WSL database startup requires one explicit TCP host; service, socket and "
            "multiple-host connection configurations are unsupported."
        )
    address = result.get("hostaddr", "")
    try:
        numeric_host = ipaddress.ip_address(host)
    except ValueError:
        numeric_host = None
    if numeric_host is not None and numeric_host.is_loopback and host not in _LOOPBACK:
        raise CapabilityUnavailableError("The database loopback address is unsupported.")
    if address:
        try:
            numeric = ipaddress.ip_address(address)
        except ValueError:
            raise CapabilityUnavailableError("The database host address is invalid.") from None
        if (host.lower() in _LOOPBACK) != numeric.is_loopback:
            raise CapabilityUnavailableError("The database host and address select different routes.")
        if host.lower() in _LOOPBACK and address != "127.0.0.1":
            raise CapabilityUnavailableError("The database address is not the native loopback endpoint.")
    return result


def _canonical_text(path: Path, *, optional: bool = False) -> str:
    try:
        if optional and not path.exists() and not path.is_symlink():
            return ""
        if path.resolve(strict=True) != path or path.is_symlink():
            raise ValueError
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024 or b"\x00" in raw:
            raise ValueError
        return raw.decode("utf-8-sig")
    except (OSError, ValueError):
        raise CapabilityUnavailableError(_CONFIG_ERROR) from None


def _require_scram_configuration(root: Path) -> None:
    data = root / ".potbot" / "native-postgres" / "data"
    # The owned installation uses PostgreSQL's default HBA path. Refuse custom
    # includes/overrides instead of assuming the file we inspect is effective.
    for name in ("postgresql.conf", "postgresql.auto.conf"):
        text = _canonical_text(data / name, optional=name == "postgresql.auto.conf")
        for line in text.splitlines():
            if re.match(r"\s*(?:include(?:_dir|_if_exists)?|hba_file)\b", line, re.I):
                raise CapabilityUnavailableError(_CONFIG_ERROR)
    try:
        options = shlex.split(_canonical_text(data / "postmaster.opts"))
    except ValueError:
        raise CapabilityUnavailableError(_CONFIG_ERROR) from None
    expected = {"listen_addresses=127.0.0.1", "port=" + str(_NATIVE_PORT)}
    seen: set[str] = set()
    if not options or len(options) % 2 != 1:
        raise CapabilityUnavailableError(_CONFIG_ERROR)
    for flag, value in zip(options[1::2], options[2::2], strict=True):
        if flag == "-D" and Path(value).resolve() == data:
            seen.add("data")
        elif flag == "-c" and value in expected:
            seen.add(value)
        else:
            raise CapabilityUnavailableError(_CONFIG_ERROR)
    if seen != {*expected, "data"}:
        raise CapabilityUnavailableError(_CONFIG_ERROR)
    for line in _canonical_text(data / "pg_hba.conf").splitlines():
        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            raise CapabilityUnavailableError(_CONFIG_ERROR) from None
        if not fields or fields[0] == "local":
            continue
        if fields[0] not in {"host", "hostssl", "hostnossl", "hostgssenc", "hostnogssenc"}:
            raise CapabilityUnavailableError(_CONFIG_ERROR)
        if len(fields) < 5:
            raise CapabilityUnavailableError(_CONFIG_ERROR)
        method = fields[4]
        if method not in {"scram-sha-256", "reject"} and len(fields) >= 6:
            try:
                ipaddress.ip_address(fields[4])  # Separate netmask syntax.
            except ValueError:
                raise CapabilityUnavailableError(_CONFIG_ERROR) from None
            method = fields[5]
        if method not in {"scram-sha-256", "reject"}:
            raise CapabilityUnavailableError(_CONFIG_ERROR)


def managed_loopback_target(dsn: str, *, root: Path) -> tuple[str, int] | None:
    """Return only Potbot's fixed native endpoint; None preserves a remote DSN."""
    from potbot.native_postgres import _manifest, is_managed

    values = _parameters(dsn)
    if values["host"].lower() not in _LOOPBACK:
        return None
    if (
        values.get("port") != str(_NATIVE_PORT)
        or values.get("dbname") != "potbot"
        or not values.get("user")
        or values["user"].lower() in {"postgres", "potbot_admin"}
        or not is_managed(root)
    ):
        raise CapabilityUnavailableError(
            "WSL can forward only the verified Potbot native database with an explicit "
            "application role; configure a reachable TCP host for another database."
        )
    _manifest(root)
    _require_scram_configuration(root)
    return "127.0.0.1", _NATIVE_PORT


def _private_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise CapabilityUnavailableError("The WSL database endpoint is invalid.") from None
    if not any(address in network for network in _PRIVATE_NETWORKS):
        raise CapabilityUnavailableError("The WSL database endpoint must be a private guest route.")
    return str(address)


def rewrite_for_relay(dsn: str, host: str, port: int) -> str:
    """Keep the original host for TLS identity; hostaddr selects the relay route."""
    from psycopg.conninfo import make_conninfo

    values = _parameters(dsn)
    if values["host"].lower() not in _LOOPBACK or values.get("port") != str(_NATIVE_PORT):
        raise CapabilityUnavailableError("Only the native loopback database can use this relay.")
    endpoint = _private_ipv4(host)
    if type(port) is not int or not 1 <= port <= 65535:
        raise CapabilityUnavailableError("The WSL database relay port is invalid.")
    return make_conninfo(dsn, hostaddr=endpoint, port=port)


def _close_socket(channel: socket.socket) -> None:
    try:
        channel.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    channel.close()


class PostgresRelay:
    """Context-managed relay with one exact peer and a fixed native destination."""

    def __init__(self, host: str, guest: str, *, target: tuple[str, int]) -> None:
        self.host, self.guest = _private_ipv4(host), _private_ipv4(guest)
        if self.host == self.guest or target != ("127.0.0.1", _NATIVE_PORT):
            raise CapabilityUnavailableError("The database relay destination is not the native DB.")
        self._target = target
        self.port = 0
        self._listener: socket.socket | None = None
        self._acceptor: threading.Thread | None = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._channels: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()

    def __enter__(self) -> PostgresRelay:
        if self._listener is not None or self._stopped.is_set():
            raise CapabilityUnavailableError("A database relay generation cannot be restarted.")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is None:
                    raise CapabilityUnavailableError("Exclusive database socket binding is unavailable.")
                listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            listener.bind((self.host, 0))
            listener.listen(_MAX_CONNECTIONS)
            listener.settimeout(0.2)
            self.port = listener.getsockname()[1]
            self._listener = listener
            self._acceptor = threading.Thread(target=self._accept, name="potbot-db-accept", daemon=True)
            self._acceptor.start()
        except BaseException:
            listener.close()
            self._listener = None
            raise
        return self

    def _accept(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stopped.is_set():
            try:
                client, peer = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if peer[0] != self.guest:
                _close_socket(client)
                continue
            with self._lock:
                if self._stopped.is_set() or len(self._workers) >= _MAX_CONNECTIONS:
                    _close_socket(client)
                    continue
                self._channels.add(client)
                worker = threading.Thread(
                    target=self._connection, args=(client,), name="potbot-db-stream", daemon=True,
                )
                self._workers.add(worker)
                # Start while holding the gate so close() never joins a thread
                # that has been recorded but has not yet started.
                worker.start()

    def _connection(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection(self._target, timeout=_CONNECT_TIMEOUT)
            with self._lock:
                if self._stopped.is_set():
                    return
                self._channels.add(upstream)
            self._forward(client, upstream)
        except (OSError, ValueError):
            # The PostgreSQL client sees the closed connection. Protocol bytes,
            # connection strings and exception text never enter service logs.
            pass
        finally:
            for channel in (client, upstream):
                if channel is not None:
                    _close_socket(channel)
            with self._lock:
                self._channels.discard(client)
                if upstream is not None:
                    self._channels.discard(upstream)
                self._workers.discard(threading.current_thread())

    def _forward(self, client: socket.socket, upstream: socket.socket) -> None:
        channels = (client, upstream)
        other = {client: upstream, upstream: client}
        pending = {client: bytearray(), upstream: bytearray()}
        readable = {client: True, upstream: True}
        ended = {client: False, upstream: False}
        activity = time.monotonic()
        received = False
        for channel in channels:
            channel.setblocking(False)
        with selectors.DefaultSelector() as selector:
            while not self._stopped.is_set():
                for source in channels:
                    destination = other[source]
                    if not readable[source] and not pending[destination] and not ended[destination]:
                        destination.shutdown(socket.SHUT_WR)
                        ended[destination] = True
                    events = (
                        selectors.EVENT_READ
                        if readable[source] and len(pending[destination]) < _BUFFER_LIMIT else 0
                    ) | (selectors.EVENT_WRITE if pending[source] else 0)
                    try:
                        key = selector.get_key(source)
                    except KeyError:
                        key = None
                    if key is not None and not events:
                        selector.unregister(source)
                    elif key is not None:
                        selector.modify(source, events)
                    elif events:
                        selector.register(source, events)
                if not selector.get_map():
                    return
                allowance = _IDLE_TIMEOUT if received else _FIRST_BYTE_TIMEOUT
                remaining = activity + allowance - time.monotonic()
                if remaining <= 0:
                    return
                for key, events in selector.select(min(0.2, remaining)):
                    ready = key.fileobj
                    assert isinstance(ready, socket.socket)
                    source = ready
                    destination = other[source]
                    if events & selectors.EVENT_READ:
                        try:
                            data = source.recv(_BUFFER_LIMIT - len(pending[destination]))
                        except BlockingIOError:
                            data = None
                        if data:
                            pending[destination].extend(data)
                            received = True
                            activity = time.monotonic()
                        elif data == b"":
                            readable[source] = False
                    if events & selectors.EVENT_WRITE and pending[source]:
                        try:
                            sent = source.send(pending[source])
                        except BlockingIOError:
                            sent = 0
                        if sent:
                            del pending[source][:sent]
                            activity = time.monotonic()

    def close(self) -> None:
        self._stopped.set()
        if self._listener is not None:
            _close_socket(self._listener)
        with self._lock:
            channels = tuple(self._channels)
        for channel in channels:
            _close_socket(channel)
        deadline = time.monotonic() + _CONNECT_TIMEOUT + 2
        if self._acceptor is not None:
            self._acceptor.join(timeout=max(0, deadline - time.monotonic()))
        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if (self._acceptor and self._acceptor.is_alive()) or any(w.is_alive() for w in workers):
            raise CapabilityUnavailableError("The database relay has not completed shutdown.")

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
