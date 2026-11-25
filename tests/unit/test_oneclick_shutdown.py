"""Shutdown orchestration uses live owners and refuses an unverified restart."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot import desktop, native_postgres, oneclick, service
from potbot.domain.errors import CapabilityUnavailableError
from potbot.instance import InstanceLease, StartupCancelled, is_running


@pytest.fixture
def shutdown_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(oneclick, "project_root", lambda: tmp_path)
    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(native_postgres, "stop_if_managed", lambda root: None)
    # Do not touch the test machine's service managers or listeners.
    monkeypatch.setattr(service, "manager_action", lambda _: pytest.fail("no configured manager"))
    monkeypatch.setattr(oneclick, "_busy_ports", lambda config=None: [])
    if os.name == "nt":
        from potbot import windows_core_runtime, windows_service_control

        monkeypatch.setattr(windows_core_runtime, "stop_owned_windows_runtime", lambda: False)
        monkeypatch.setattr(windows_service_control, "stop_registered_task", lambda *a, **k: None)
    return tmp_path


def test_stop_is_idempotent_without_configuration(shutdown_root, capsys):
    assert oneclick._stop() == 0
    assert oneclick._stop() == 0
    assert "has stopped" in capsys.readouterr().err


def _owner(root: Path, role: str, *, ignores_stop: bool = False) -> subprocess.Popen[str]:
    code = """
import sys, time
from pathlib import Path
from potbot.instance import InstanceLease
root, role, ignores = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'yes'
with InstanceLease(role, root=root) as lease:
    print('ready', flush=True)
    if ignores:
        time.sleep(30)
    else:
        if not lease.stopped.wait(5):
            raise RuntimeError('no stop request')
        if role == 'service':
            (root / 'service-saw-stop').write_text('yes')
        else:
            end = time.monotonic() + 5
            while not (root / 'service-saw-stop').exists():
                if time.monotonic() > end:
                    raise RuntimeError('stop waited before notifying every owner')
                time.sleep(.01)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(root), role, "yes" if ignores_stop else "no"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not is_running(role, root=root):
        if child.poll() is not None or time.monotonic() > deadline:
            child.kill()
            raise AssertionError(child.communicate(timeout=5))
        time.sleep(0.01)
    return child


def test_stop_notifies_all_generations_before_waiting(shutdown_root):
    children = [_owner(shutdown_root, role) for role in ("startup", "desktop", "service")]
    try:
        assert oneclick._stop(timeout=5) == 0
        for child in children:
            _, error = child.communicate(timeout=5)
            assert child.returncode == 0, error
        assert not any(is_running(role, root=shutdown_root) for role in ("startup", "desktop", "service"))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_stop_fails_if_owner_has_not_exited(shutdown_root, monkeypatch):
    stopped_database = []
    monkeypatch.setattr(native_postgres, "stop_if_managed", stopped_database.append)
    child = _owner(shutdown_root, "service", ignores_stop=True)
    try:
        with pytest.raises(CapabilityUnavailableError, match="service has not exited"):
            oneclick._stop(timeout=0.15)
        assert is_running("service", root=shutdown_root)
        assert stopped_database == []
    finally:
        child.kill()
        child.wait(timeout=5)


def test_unknown_listener_is_never_killed_or_reported_stopped(shutdown_root, monkeypatch):
    monkeypatch.setattr(oneclick, "_busy_ports", lambda config=None: [3100])
    monkeypatch.setattr(oneclick, "_STOP_LISTENER_WAIT_SECONDS", 0)
    with pytest.raises(CapabilityUnavailableError, match="shutdown could not be verified"):
        oneclick._stop()


def test_actual_bound_port_prevents_new_supervisor():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        with pytest.raises(CapabilityUnavailableError, match=str(port)):
            oneclick._require_ports_available(SimpleNamespace(host="127.0.0.1", port=port))
        assert listener.fileno() >= 0


def test_cancelled_start_does_not_install_or_start_service(shutdown_root, monkeypatch):
    monkeypatch.setattr(oneclick, "_install_or_upgrade", lambda _: pytest.fail("cancelled setup"))
    with InstanceLease("startup", root=shutdown_root) as lease:
        lease.stopped.set()
        with pytest.raises(StartupCancelled):
            oneclick._start_owned(shutdown_root, lease, open_browser=False)


def test_desktop_close_cancels_a_worker_that_has_not_acquired_its_lease(shutdown_root, monkeypatch):
    monkeypatch.setattr(oneclick, "_install_or_upgrade", lambda _: pytest.fail("cancelled setup"))
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(StartupCancelled):
        oneclick.start_services(stop_event=stopped)
    assert not is_running("startup", root=shutdown_root)


def test_fallback_that_loses_to_queued_service_waits_for_the_winner(shutdown_root, monkeypatch):
    rejected = SimpleNamespace(poll=lambda: 1)
    monkeypatch.setattr(oneclick.subprocess, "Popen", lambda *a, **k: rejected)
    monkeypatch.setattr(oneclick, "_healthy", lambda _: True)
    monkeypatch.setattr(oneclick, "_gui_accepts_current_password", lambda: True)
    with InstanceLease("service", root=shutdown_root), InstanceLease("startup", root=shutdown_root) as lease:
        assert oneclick._foreground(object(), lease=lease, open_browser=False) == 0
        assert is_running("service", root=shutdown_root)


def test_second_desktop_is_rejected_before_importing_qt(tmp_path, monkeypatch, capsys):
    from potbot import dependencies

    monkeypatch.setattr(dependencies, "project_root", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_run_desktop", lambda _: pytest.fail("second GUI"))
    with InstanceLease("desktop", root=tmp_path):
        assert desktop.main() == 1
    assert "already running" in capsys.readouterr().err
