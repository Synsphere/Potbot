"""Cross-process lifetime tests: real locks, crash recovery, and stop generations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from potbot import instance, service
from potbot.domain.errors import CapabilityUnavailableError

_OWNER = """
import sys, time
from pathlib import Path
from potbot.instance import InstanceLease
root = Path(sys.argv[1])
with InstanceLease('service', root=root) as lease:
    (root / 'ready').write_text(lease.generation)
    lease.stopped.wait()
    time.sleep(0.2)
"""


@pytest.fixture(autouse=True)
def isolate_windows_instances(monkeypatch, tmp_path):
    # Windows intentionally shares a user-wide namespace between checkouts;
    # tests must not touch a developer's actual Potbot service.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


def _owner(root: Path, code: str = _OWNER) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 10
    while not (root / "ready").exists():
        if process.poll() is not None:
            _, error = process.communicate()
            raise AssertionError(error)
        if time.monotonic() >= deadline:
            process.kill()
            process.wait(timeout=5)
            raise AssertionError("Instance subprocess did not become ready")
        time.sleep(0.02)
    return process


def test_exclusive_between_processes_and_stop_waits_for_cleanup(tmp_path):
    process = _owner(tmp_path)
    try:
        assert instance.is_running("service", root=tmp_path)
        assert instance.owner_root("service", root=tmp_path) == tmp_path.resolve()
        with pytest.raises(instance.InstanceBusy):
            with instance.InstanceLease("service", root=tmp_path):
                pytest.fail("Two owners acquired the same lease")
        assert instance.request_stop("service", root=tmp_path)
        assert not instance.wait_stopped("service", root=tmp_path, timeout=0.01)
        assert instance.wait_stopped("service", root=tmp_path, timeout=5)
        assert process.wait(timeout=5) == 0
        assert not instance.request_stop("service", root=tmp_path)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_crash_releases_lock_and_stale_receipt_does_not_stop_new_owner(tmp_path):
    process = _owner(tmp_path)
    old_generation = (tmp_path / "ready").read_text()
    process.kill()
    process.wait(timeout=5)
    assert not instance.is_running("service", root=tmp_path)
    assert instance.owner_root("service", root=tmp_path) is None
    with instance.InstanceLease("service", root=tmp_path) as lease:
        assert lease.generation != old_generation
        stop = instance.runtime_home(tmp_path) / "service.stop"
        stop.write_text(json.dumps({"generation": old_generation}))
        assert not lease.stopped.wait(timeout=0.3)
        assert instance.request_stop("service", root=tmp_path)
        assert lease.stopped.wait(timeout=2)
    assert not instance.is_running("service", root=tmp_path)


def test_retained_lease_is_not_released_before_process_teardown(tmp_path):
    code = """
import sys, time
from pathlib import Path
from potbot.instance import InstanceLease
root = Path(sys.argv[1])
with InstanceLease('service', root=root) as lease:
    lease.retain_until_exit()
