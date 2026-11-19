from __future__ import annotations

import os
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
    _runtime_secret_environment,
    _stream_process,
    _supervisor_command,
    _write_process_log,
    _wsl_environment,
)


def test_windows_core_runtime_forwards_only_scoped_runtime_secrets(monkeypatch) -> None:
    from potbot.syn_release import host

    monkeypatch.setenv("POTBOT_ADMIN_DSN", "must-not-reach-core-runtime")
    monkeypatch.setattr(
        host,
        "windows_environment",
        lambda: {"SystemRoot": r"C:\\Windows", "PATH": r"C:\\Windows\\System32"},
    )
    token = "x" * 48
    application_dsn = "postgresql://restricted:secret@127.0.0.1:55436/potbot"
    supervisor = _runtime_secret_environment(token, application_dsn)
    assert supervisor["POTBOT_XPRA_BRIDGE_TOKEN"] == token
    assert supervisor["POTBOT_DSN"] == application_dsn
    assert supervisor["POTBOT_NON_INTERACTIVE"] == "1"
    assert "POTBOT_ADMIN_DSN" not in supervisor

    custody = {
        **supervisor,
        "SYN_HOST_CUSTODY_PIPE": "SynHostCustody-" + "a" * 32,
        "SYN_HOST_CUSTODY_CLIENT_WIN": r"C:\\Potbot\\pipe-client.ps1",
        "SYN_HOST_CUSTODY_POWERSHELL": (
            r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
        ),
        "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED": "1",
        "WSLENV": (
            "SYN_HOST_CUSTODY_PIPE/u:"
            "SYN_HOST_CUSTODY_CLIENT_WIN/u:"
            "SYN_HOST_CUSTODY_POWERSHELL/pu:"
            "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED/u"
        ),
    }
    result = _wsl_environment(custody, token, application_dsn)
    forwarded = result["WSLENV"].split(":")
    assert "POTBOT_XPRA_BRIDGE_TOKEN/u" in forwarded
    assert "POTBOT_DSN/u" in forwarded
    assert "POTBOT_NON_INTERACTIVE/u" in forwarded
    assert "POTBOT_ADMIN_DSN" not in result["WSLENV"]
    assert "DBUS_SESSION_BUS_ADDRESS" not in result


@pytest.mark.parametrize("token", ["short", "x\n" * 20])
def test_windows_core_runtime_rejects_bad_bridge_tokens(monkeypatch, token: str) -> None:
    from potbot.syn_release import host

    monkeypatch.setattr(host, "windows_environment", lambda: {"SystemRoot": r"C:\\Windows"})
    with pytest.raises(Exception):
        _runtime_secret_environment(token, "postgresql://restricted@127.0.0.1/potbot")


def test_windows_core_runtime_uses_host_custody_supervisor() -> None:
    command = _supervisor_command(
        "/var/lib/potbot/core-source/" + "a" * 64 + "/.venv/bin/python",
        "/home/synoperator/.local/share/potbot/core/workspace/private/core-state.json",
    )
    assert command[:6] == (
        __import__("sys").executable,
        "-I",
        "-B",
        "-m",
        "potbot.windows_core_runtime",
        "supervise",
    )
    source = (project_root() / "src/potbot/windows_core_runtime.py").read_text(encoding="utf-8")
    assert "with windows_host_custody(manager) as custody_environment:" in source
    assert 'resolve_postgres_dsn("application")' in source
    assert '"POTBOT_NON_INTERACTIVE": "1"' in source


def test_root_bootstrap_enables_universe_and_marks_failure_stages() -> None:
    assert "add-apt-repository --yes universe" in _ROOT_BOOTSTRAP
    for stage in (
        "ubuntu-repositories",
        "core-packages",
        "uv",
        "cosign",
        "source-staging",
        "locked-python",
        "service-runtime-permissions",
        "service-runtime-probe",
    ):
        assert f'progress "{stage}"' in _ROOT_BOOTSTRAP
    assert "POTBOT_BOOTSTRAP_FAILED_STAGE=%s" in _ROOT_BOOTSTRAP
    assert "install -d -m 0755 -o root -g root /var/lib/potbot" in _ROOT_BOOTSTRAP
    assert "/usr/bin/python3.12 -m venv --copies" in _ROOT_BOOTSTRAP
    assert 'test ! -L "$UV_PROJECT_ENVIRONMENT/bin/python"' in _ROOT_BOOTSTRAP
    assert '--python "$UV_PROJECT_ENVIRONMENT/bin/python"' in _ROOT_BOOTSTRAP
    assert "--no-editable" in _ROOT_BOOTSTRAP
    assert 'service_gid="$(id -g synoperator)"' in _ROOT_BOOTSTRAP
    assert '-path "$UV_PROJECT_ENVIRONMENT" -prune' in _ROOT_BOOTSTRAP
    assert 'chmod u+rwX,g+rX,g-w,o-rwx' in _ROOT_BOOTSTRAP
    assert 'chmod g+s' in _ROOT_BOOTSTRAP
    assert 'install -d -m 2750 -o root -g "$service_gid" "$UV_PROJECT_ENVIRONMENT"' in _ROOT_BOOTSTRAP
    assert "umask 0027" in _ROOT_BOOTSTRAP
    assert "/usr/sbin/runuser -u synoperator --" in _ROOT_BOOTSTRAP
    assert "import potbot.tools.scoped_provision" in _ROOT_BOOTSTRAP
    assert "import potbot.tools.scoped_launcher" in _ROOT_BOOTSTRAP


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
    assert "[Potbot WSL] qualification:" in source


def test_stream_process_forwards_and_retains_output(capsys) -> None:
    result = _stream_process(
        [
            __import__("sys").executable,
            "-c",
            "import sys; print('live-out'); print('live-err', file=sys.stderr)",
        ],
        timeout=10,
        env=dict(os.environ),
    )
    visible = capsys.readouterr()
    assert result.returncode == 0
    assert "live-out" in visible.out
    assert "live-err" in visible.err
    assert "live-out" in result.stdout
    assert "live-err" in result.stderr
