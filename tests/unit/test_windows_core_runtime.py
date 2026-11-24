from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from potbot.dependencies import project_root
from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError
from potbot.windows_core_runtime import (
    _MAX_PROCESS_LOG_BYTES,
    _ROOT_BOOTSTRAP,
    _SANDBOX_IMAGE_INPUTS,
    _SOURCE_TOP,
    WSL_KERNEL_ARCHIVE_SHA256,
    WSL_KERNEL_MANIFEST_SHA256,
    WSL_KERNEL_PUBLIC_KEY,
    WSL_KERNEL_RELEASE,
    WSL_KERNEL_TAG,
    _failed_bootstrap_stage,
    _failed_qualification_stage,
    _runtime_secret_environment,
    _sandbox_image_source,
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
        "SYN_HOST_CUSTODY_CLIENT_WIN": "/usr/local/libexec/potbot-parent-keystore-client",
        "SYN_HOST_CUSTODY_POWERSHELL": "/usr/local/libexec/potbot-parent-keystore-client",
        "POTBOT_PARENT_KEYSTORE_HOST": "172.30.16.1",
        "POTBOT_PARENT_KEYSTORE_PORT": "42319",
        "POTBOT_PARENT_KEYSTORE_SESSION": "SynHostCustody-" + "a" * 32,
        "POTBOT_PARENT_KEYSTORE_TOKEN": "11" * 32,
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
    assert "POTBOT_PARENT_KEYSTORE_TOKEN/u" in forwarded
    assert "SYN_HOST_CUSTODY_POWERSHELL/pu" not in forwarded
    assert result["SYN_HOST_CUSTODY_POWERSHELL"] == custody["SYN_HOST_CUSTODY_POWERSHELL"]
    assert "POTBOT_ADMIN_DSN" not in result["WSLENV"]
    assert "DBUS_SESSION_BUS_ADDRESS" not in result
    assert set(result) <= {
        "SystemRoot",
        "PATH",
        "SYN_HOST_CUSTODY_PIPE",
        "SYN_HOST_CUSTODY_CLIENT_WIN",
        "SYN_HOST_CUSTODY_POWERSHELL",
        "POTBOT_PARENT_KEYSTORE_HOST",
        "POTBOT_PARENT_KEYSTORE_PORT",
        "POTBOT_PARENT_KEYSTORE_SESSION",
        "POTBOT_PARENT_KEYSTORE_TOKEN",
        "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED",
        "POTBOT_XPRA_BRIDGE_TOKEN",
        "POTBOT_DSN",
        "POTBOT_NON_INTERACTIVE",
        "WSLENV",
    }


@pytest.mark.parametrize("token", ["short", "x\n" * 20])
def test_windows_core_runtime_rejects_bad_bridge_tokens(monkeypatch, token: str) -> None:
    from potbot.syn_release import host

    monkeypatch.setattr(host, "windows_environment", lambda: {"SystemRoot": r"C:\\Windows"})
    with pytest.raises(StateMismatchError):
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
        "service-venv-permissions",
        "service-runtime-probe",
    ):
        assert f'progress "{stage}"' in _ROOT_BOOTSTRAP
    assert "POTBOT_BOOTSTRAP_FAILED_STAGE=%s" in _ROOT_BOOTSTRAP
    assert "install -d -m 0755 -o root -g root /var/lib/potbot" in _ROOT_BOOTSTRAP
    assert "/usr/bin/python3.12 -m venv --copies" in _ROOT_BOOTSTRAP
    assert 'test ! -L "$UV_PROJECT_ENVIRONMENT/bin/python"' in _ROOT_BOOTSTRAP
    assert '--python "$UV_PROJECT_ENVIRONMENT/bin/python"' in _ROOT_BOOTSTRAP
    assert "--no-editable" in _ROOT_BOOTSTRAP
    assert "--link-mode copy" in _ROOT_BOOTSTRAP
    assert 'chgrp -hR "$service_gid" "$UV_PROJECT_ENVIRONMENT"' in _ROOT_BOOTSTRAP
    assert 'chmod -R u+rwX,g+rX,g-w,o-rwx "$UV_PROJECT_ENVIRONMENT"' in _ROOT_BOOTSTRAP
    assert 'service_gid="$(id -g synoperator)"' in _ROOT_BOOTSTRAP
    assert '-path "$UV_PROJECT_ENVIRONMENT" -prune' in _ROOT_BOOTSTRAP
    assert "chmod u+rwX,g+rX,g-w,o-rwx" in _ROOT_BOOTSTRAP
    assert "chmod g+s" in _ROOT_BOOTSTRAP
    assert (
        'install -d -m 2750 -o root -g "$service_gid" "$UV_PROJECT_ENVIRONMENT"' in _ROOT_BOOTSTRAP
    )
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
            Path(source) == candidate or candidate in Path(source).parents for candidate in staged
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
    assert (
        WSL_KERNEL_ARCHIVE_SHA256
        == "b814f2e50df83aa7fd0c5b7ca7ea8df3fa0b8694bb62ef8b8721033f1788f0c4"
    )
    assert (
        WSL_KERNEL_MANIFEST_SHA256
        == "7da3d6b6bbc778df001f557107c66d377c676a1e1d2410043ac843cebf81554e"
    )
    assert (
        WSL_KERNEL_PUBLIC_KEY == "945786a5587110a061a6b73a02057b327f1ce819db4b0b5850201fbca41149cb"
    )


