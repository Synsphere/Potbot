"""The WSL database path is ready before launch and outlives guest cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from potbot import windows_core_runtime as runtime
from potbot import wsl_postgres
from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError
from potbot.syn_release import custody_firewall, host, windows_custody


@pytest.fixture
def relay_setup(monkeypatch, tmp_path):
    events = []
    manager = SimpleNamespace()

    class Relay:
        port = 43210

        def __init__(self, address, guest, *, target):
            assert (address, guest, target) == (
                "172.27.32.1", "172.27.44.68", ("127.0.0.1", 55436),
            )

        def __enter__(self):
            events.append("relay-open")
            return self

        def __exit__(self, *args):
            events.append("relay-close")

    def firewall(actual, **options):
        assert actual is manager
        assert options["purpose"] == "database"
        assert options["authority_pid"] == os.getpid()
        assert options["controller_created"] == 12345
        assert options["port"] == 43210
        options["stage"]("database-firewall")
        assert options["tcp_probe"]()
        events.append("firewall-checked")

    monkeypatch.setattr(runtime, "project_root", lambda: tmp_path)
    monkeypatch.setattr(wsl_postgres, "managed_loopback_target", lambda *a, **k: ("127.0.0.1", 55436))
    monkeypatch.setattr(wsl_postgres, "PostgresRelay", Relay)
    monkeypatch.setattr(windows_custody, "_wsl_network_identity", lambda _: ("172.27.32.1", "172.27.44.68"))
    monkeypatch.setattr(windows_custody, "_process_creation_filetime", lambda: 12345)
    monkeypatch.setattr(windows_custody, "_tcp_via_wsl", lambda *a: True)
    monkeypatch.setattr(custody_firewall, "ensure_custody_firewall", firewall)
    return manager, {"POTBOT_PARENT_KEYSTORE_HOST": "172.27.32.1"}, events


def test_database_relay_preserves_login_and_tls_identity(relay_setup, capsys):
    from psycopg.conninfo import conninfo_to_dict

    manager, custody, events = relay_setup
    dsn = "postgresql://potbot_workspace:never-print-me@localhost:55436/potbot?sslmode=verify-full"
    with runtime._windows_database_connection(manager, custody, dsn) as guest_dsn:
        values = conninfo_to_dict(guest_dsn)
        assert values == {
            **conninfo_to_dict(dsn), "hostaddr": "172.27.32.1", "port": "43210",
        }
        assert events == ["relay-open", "firewall-checked"]
    assert events[-1] == "relay-close"
    assert "never-print-me" not in capsys.readouterr().out


def test_remote_database_uses_exact_original_dsn(monkeypatch, relay_setup):
    manager, custody, events = relay_setup
    monkeypatch.setattr(wsl_postgres, "managed_loopback_target", lambda *a, **k: None)
    dsn = "postgresql://application:private@database.internal/potbot?sslmode=verify-full"
    with runtime._windows_database_connection(manager, custody, dsn) as actual:
        assert actual == dsn
        assert events == []


def test_changed_network_fails_before_opening_listener(relay_setup, capsys):
    manager, custody, events = relay_setup
    custody["POTBOT_PARENT_KEYSTORE_HOST"] = "172.28.32.1"
    with pytest.raises(StateMismatchError, match="network changed"):
        with runtime._windows_database_connection(manager, custody, "not-logged"):
            pytest.fail("must not launch with a mismatched network")
    assert events == []
    assert "POTBOT_CORE_STAGE=database-failed" in capsys.readouterr().out


def test_uac_decline_closes_relay_and_keeps_specific_terminal_stage(monkeypatch, relay_setup, capsys):
    manager, custody, events = relay_setup

    def decline(*args, **options):
        options["stage"]("database-firewall-declined")
        raise CapabilityUnavailableError("Database firewall administrator approval was declined")

    monkeypatch.setattr(custody_firewall, "ensure_custody_firewall", decline)
    with pytest.raises(CapabilityUnavailableError, match="declined"):
        with runtime._windows_database_connection(manager, custody, "not-logged"):
            pytest.fail("must not launch after declined UAC")
    assert events == ["relay-open", "relay-close"]
    assert capsys.readouterr().out.splitlines()[-1] == "POTBOT_CORE_STAGE=database-firewall-declined"


def test_guest_error_closes_relay_without_mislabeling_it_as_database_failure(relay_setup, capsys):
    manager, custody, events = relay_setup
    with pytest.raises(RuntimeError, match="operator failed"):
        with runtime._windows_database_connection(
            manager, custody, "postgresql://app:private@127.0.0.1:55436/potbot",
        ):
            raise RuntimeError("operator failed")
    assert events[-1] == "relay-close"
    assert "database-failed" not in capsys.readouterr().out


@pytest.mark.parametrize("success", [True, False])
def test_guest_probe_uses_only_environment_for_credentials(monkeypatch, success, capsys):
    secret = "postgresql://app:do-not-print@localhost:43210/potbot"
    environment = {"POTBOT_DSN": secret}
    manager = SimpleNamespace(command=lambda python, args: [python, *args])

    def invoke(command, **options):
        assert secret not in " ".join(command)
        assert options["env"] is environment
        assert options["stdin"] == subprocess.DEVNULL
        assert options["timeout"] == 20
        return SimpleNamespace(
            returncode=0 if success else 1,
            stdout=json.dumps({"ready": True} if success else {
                "ready": False, "error_type": "OperationalError", "sqlstate": "28P01",
            }),
            stderr=secret,
        )

    monkeypatch.setattr(runtime.subprocess, "run", invoke)
    if success:
        runtime._probe_wsl_database(manager, "/venv/bin/python", environment)
        assert "database-ready" in capsys.readouterr().out
    else:
        with pytest.raises(CapabilityUnavailableError, match="OperationalError, SQLSTATE 28P01") as error:
            runtime._probe_wsl_database(manager, "/venv/bin/python", environment)
        assert secret not in str(error.value)
        output = capsys.readouterr()
        assert secret not in output.out + output.err
        assert "database-failed" in output.out


def test_actual_probe_hides_driver_error_details():
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", runtime._DATABASE_PROBE],
        env={**os.environ, "POTBOT_DSN": "host=127.0.0.1 port=1 user=fixture password=do-not-print dbname=fixture"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 1
    record = json.loads(result.stdout)
    assert record == {"ready": False, "error_type": "OperationalError", "sqlstate": None}
    assert "do-not-print" not in result.stdout + result.stderr


@pytest.mark.parametrize("launch_fails", [False, True])
def test_supervisor_keeps_database_and_custody_until_guest_cleanup(monkeypatch, launch_fails):
    events = []
    manager = SimpleNamespace(command=lambda python, args: [python, *args])

    @contextmanager
    def custody(_manager):
        assert _manager is manager
        events.append("custody-open")
        try:
            yield {"custody": "retained"}
        finally:
            events.append("custody-close")

    @contextmanager
    def database(_manager, custody_environment, application_dsn):
        assert custody_environment == {"custody": "retained"}
        assert application_dsn == "windows-application-dsn"
        events.append("database-open")
        try:
            yield "guest-application-dsn"
        finally:
            events.append("database-close")

    def wait(timeout=None):
        events.append(("wait", timeout))
        return 0

    def launch(command, **options):
        events.append("launch")
        assert events[-2] == "database-probed"
        assert options["env"] == {"POTBOT_DSN": "guest-application-dsn"}
        assert options["stdin"] == subprocess.PIPE
        if launch_fails:
            raise OSError("cannot launch WSL")
        return SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: events.append("lifetime-close")), wait=wait,
        )

    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt", environ={
        "POTBOT_XPRA_BRIDGE_TOKEN": "x" * 48, "POTBOT_DSN": "windows-application-dsn",
    }))
    monkeypatch.setattr(host, "WslOperatorHost", lambda: manager)
    monkeypatch.setattr(windows_custody, "windows_host_custody", custody)
    monkeypatch.setattr(runtime, "_windows_database_connection", database)
    monkeypatch.setattr(runtime, "_wsl_environment", lambda custody, token, dsn: {"POTBOT_DSN": dsn})
    monkeypatch.setattr(runtime, "_probe_wsl_database", lambda *a: events.append("database-probed"))
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    if launch_fails:
        with pytest.raises(OSError, match="cannot launch"):
            runtime._supervise_windows_core_runtime("/venv/bin/python", "/home/synoperator/state")
        assert events[-2:] == ["database-close", "custody-close"]
    else:
        assert runtime._supervise_windows_core_runtime("/venv/bin/python", "/home/synoperator/state") == 0
        assert events == [
            "custody-open", "database-open", "database-probed", "launch", ("wait", None),
            "lifetime-close", ("wait", 25), "database-close", "custody-close",
        ]
