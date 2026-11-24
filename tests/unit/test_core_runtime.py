from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot import core_runtime
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError


def test_frontend_bridge_settings_are_loopback_and_workspace_scoped() -> None:
    config = SimpleNamespace(workspace_id="123e4567-e89b-12d3-a456-426614174000")
    env = core_runtime.frontend_bridge_environment(config, "x" * 48)
    assert env == {
        "POTBOT_UI_XPRA_WORKSPACE_ID": config.workspace_id,
        "POTBOT_UI_XPRA_BRIDGE_URL": "http://127.0.0.1:8766/",
        "POTBOT_UI_XPRA_BRIDGE_TOKEN": "x" * 48,
        "POTBOT_UI_XPRA_PUBLIC_WS_URL": "ws://127.0.0.1:8766/v1/xpra/ws",
    }
    with pytest.raises(InvalidInputError):
        core_runtime.frontend_bridge_environment(config, "short")


def test_native_windows_cannot_be_reported_as_full_runtime(monkeypatch) -> None:
    monkeypatch.setattr(core_runtime.sys, "platform", "win32")
    with pytest.raises(CapabilityUnavailableError, match="managed Linux/WSL"):
        core_runtime.prepare_core_runtime(SimpleNamespace())


def test_missing_operator_runtime_is_a_startup_blocker(monkeypatch) -> None:
    monkeypatch.setattr(core_runtime.sys, "platform", "linux")
    config = SimpleNamespace(operator_manifest=None, scoped_worker_launcher=False)
    with pytest.raises(CapabilityUnavailableError, match="operator manifest"):
        core_runtime.prepare_core_runtime(config)


def test_private_policy_writer_never_follows_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "policy.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(InvalidInputError):
        core_runtime._private_write(link, {"safe": True})
