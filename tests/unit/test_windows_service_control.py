"""Bounded, data-only invocation of the Windows legacy task stop helper."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from types import SimpleNamespace

import pytest

from potbot import windows_service_control as control
from potbot.domain.errors import CapabilityUnavailableError
from potbot.syn_release import host


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(control, "_WINDOWS", True)
    monkeypatch.setenv("SystemRoot", "/windows")
    monkeypatch.setattr(host, "windows_environment", lambda: {"SystemRoot": "/windows"})


@pytest.mark.parametrize("include_desktop", [False, True])
def test_stop_passes_paths_as_encoded_data_and_excludes_caller(windows, monkeypatch, tmp_path, include_desktop):
    root = tmp_path / "space ' quote $(never-run)"
    root.mkdir()
    invocations = []

    def run(argv, **kwargs):
        invocations.append(argv)
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 90
        assert kwargs["env"] == {"SystemRoot": "/windows"}
        assert "-NonInteractive" in argv and "-EncodedCommand" in argv
        script = base64.b64decode(argv[-1]).decode("utf-16le")
        assert "$(never-run)" not in script
        encoded = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
        assert encoded is not None
        request = json.loads(base64.b64decode(encoded[1]))
        assert request == {
            "python": str(root / ".venv" / "Scripts" / "python.exe"),
            "postgres": str(root / ".potbot" / "native-postgres"),
            "include_desktop": include_desktop,
            "caller_pid": control.os.getpid(),
        }
        return SimpleNamespace(returncode=0, stdout=b"POTBOT_TASK_STOP=stopped\r\n")

    monkeypatch.setattr(control.subprocess, "run", run)
    control.stop_registered_task(root, include_desktop=include_desktop)
    assert len(invocations) == 1


def test_stop_absent_task_is_success(windows, monkeypatch, tmp_path):
    monkeypatch.setattr(
        control.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"POTBOT_TASK_STOP=absent\r\n"),
    )
    control.stop_registered_task(tmp_path)


@pytest.mark.parametrize("code,output", [(20, b""), (21, b""), (22, b""), (0, b"untrusted-output")])
def test_stop_refusal_and_unverified_success_are_errors(windows, monkeypatch, tmp_path, code, output):
    monkeypatch.setattr(
        control.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stdout=output),
    )
    with pytest.raises(CapabilityUnavailableError) as failure:
        control.stop_registered_task(tmp_path)
    assert "untrusted-output" not in str(failure.value)


def test_stop_timeout_does_not_report_success(windows, monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("hidden-output", 90)

    monkeypatch.setattr(control.subprocess, "run", timeout)
    with pytest.raises(CapabilityUnavailableError, match="shutdown is incomplete"):
        control.stop_registered_task(tmp_path)


def test_stop_is_noop_on_non_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "_WINDOWS", False)
    monkeypatch.setattr(control.subprocess, "run", lambda *a, **k: pytest.fail("Windows only"))
    control.stop_registered_task(tmp_path)
