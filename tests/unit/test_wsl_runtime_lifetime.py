"""Guest cleanup is tied to its Windows owner, not to a recycled process id."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError
from potbot.syn_release import host
from potbot.windows_core_runtime import _stop_owned_distribution


def _manager(tmp_path: Path, *, receipt: bool = True, registered: bool = True):
    path = tmp_path / "host.json"
    if receipt:
        path.write_text("owned", encoding="ascii")
    registration = object() if registered else None
    calls = []

    def verify(actual, record):
        assert actual is registration
        assert record == {"state": "ready"}
        calls.append("verified")

    manager = SimpleNamespace(
        receipt=path,
        wsl="owned-wsl.exe",
        _read_receipt=lambda: {"state": "ready"},
        _require_registration=verify,
        command=lambda executable, args: [
            "owned-wsl.exe", "--distribution", host.DISTRO, "--user", "synoperator",
            "--exec", executable, *args,
        ],
    )
    return manager, registration, calls


@pytest.mark.parametrize("receipt,registered", [(False, False), (True, False)])
def test_stop_absent_guest_does_not_install_or_launch(monkeypatch, tmp_path, receipt, registered):
    manager, registration, _ = _manager(tmp_path, receipt=receipt, registered=registered)
    monkeypatch.setattr(host, "_registered_distro", lambda: registration)
    monkeypatch.setattr(host, "run_host", lambda *a, **k: pytest.fail("must not launch WSL"))
    assert _stop_owned_distribution(manager) is False


def test_stop_refuses_unowned_named_guest(monkeypatch, tmp_path):
    manager, registration, _ = _manager(tmp_path, receipt=False)
    monkeypatch.setattr(host, "_registered_distro", lambda: registration)
    monkeypatch.setattr(host, "run_host", lambda *a, **k: pytest.fail("must not stop foreign WSL"))
    with pytest.raises(StateMismatchError, match="ownership receipt"):
        _stop_owned_distribution(manager)


def test_stop_refuses_mismatched_registration(monkeypatch, tmp_path):
    manager, registration, _ = _manager(tmp_path)
    monkeypatch.setattr(host, "_registered_distro", lambda: registration)

    def mismatch(*args):
        raise StateMismatchError("registration mismatch")

    manager._require_registration = mismatch
    monkeypatch.setattr(host, "run_host", lambda *a, **k: pytest.fail("must not stop foreign WSL"))
    with pytest.raises(StateMismatchError, match="mismatch"):
        _stop_owned_distribution(manager)


def test_stop_targets_only_owned_distro_and_reads_back_stopped(monkeypatch, tmp_path):
    manager, registration, calls = _manager(tmp_path)
    monkeypatch.setattr(host, "_registered_distro", lambda: registration)
    running = True

    def invoke(command, **kwargs):
        nonlocal running
        assert calls[0] == "verified"
        calls.append(command)
        if command[1:2] == ["--list"]:
            return "Ubuntu\n" + ("\ufeff" + host.DISTRO + "\n" if running else "")
        if command[1:2] == ["--distribution"]:
            assert command[2] == host.DISTRO
            return "quiet\n"
        assert command == [manager.wsl, "--terminate", host.DISTRO]
        running = False
        return ""

    monkeypatch.setattr(host, "run_host", invoke)
    assert _stop_owned_distribution(manager) is True
    assert calls[-1] == [manager.wsl, "--list", "--running", "--quiet"]
    assert _stop_owned_distribution(manager) is False
    assert sum(isinstance(call, list) and "--terminate" in call for call in calls) == 1


def test_stop_reports_incomplete_shutdown(monkeypatch, tmp_path):
    from potbot import windows_core_runtime as runtime

    manager, registration, _ = _manager(tmp_path)
    monkeypatch.setattr(host, "_registered_distro", lambda: registration)
    monkeypatch.setattr(host, "run_host", lambda *a, **k: host.DISTRO + "\n")
    ticks = iter([0.0, 16.0])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(ticks))
    with pytest.raises(CapabilityUnavailableError, match="shutdown is incomplete"):
        _stop_owned_distribution(manager)


@pytest.mark.skipif(os.name != "posix", reason="WSL runtime uses Linux descriptors and flock")
@pytest.mark.parametrize("payload", [b"", b"unexpected payload"])
def test_guest_lifetime_pipe_requests_stop_when_owner_closes(payload):
    from potbot.wsl_core_runtime import _watch_parent_lifetime

    read_fd, write_fd = os.pipe()
    stopped = threading.Event()
    thread = threading.Thread(target=_watch_parent_lifetime, args=(stopped, read_fd))
    thread.start()
    try:
        assert not stopped.wait(0.05)
        if payload:
            os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = -1
        assert stopped.wait(2)
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        stopped.set()
        thread.join(timeout=2)
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)


@pytest.mark.skipif(os.name != "posix", reason="WSL runtime uses Linux flock")
def test_guest_lock_excludes_second_generation_and_recovers_after_owner_crash(tmp_path):
    from potbot.wsl_core_runtime import _core_runtime_lock

    lock = tmp_path / "core.lock"
    code = """import sys, time
from pathlib import Path
from potbot.wsl_core_runtime import _core_runtime_lock
with _core_runtime_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(CapabilityUnavailableError, match="already running"):
            with _core_runtime_lock(lock):
                pytest.fail("a second generation must not acquire the guest lock")
    finally:
        child.kill()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
    with _core_runtime_lock(lock):
        assert lock.is_file()


@pytest.mark.skipif(os.name != "posix", reason="Linux-only WSL runtime module")
def test_cancellation_before_startup_does_not_launch_children(monkeypatch):
    from potbot import wsl_core_runtime as runtime

    stopped = threading.Event()
    stopped.set()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("cancelled"))
    with pytest.raises(runtime._CoreStopped):
        runtime._run_core_owned(SimpleNamespace(state="must-not-read"), stopped)


@pytest.mark.skipif(os.name != "posix", reason="Linux-only WSL runtime module")
def test_secret_service_probe_timeout_reaps_started_process(monkeypatch, tmp_path):
    from potbot import wsl_core_runtime as runtime

    module = tmp_path / "src/potbot/syn_release/secret_service_proxy.py"
    module.parent.mkdir(parents=True)
    module.write_text("# Layout fixture; the child is replaced by a real sleeper below.\n")
    children = []
    popen = subprocess.Popen

    def child(*args, **kwargs):
        process = popen([sys.executable, "-c", "import time; time.sleep(30)"])
        children.append(process)
        return process

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("busctl", 2)

    monkeypatch.setattr(runtime.subprocess, "Popen", child)
    monkeypatch.setattr(runtime.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        runtime._start_secret_service(tmp_path, {}, threading.Event())
    assert len(children) == 1 and children[0].poll() is not None