def test_failed_qualification_stage_uses_last_marker() -> None:
    stderr = (
        "POTBOT_QUALIFICATION_STAGE=scoped-launcher\nPOTBOT_QUALIFICATION_STAGE=sandbox-image\n"
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


def test_sandbox_image_source_is_git_derived_and_tracks_workflow_triggers() -> None:
    assert ".github/workflows/sandbox-system-image.yml" in _SANDBOX_IMAGE_INPUTS
    assert "sandbox" in _SANDBOX_IMAGE_INPUTS
    assert "src/potbot/tools/queue_transport_worker.py" in _SANDBOX_IMAGE_INPUTS
    assert "tests/unit/test_workspace_qualification_smoke.py" in _SANDBOX_IMAGE_INPUTS

    workflow = (project_root() / ".github/workflows/sandbox-system-image.yml").read_text(
        encoding="utf-8"
    )
    push_block = workflow.split("  pull_request:", 1)[0]
    paths_block = push_block.split("    paths:", 1)[1]
    workflow_paths = {
        line.strip()[2:] for line in paths_block.splitlines() if line.strip().startswith("- ")
    }
    normalized = {item[:-3] if item.endswith("/**") else item for item in workflow_paths}
    assert set(_SANDBOX_IMAGE_INPUTS) == normalized

    value = _sandbox_image_source(project_root())
    assert len(value) == 40
    assert all(character in "0123456789abcdef" for character in value)


def test_wsl_runtime_resolves_synsphere_signed_image_instead_of_hard_coding_digest() -> None:
    root = project_root()
    source = (root / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/sandbox-system-image.yml").read_text(encoding="utf-8")
    assert 'IMAGE = "ghcr.io/synsphere/potbot-sandbox"' in source
    assert "https://github.com/Synsphere/Potbot/" in source
    assert "IMAGE_DIGEST =" not in source
    assert "IMAGE_SOURCE =" not in source
    assert "system-v1-{image_source}-amd64" in source
    assert "src/potbot/tools/queue_transport_worker.py" in workflow


def test_wsl_qualification_passes_installed_scoped_launcher_environment() -> None:
    source = (project_root() / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    assert '"POTBOT_OPERATOR_SCOPED_LAUNCHER": str(SCOPED_SOCKET)' in source
    assert "monitor_environment = _monitor_environment()" in source
    assert 'line == "# Managed by Potbot scoped launcher v2; do not edit"' in source


def test_wsl_sandbox_qualification_runs_as_complete_synoperator_identity() -> None:
    source = (project_root() / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    assert "os.chown(qroot, service.pw_uid, service.pw_gid)" in source
    assert '"--reuid",' in source
    assert "str(service.pw_uid)" in source
    assert "service.pw_gid != 69" in source
    assert '"--regid",' in source
    assert "str(service.pw_gid)" in source
    assert '"HOME": service.pw_dir' in source
    assert '"PYTHONNOUSERSITE": "1"' in source
    assert "_verify_image(" in source
    assert "service," in source


def test_wsl_image_cache_wrapper_is_traversable_but_not_writable_by_synoperator() -> None:
    source = (project_root() / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    assert "def _prepare_image_cache_access(" in source
    assert 'bundle = image_root / "bundle"' in source
    assert "os.chown(directory, 0, service.pw_gid)" in source
    assert "os.chmod(directory, 0o750)" in source
    assert "stat.S_IWGRP | stat.S_IWOTH" in source
    assert "root_info.st_uid != 0" in source
    assert "root_info.st_mode & stat.S_IXOTH" in source
    assert "_prepare_image_cache_access(image_root, rootfs, service)" in source


def test_wsl_root_prepares_qualification_scratch_before_privilege_drop() -> None:
    source = (project_root() / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    assert '("tools", 69, 0o2775)' in source
    assert '("ai", service.pw_gid, 0o775)' in source
    assert '("private", service.pw_gid, 0o700)' in source
    assert '("audit", service.pw_gid, 0o700)' in source
    assert "os.chown(path, service.pw_uid, gid)" in source
    assert "str(service.pw_uid)" in source
    assert "str(service.pw_gid)" in source


def test_wsl_workspace_shared_tools_are_gid69_setgid() -> None:
    source = (project_root() / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    assert "(tools, 69, 0o2775)" in source
    assert "_ensure_directory(path, service.pw_uid, gid, mode)" in source
    assert "(home, service.pw_gid, 0o700)" in source
    assert "(models, service.pw_gid, 0o755)" in source


def test_wsl_workspace_ai_config_matches_qualified_group_writable_layout() -> None:
    source = (project_root() / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    # The disposable image qualifier and the persistent workspace must enforce
    # the same writable /etc/ai contract. Keep history operator-private.
    assert "(ai, service.pw_gid, 0o775)" in source
    assert '("ai", service.pw_gid, 0o775)' in source
    assert "(ai, service.pw_gid, 0o700)" not in source
    assert "(past, service.pw_gid, 0o700)" in source


def test_wsl_terminal_recipe_dependency_is_in_sandbox_image() -> None:
    root = project_root()
    source = (root / "src/potbot/wsl_core_runtime.py").read_text(encoding="utf-8")
    image = (root / "sandbox/image/Dockerfile").read_text(encoding="utf-8")
    install = image.split("apt-get install -y --no-install-recommends", 1)[1].split(
        "&& apt-get clean", 1
    )[0]
    assert '"/usr/bin/xterm"' in source
    assert "xterm" in install.split()


@pytest.mark.skipif(os.name != "posix", reason="Linux-only WSL runtime module")
def test_wsl_finalize_refreshes_qualification_without_treating_it_as_image_change(
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    from potbot.wsl_core_runtime import _activate_workspace_image_pin

    workspace = uuid4()
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    digest = "a" * 64
    source = "b" * 40
    first_qualification = "c" * 64
    next_qualification = "d" * 64

    class Ledger:
        def __init__(self) -> None:
            self.pin = None
            self.calls = []

        def image_status(self, selected):
            assert selected == workspace
            return None if self.pin is None else dict(self.pin)

        def pin_image(self, selected, **kwargs):
            assert selected == workspace
            expected = kwargs.pop("expected_revision", 0)
            current = 0 if self.pin is None else self.pin["revision"]
            assert expected == current
            revision = current + 1
            self.pin = {
                **kwargs,
                "rootfs_path": str(kwargs["rootfs_path"]),
                "revision": revision,
            }
            self.calls.append(dict(self.pin))
            return revision

    ledger = Ledger()
    assert (
        _activate_workspace_image_pin(
            ledger,
            workspace,
            image_digest=digest,
            image_source=source,
            rootfs=rootfs,
            qualification=first_qualification,
        )
        == 1
    )
    assert (
        _activate_workspace_image_pin(
            ledger,
            workspace,
            image_digest=digest,
            image_source=source,
            rootfs=rootfs,
            qualification=next_qualification,
        )
        == 2
    )
    assert ledger.pin["qualification_sha256"] == next_qualification
    assert len(ledger.calls) == 2

    ledger.pin["digest"] = "e" * 64
    with pytest.raises(CapabilityUnavailableError, match="image pin differs"):
        _activate_workspace_image_pin(
            ledger,
            workspace,
            image_digest=digest,
            image_source=source,
            rootfs=rootfs,
            qualification="f" * 64,
        )