(root / 'ready').write_text('context has exited')
time.sleep(120)
"""
    process = _owner(tmp_path, code)
    try:
        assert instance.is_running("service", root=tmp_path)
        assert instance.request_stop("service", root=tmp_path)
        assert not instance.wait_stopped("service", root=tmp_path, timeout=0.1)
    finally:
        process.kill()
        process.wait(timeout=5)
    assert instance.wait_stopped("service", root=tmp_path, timeout=5)


def test_windows_preparation_responds_to_cooperative_stop(monkeypatch, tmp_path):
    from potbot import windows_core_runtime

    preparing = threading.Event()
    release = threading.Event()
    outcome = []

    def prepare(*args):
        preparing.set()
        release.wait(timeout=5)
        return object()

    monkeypatch.setattr(windows_core_runtime, "prepare_windows_core_runtime", prepare)
    with instance.InstanceLease("service", root=tmp_path) as lease:
        def wait_for_preparation():
            try:
                service._prepare_windows(None, "unused-test-token", lease)
            except instance.StartupCancelled:
                outcome.append("cancelled")

        waiter = threading.Thread(target=wait_for_preparation)
        waiter.start()
        try:
            assert preparing.wait(timeout=5)
            instance.request_stop("service", root=tmp_path)
            waiter.join(timeout=2)
            assert not waiter.is_alive()
            assert outcome == ["cancelled"]
        finally:
            release.set()
            waiter.join(timeout=5)


def test_stopping_gate_blocks_new_start_but_can_signal_existing_start(tmp_path):
    with instance.InstanceLease("startup", root=tmp_path) as starter:
        with instance.InstanceLease("stopping", root=tmp_path):
            with pytest.raises(instance.InstanceBusy, match="stopping"):
                with instance.InstanceLease("service", root=tmp_path):
                    pytest.fail("A service started during stop")
            assert instance.request_stop("startup", root=tmp_path)
            assert starter.stopped.wait(timeout=2)
            with pytest.raises(instance.StartupCancelled):
                starter.check_stop()
    with instance.InstanceLease("service", root=tmp_path):
        assert instance.is_running("service", root=tmp_path)


def test_late_python_start_honors_cancelled_launcher_after_stop_gate_is_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "_launcher_stop_requested", lambda: True)
    with instance.InstanceLease("stopping", root=tmp_path):
        pass
    for role in ("desktop", "startup", "service"):
        with pytest.raises(instance.StartupCancelled):
            with instance.InstanceLease(role, root=tmp_path):
                pytest.fail("A delayed launcher restarted Potbot after stop")


def test_launcher_event_name_is_bounded_and_checked_read_only(monkeypatch):
    monkeypatch.setattr(instance, "_WINDOWS", True)
    names = []
    monkeypatch.setattr(instance, "_event_signalled", lambda name: names.append(name) or True)
    name = r"Global\Potbot.Start.Stop.S-1-5-21-123-456-789-1001"
    monkeypatch.setenv("POTBOT_START_STOP_EVENT", name)
    assert instance._launcher_stop_requested()
    assert names == [name]
    monkeypatch.setenv("POTBOT_START_STOP_EVENT", "unrelated-event")
    with pytest.raises(instance.InvalidInputError):
        instance._launcher_stop_requested()
    assert names == [name]


def test_existing_supervisor_rejects_second_before_preparation(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        service, "_supervise_owned", lambda *args: pytest.fail("Duplicate reached preparation"),
    )
    config = service.ServiceConfig("unused", "unused", str(tmp_path / "policy.json"))
    with instance.InstanceLease("service", root=tmp_path):
        with pytest.raises(instance.InstanceBusy):
            service._supervise(config)


def test_manager_stop_uses_live_control_instead_of_forcing_process(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    monkeypatch.setattr(service, "_managed", lambda *args: pytest.fail("Forced manager stop"))
    ready = threading.Event()

    def own():
        with instance.InstanceLease("service", root=tmp_path) as lease:
            ready.set()
            lease.stopped.wait(timeout=5)

    thread = threading.Thread(target=own)
    thread.start()
    assert ready.wait(timeout=5)
    service.manager_action("stop")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_manager_stop_timeout_does_not_fall_back_to_force(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    monkeypatch.setattr(instance, "request_stop", lambda *args, **kwargs: True)
    monkeypatch.setattr(instance, "wait_stopped", lambda *args, **kwargs: False)
    monkeypatch.setattr(service, "_managed", lambda *args: pytest.fail("Forced manager stop"))
    with pytest.raises(CapabilityUnavailableError, match="still stopping"):
        service.manager_action("stop")


def test_windows_scope_is_shared_but_stop_rejects_different_checkout(monkeypatch, tmp_path):
    # Test the shared-directory policy without invoking Windows locking on POSIX.
    shared = tmp_path / "instances"
    monkeypatch.setattr(instance, "runtime_home", lambda root=None: shared)
    other = tmp_path / "other-checkout"
    with instance.InstanceLease("service", root=tmp_path):
        assert instance.owner_root("service", root=other) == tmp_path.resolve()
        with pytest.raises(instance.InstanceBusy, match="active Potbot checkout"):
            instance.request_stop("service", root=other)


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel process-job integration")
def test_windows_job_removes_descendant_when_owner_is_killed(tmp_path):
    marker = tmp_path / "child-pid"
    code = """
import subprocess, sys, time
from pathlib import Path
from potbot.windows_process_job import contain_service_process
contain_service_process()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(120)
"""
    owner = subprocess.Popen([sys.executable, "-c", code, str(marker)])
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "Process job failed to initialize"
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
        assert handle
        try:
            owner.kill()
            owner.wait(timeout=10)
            assert kernel.WaitForSingleObject(handle, 10000) == 0
        finally:
            kernel.CloseHandle(handle)
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=10)
