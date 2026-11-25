"""Ctrl+C requests ordinary shutdown rather than raising inside Qt callbacks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from potbot import desktop, instance, oneclick
from potbot.domain.errors import CapabilityUnavailableError


def test_signal_handler_defers_locks_and_service_io_to_timer(monkeypatch, tmp_path):
    lease = SimpleNamespace(stopped=threading.Event(), root=tmp_path)
    requests = []
    monkeypatch.setattr(desktop, "request_stop", lambda role, **kw: requests.append((role, kw)))
    interrupt = desktop._DesktopInterrupt()
    interrupt.handle(signal.SIGINT, None)
    assert interrupt.requested
    assert not lease.stopped.is_set()
    assert requests == []
    interrupt.dispatch(lease)
    assert lease.stopped.is_set()
    assert requests == [("service", {"root": tmp_path})]
    interrupt.handle(signal.SIGINT, None)
    interrupt.dispatch(lease)
    assert len(requests) == 1


def test_failed_early_stop_request_does_not_raise_inside_qt_timer(monkeypatch, tmp_path):
    lease = SimpleNamespace(stopped=threading.Event(), root=tmp_path)

    def unavailable(*args, **kwargs):
        raise CapabilityUnavailableError("service has not published its receipt")

    monkeypatch.setattr(desktop, "request_stop", unavailable)
    interrupt = desktop._DesktopInterrupt(requested=True)
    interrupt.dispatch(lease)
    assert lease.stopped.is_set()


@pytest.mark.parametrize("stop_code,expected", [(0, 130), (1, 1)])
def test_interrupt_runs_full_stop_after_desktop_lease_is_released(
    monkeypatch, tmp_path, stop_code, expected,
):
    monkeypatch.setenv("POTBOT_PROJECT_ROOT", str(tmp_path))
    before = signal.getsignal(signal.SIGINT)
    calls = []

    def run(lease, interrupt):
        assert instance.is_running("desktop", root=tmp_path)
        interrupt.handle(signal.SIGINT, None)
        return 0

    def stop(arguments):
        assert arguments == ["stop"]
        assert not instance.is_running("desktop", root=tmp_path)
        # Repeated Ctrl+C cannot raise KeyboardInterrupt during final cleanup.
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler) and handler != before
        handler(signal.SIGINT, None)
        calls.append(arguments)
        return stop_code

    monkeypatch.setattr(desktop, "_run_desktop", run)
    monkeypatch.setattr(oneclick, "main", stop)
    assert desktop.main() == expected
    assert calls == [["stop"]]
    assert signal.getsignal(signal.SIGINT) == before


def test_normal_window_close_preserves_existing_service_lifetime(monkeypatch, tmp_path):
    monkeypatch.setenv("POTBOT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(desktop, "_run_desktop", lambda lease, interrupt: 0)
    monkeypatch.setattr(oneclick, "main", lambda *a: pytest.fail("ordinary close is not Ctrl+C"))
    assert desktop.main() == 0


def test_previous_handler_restored_on_desktop_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("POTBOT_PROJECT_ROOT", str(tmp_path))
    before = signal.getsignal(signal.SIGINT)

    def unavailable(*args):
        raise CapabilityUnavailableError("Qt unavailable")

    monkeypatch.setattr(desktop, "_run_desktop", unavailable)
    with pytest.raises(CapabilityUnavailableError, match="Qt unavailable"):
        desktop.main()
    assert signal.getsignal(signal.SIGINT) == before
    assert not instance.is_running("desktop", root=tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="real POSIX signal delivery; Windows uses console events")
def test_real_sigint_does_not_interrupt_python_callback():
    code = """import os, signal
from potbot.desktop import _capture_interrupt
previous = signal.getsignal(signal.SIGINT)
with _capture_interrupt() as interrupt:
    os.kill(os.getpid(), signal.SIGINT)
    assert interrupt.requested
    assert not interrupt.dispatched
    os.kill(os.getpid(), signal.SIGINT)
    print('callback-completed', flush=True)
assert signal.getsignal(signal.SIGINT) == previous
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "callback-completed"
