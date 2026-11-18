from __future__ import annotations

import pytest

from potbot.windows_core_runtime import _wsl_environment


def test_wsl_environment_propagates_only_the_xpra_secret(monkeypatch) -> None:
    monkeypatch.setenv("WSLENV", "EXISTING/u")
    monkeypatch.setenv("POTBOT_ADMIN_DSN", "not-forwarded")
    result = _wsl_environment("x" * 48)
    assert result["POTBOT_XPRA_BRIDGE_TOKEN"] == "x" * 48
    assert result["WSLENV"].split(":") == ["EXISTING/u", "POTBOT_XPRA_BRIDGE_TOKEN"]
    assert "POTBOT_ADMIN_DSN" not in result["WSLENV"]


@pytest.mark.parametrize("token", ["short", "x\n" * 20])
def test_wsl_environment_rejects_bad_bridge_tokens(token: str) -> None:
    with pytest.raises(Exception):
        _wsl_environment(token)
