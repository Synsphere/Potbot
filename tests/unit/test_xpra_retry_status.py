"""A failed runtime becomes terminal when the supervisor stops retrying it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from potbot import oneclick, service
from potbot.domain.errors import CapabilityUnavailableError
from potbot.instance import InstanceLease


def test_exhausted_runtime_failure_reaches_startup_waiter(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(oneclick, "project_root", lambda: tmp_path)
    config = SimpleNamespace(frontend_enabled=True, workspace_id="this-workspace")
    home = tmp_path / ".potbot" / "service"
    home.mkdir(parents=True)
    with InstanceLease("service", root=tmp_path):
        # Use the same projection and writer as the real supervisor. While its
        # restart budget remains, the child error must not end startup.
        stage = service._xpra_display_stage(
            "runtime-failed", ready=False, blocked=False, timed_out=False,
        )
        assert stage == "runtime-failed"
        service._write_xpra_status(home, config.workspace_id, stage, False)
        assert oneclick._xpra_readiness(config) == (False, "")
        oneclick._raise_xpra_failure(config)

        # Once the supervisor blocks further launches, it must publish a
        # terminal state instead of leaving oneclick in its long retry loop.
        stage = service._xpra_display_stage(
            "runtime-failed", ready=False, blocked=True, timed_out=False,
        )
        assert stage == "restart-limit"
        service._write_xpra_status(home, config.workspace_id, stage, False)
        with pytest.raises(CapabilityUnavailableError, match="restart limit reached"):
            oneclick._raise_xpra_failure(config)


@pytest.mark.parametrize("stage", [
    "custody-tcp-failed", "custody-auth-failed", "custody-firewall-failed",
    "custody-firewall-declined", "startup-timeout",
])
def test_terminal_failure_keeps_its_specific_diagnostic(stage):
    assert service._xpra_display_stage(
        stage, ready=False, blocked=True, timed_out=False,
    ) == stage


@pytest.mark.parametrize(("stage", "ready", "timed_out", "expected"), [
    ("runtime-failed", True, False, "ready"),
    ("xpra-wsl-ready", False, False, "host-bridge-unreachable"),
    ("starting", False, True, "startup-timeout"),
    ("secret-service", False, False, "secret-service"),
])
def test_other_supervisor_readiness_transitions_are_preserved(stage, ready, timed_out, expected):
    assert service._xpra_display_stage(
        stage, ready=ready, blocked=False, timed_out=timed_out,
    ) == expected
