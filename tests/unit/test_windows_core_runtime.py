from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from potbot.dependencies import project_root
from potbot.windows_core_runtime import (
    _MAX_PROCESS_LOG_BYTES,
    _ROOT_BOOTSTRAP,
    _SOURCE_TOP,
    WSL_KERNEL_ARCHIVE_SHA256,
    WSL_KERNEL_MANIFEST_SHA256,
    WSL_KERNEL_PUBLIC_KEY,
    WSL_KERNEL_RELEASE,
    WSL_KERNEL_TAG,
    _ensure_required_wsl_kernel,
    _failed_bootstrap_stage,
    _failed_qualification_stage,
    _write_process_log,
    _wsl_environment,
)


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


def test_root_bootstrap_enables_universe_and_marks_failure_stages() -> None:
    assert "add-apt-repository --yes universe" in _ROOT_BOOTSTRAP
    for stage in (
        "ubuntu-repositories",
        "core-packages",
        "uv",
        "cosign",
        "source-staging",
        "locked-python",
    ):
        assert f'stage="{stage}"' in _ROOT_BOOTSTRAP
    assert "POTBOT_BOOTSTRAP_FAILED_STAGE=%s" in _ROOT_BOOTSTRAP


def test_wsl_source_bundle_covers_all_wheel_force_includes() -> None:
    root = project_root()
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    forced = document["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    staged = tuple(Path(item) for item in _SOURCE_TOP)

    missing = [
        source
        for source in forced
        if not any(
            Path(source) == candidate or candidate in Path(source).parents
            for candidate in staged
        )
    ]
    assert missing == []


def test_failed_bootstrap_stage_uses_last_marker() -> None:
    stderr = (
        "apt output\nPOTBOT_BOOTSTRAP_FAILED_STAGE=ubuntu-repositories\n"
        "retry output\nPOTBOT_BOOTSTRAP_FAILED_STAGE=core-packages\n"
    )
    assert _failed_bootstrap_stage(stderr) == "core-packages"
    assert _failed_bootstrap_stage("ordinary error") == "unknown"


def test_process_log_is_bounded_and_retained_under_service_home(tmp_path) -> None:
    result = subprocess.CompletedProcess(
        ["fixture"], 17, stdout="o" * _MAX_PROCESS_LOG_BYTES, stderr="e" * _MAX_PROCESS_LOG_BYTES
    )
    path = _write_process_log(tmp_path, "wsl-core-bootstrap.log", result)
    payload = path.read_bytes()
    assert path == tmp_path / ".potbot" / "service" / "wsl-core-bootstrap.log"
    assert len(payload) <= _MAX_PROCESS_LOG_BYTES
    assert payload.startswith(b"[older diagnostic output truncated]\n")
    assert b"e" * 1024 in payload

def test_windows_core_pins_the_signed_taskstats_kernel_release() -> None:
    assert WSL_KERNEL_TAG == "v0.5.0.dev13"
    assert WSL_KERNEL_RELEASE.endswith("-potbot-ns-taskstats")
    assert WSL_KERNEL_ARCHIVE_SHA256 == "b814f2e50df83aa7fd0c5b7ca7ea8df3fa0b8694bb62ef8b8721033f1788f0c4"
    assert WSL_KERNEL_MANIFEST_SHA256 == "7da3d6b6bbc778df001f557107c66d377c676a1e1d2410043ac843cebf81554e"
    assert WSL_KERNEL_PUBLIC_KEY == "945786a5587110a061a6b73a02057b327f1ce819db4b0b5850201fbca41149cb"


def test_failed_qualification_stage_uses_last_marker() -> None:
    stderr = (
        "POTBOT_QUALIFICATION_STAGE=scoped-launcher\n"
        "POTBOT_QUALIFICATION_STAGE=sandbox-image\n"
    )
    assert _failed_qualification_stage(stderr) == "sandbox-image"
    assert _failed_qualification_stage("ordinary error") == "unknown"


def test_wsl_qualifier_emits_named_stage_markers() -> None:
    source = (project_root() / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    for stage in (
        "runtime-preflight",
        "service-identities",
        "scoped-launcher",
        "sandbox-image",
        "workspace-state",
        "finalize",
        "complete",
    ):
        assert f'_qualification_stage("{stage}")' in source
