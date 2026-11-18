"""Windows handoff for Potbot's mandatory Linux/WSL core runtime.

The native Windows service continues to own PostgreSQL, API, hosted workers and
the Next.js UI.  This module provisions and supervises the mandatory Linux-only
operator/Xpra/sandbox layer inside Potbot's already-owned WSL distribution.

No UI/API/database secret is copied into WSL.  Only the ephemeral Xpra bridge
bearer is inherited by the one WSL child through WSLENV.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from potbot.dependencies import project_root
from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError

if TYPE_CHECKING:
    from potbot.service import ServiceConfig
    from potbot.syn_release.host import WslOperatorHost

_SOURCE_TOP = (
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "src",
    "scripts",
    "sandbox/clients",
    "sandbox/qualification",
    "native/potbot-cgroup-join",
    "native/potbot-identity",
    "vocab",
)
_SKIP_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git",
    ".venv", ".potbot", "node_modules", ".next",
}
_MAX_SOURCE_BYTES = 160 * 1024 * 1024
_MAX_PROCESS_LOG_BYTES = 128 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_BOOTSTRAP_FAILED_STAGE = re.compile(r"^POTBOT_BOOTSTRAP_FAILED_STAGE=([a-z0-9-]+)$", re.MULTILINE)
_QUALIFICATION_STAGE = re.compile(r"^POTBOT_QUALIFICATION_STAGE=([a-z0-9-]+)$", re.MULTILINE)

UV_VERSION = "0.12.23"
UV_URL = (
    "https://github.com/astral-sh/uv/releases/download/0.12.23/"
    "uv-x86_64-unknown-linux-gnu.tar.gz"
)
UV_SHA256 = "9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6"
COSIGN_VERSION = "3.1.3"
COSIGN_URL = (
    "https://github.com/sigstore/cosign/releases/download/v3.1.3/cosign-linux-amd64"
)
COSIGN_SHA256 = "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"

WSL_KERNEL_TAG = "v0.5.0.dev13"
WSL_KERNEL_RELEASE = "6.18.54.1-microsoft-standard-WSL2-potbot-ns-taskstats"
WSL_KERNEL_ARCHIVE_SHA256 = "b814f2e50df83aa7fd0c5b7ca7ea8df3fa0b8694bb62ef8b8721033f1788f0c4"
WSL_KERNEL_MANIFEST_SHA256 = "7da3d6b6bbc778df001f557107c66d377c676a1e1d2410043ac843cebf81554e"
WSL_KERNEL_PUBLIC_KEY = "945786a5587110a061a6b73a02057b327f1ce819db4b0b5850201fbca41149cb"

_ROOT_BOOTSTRAP = r"""
set -eu
stage="bootstrap"
cleanup_path=""
finish() {
  code=$?
  if [ -n "$cleanup_path" ]; then
    rm -rf "$cleanup_path" || true
  fi
  if [ "$code" -ne 0 ]; then
    printf 'POTBOT_BOOTSTRAP_FAILED_STAGE=%s\n' "$stage" >&2
  fi
  exit "$code"
}
trap finish 0

bundle="$1"
source_sha="$2"
uv_url="$3"
uv_sha="$4"
cosign_url="$5"
cosign_sha="$6"
test -f "$bundle"
test "${#source_sha}" -eq 64

stage="ubuntu-release"
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
export DEBIAN_FRONTEND=noninteractive

stage="ubuntu-repositories"
apt-get update
apt-get install --yes --no-install-recommends software-properties-common
command -v add-apt-repository >/dev/null
add-apt-repository --yes universe
apt-get update

stage="core-packages"
apt-get install --yes --no-install-recommends \
  ca-certificates curl python3.12 python3.12-venv python3.12-dev \
  xpra xauth bubblewrap uidmap libseccomp2 strace build-essential \
  skopeo umoci jq util-linux
command -v xpra >/dev/null
command -v bwrap >/dev/null
command -v xauth >/dev/null
command -v skopeo >/dev/null
command -v umoci >/dev/null
command -v strace >/dev/null
command -v setpriv >/dev/null

stage="runtime-directories"
install -d -m 0755 -o root -g root /usr/local/libexec
install -d -m 0755 -o root -g root /var/lib/potbot/core-source

stage="uv"
uv_dir=/usr/local/libexec/potbot-uv-0.12.23
uv_bin="$uv_dir/uv"
uv_marker="$uv_dir/.archive-sha256"
if [ -e "$uv_dir" ]; then
  test -x "$uv_bin"
  test -f "$uv_marker"
  test "$(cat "$uv_marker")" = "$uv_sha"
else
  cleanup_path="$(mktemp -d /var/tmp/potbot-uv.XXXXXX)"
  curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    "$uv_url" -o "$cleanup_path/uv.tar.gz"
  test "$(sha256sum "$cleanup_path/uv.tar.gz" | awk '{print $1}')" = "$uv_sha"
  install -d -m 0755 -o root -g root "$uv_dir"
  tar -xzf "$cleanup_path/uv.tar.gz" -C "$cleanup_path"
  found="$(find "$cleanup_path" -type f -name uv -perm -u+x | head -n 1)"
  test -n "$found"
  install -o root -g root -m 0755 "$found" "$uv_bin"
  printf '%s\n' "$uv_sha" > "$uv_marker"
  chmod 0444 "$uv_marker"
  rm -rf "$cleanup_path"
  cleanup_path=""
fi

stage="cosign"
cosign=/usr/local/libexec/potbot-cosign
if [ ! -x "$cosign" ] || [ "$(sha256sum "$cosign" | awk '{print $1}')" != "$cosign_sha" ]; then
  cleanup_path="$(mktemp /var/tmp/potbot-cosign.XXXXXX)"
  curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    "$cosign_url" -o "$cleanup_path"
  test "$(sha256sum "$cleanup_path" | awk '{print $1}')" = "$cosign_sha"
  install -o root -g root -m 0755 "$cleanup_path" "$cosign"
  rm -f "$cleanup_path"
  cleanup_path=""
fi

stage="source-staging"
source_root="/var/lib/potbot/core-source/$source_sha"
marker="$source_root/.potbot-source-sha256"
if [ -e "$source_root" ] && [ ! -f "$marker" ]; then
  echo "Existing core source needs reconciliation" >&2
  exit 20
fi
if [ ! -f "$marker" ]; then
  pending="/var/lib/potbot/core-source/.${source_sha}.pending"
  rm -rf "$pending"
  install -d -m 0755 -o root -g root "$pending"
  /usr/bin/python3.12 - "$bundle" "$pending" <<'PY'
import pathlib, sys, tarfile
archive = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
with tarfile.open(archive, "r:gz") as tf:
    tf.extractall(target, filter="data")
PY
  printf '%s\n' "$source_sha" > "$pending/.potbot-source-sha256"
  chmod 0444 "$pending/.potbot-source-sha256"
  mv "$pending" "$source_root"
fi
test "$(cat "$marker")" = "$source_sha"

stage="locked-python"
cd "$source_root"
export UV_PROJECT_ENVIRONMENT="$source_root/.venv"
"$uv_bin" sync --locked --no-dev \
  --extra web --extra security --extra semantics --extra credentials --extra skills \
  --python /usr/bin/python3.12
test -x "$source_root/.venv/bin/python"

stage="complete"
printf '%s\n' "$source_root/.venv/bin/python"
trap - 0
"""


@dataclass(frozen=True)
class WindowsCoreRuntime:
    command: tuple[str, ...]
    environment: dict[str, str]


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total = 0
    for name in _SOURCE_TOP:
        selected = root / name
        if not selected.exists():
            if name == "vocab":
                continue
            raise CapabilityUnavailableError(f"Core runtime source is missing: {name}")
        candidates = [selected] if selected.is_file() else selected.rglob("*")
        for path in candidates:
            relative = path.relative_to(root)
            if any(part in _SKIP_NAMES for part in relative.parts):
                continue
            if path.is_symlink():
                raise CapabilityUnavailableError("Core runtime source contains a symbolic link")
            if not path.is_file():
                continue
            size = path.stat().st_size
            total += size
            if total > _MAX_SOURCE_BYTES:
                raise CapabilityUnavailableError("Core runtime source bundle exceeds its bound")
            files.append(path)
    return sorted(set(files), key=lambda p: p.as_posix())


def _source_digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _write_bundle(root: Path, files: list[Path], target: Path) -> None:
    if target.is_symlink():
        raise CapabilityUnavailableError("Core runtime staging archive is redirected")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".pending")
    temporary.unlink(missing_ok=True)
    with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            payload = path.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = stat.S_IMODE(path.stat().st_mode) & 0o755 or 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    os.replace(temporary, target)


def _write_process_log(
    root: Path, name: str, result: subprocess.CompletedProcess[str]
) -> Path:
    directory = root / ".potbot" / "service"
    if directory.exists() and directory.is_symlink():
        raise CapabilityUnavailableError("Core runtime service log directory is redirected")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.is_symlink():
        raise CapabilityUnavailableError("Core runtime diagnostic log is redirected")
    payload = (
        f"return_code={result.returncode}\n"
        f"--- stdout ---\n{result.stdout or ''}\n"
        f"--- stderr ---\n{result.stderr or ''}\n"
    ).encode("utf-8", errors="replace")
    if len(payload) > _MAX_PROCESS_LOG_BYTES:
        prefix = b"[older diagnostic output truncated]\n"
        payload = prefix + payload[-(_MAX_PROCESS_LOG_BYTES - len(prefix)) :]
    temporary = target.with_name("." + target.name + ".pending")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _failed_bootstrap_stage(stderr: str) -> str:
    matches = _BOOTSTRAP_FAILED_STAGE.findall(stderr)
    return matches[-1] if matches else "unknown"


def _failed_qualification_stage(stderr: str) -> str:
    matches = _QUALIFICATION_STAGE.findall(stderr)
    return matches[-1] if matches else "unknown"


def _ensure_required_wsl_kernel(manager: "WslOperatorHost") -> None:
    """Boot the owned WSL runtime on Potbot's signed TASKSTATS kernel."""
    from potbot.syn_release.host import DISTRO, run_host
    from potbot.wsl_kernel import download_bundle, verify_bundle
    from potbot.wsl_kernel_activation import WslKernelActivation

    current = run_host(manager.command("/usr/bin/uname", ["-r"])).strip()
    if not current or "\n" in current or "\r" in current:
        raise CapabilityUnavailableError("Managed WSL kernel identity is invalid")
    if current == WSL_KERNEL_RELEASE:
        return

    # Kernel selection is global to the WSL2 VM. Stop only Potbot's owned distro;
    # WslKernelActivation will refuse to proceed if any unrelated distro remains
    # active rather than silently terminating somebody else's work.
    run_host([manager.wsl, "--terminate", DISTRO], timeout=90)

    release_root = manager.root / "wsl-kernel-release"
    release_root.mkdir(parents=True, exist_ok=True)
    bundle = release_root / WSL_KERNEL_MANIFEST_SHA256
    if bundle.exists() or bundle.is_symlink():
        verify_bundle(
            bundle,
            manifest_sha256=WSL_KERNEL_MANIFEST_SHA256,
            public_key=WSL_KERNEL_PUBLIC_KEY,
        )
    else:
        download_bundle(
            bundle,
            tag=WSL_KERNEL_TAG,
            kernel_release=WSL_KERNEL_RELEASE,
            archive_sha256=WSL_KERNEL_ARCHIVE_SHA256,
            manifest_sha256=WSL_KERNEL_MANIFEST_SHA256,
            public_key=WSL_KERNEL_PUBLIC_KEY,
        )

    activated = WslKernelActivation(manager).activate(
        bundle,
        manifest_sha256=WSL_KERNEL_MANIFEST_SHA256,
        public_key=WSL_KERNEL_PUBLIC_KEY,
    )
    if (
        activated.get("activation") != "boot_verified"
        or activated.get("kernel_release") != WSL_KERNEL_RELEASE
    ):
        raise CapabilityUnavailableError("Managed WSL TASKSTATS kernel did not verify after boot")

    # Activation intentionally terminates the probe distro after boot evidence.
    # Restore the managed user session before the ordinary core bootstrap starts.
    manager.start_keystore()


def _head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    value = result.stdout.strip()
    if result.returncode or _COMMIT.fullmatch(value) is None:
        raise CapabilityUnavailableError("Current Potbot Git commit could not be identified")
    return value


def _wsl_environment(token: str) -> dict[str, str]:
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or "\n" in token or "\r" in token:
        raise StateMismatchError("Xpra bridge token is invalid")
    env = dict(os.environ)
    env["POTBOT_XPRA_BRIDGE_TOKEN"] = token
    existing = [item for item in env.get("WSLENV", "").split(":") if item]
    if "POTBOT_XPRA_BRIDGE_TOKEN" not in existing:
        existing.append("POTBOT_XPRA_BRIDGE_TOKEN")
    env["WSLENV"] = ":".join(existing)
    return env


def prepare_windows_core_runtime(config: "ServiceConfig", token: str) -> WindowsCoreRuntime:
    if os.name != "nt":
        raise CapabilityUnavailableError("Windows core runtime handoff requires Windows")
    from potbot.syn_release.host import DISTRO, WslOperatorHost, run_host

    root = project_root()
    files = _source_files(root)
    source_sha = _source_digest(root, files)
    if _HEX.fullmatch(source_sha) is None:
        raise AssertionError("source digest is not canonical")
    manager = WslOperatorHost()
    manager.setup()
    _ensure_required_wsl_kernel(manager)

    staging = manager.root / "core-runtime" / source_sha
    bundle = staging / "source.tar.gz"
    _write_bundle(root, files, bundle)
    native = run_host(manager.command("/usr/bin/wslpath", ["-a", "-u", str(bundle)])).strip()
    if not native.startswith("/") or "\n" in native:
        raise CapabilityUnavailableError("Core runtime source bundle is not visible to WSL")

    root_command = [
        manager.wsl, "--distribution", DISTRO, "--user", "root", "--exec",
        "/bin/sh", "-c", _ROOT_BOOTSTRAP, "potbot-core",
        native, source_sha, UV_URL, UV_SHA256, COSIGN_URL, COSIGN_SHA256,
    ]
    result = subprocess.run(
        root_command, capture_output=True, text=True, timeout=1800, check=False,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
    )
    bootstrap_log = _write_process_log(root, "wsl-core-bootstrap.log", result)
    if result.returncode:
        stage = _failed_bootstrap_stage(result.stderr or "")
        raise CapabilityUnavailableError(
            f"Managed WSL core bootstrap failed during {stage}; inspect {bootstrap_log}"
        )
    python = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    expected = f"/var/lib/potbot/core-source/{source_sha}/.venv/bin/python"
    if python != expected:
        raise CapabilityUnavailableError("Managed WSL core Python did not confirm its identity")

    prepare = subprocess.run(
        [
            manager.wsl, "--distribution", DISTRO, "--user", "root", "--exec",
            python, "-I", "-B", "-m", "potbot.wsl_core_runtime", "prepare",
            "--workspace-id", config.workspace_id,
            "--principal-id", config.principal_id,
            "--source-root", f"/var/lib/potbot/core-source/{source_sha}",
            "--source-sha256", source_sha,
            "--consumer-commit", _head(root),
        ],
        capture_output=True, text=True, timeout=1800, check=False,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
    )
    qualification_log = _write_process_log(root, "wsl-core-qualification.log", prepare)
    if prepare.returncode:
        stage = _failed_qualification_stage(prepare.stderr or "")
        raise CapabilityUnavailableError(
            f"Managed WSL core qualification failed during {stage}; inspect {qualification_log}"
        )
    try:
        import json
        record = json.loads(prepare.stdout.strip().splitlines()[-1])
        state = record["state"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise CapabilityUnavailableError("Managed WSL core runtime returned an invalid receipt") from None
    if not isinstance(state, str) or not state.startswith("/home/synoperator/"):
        raise CapabilityUnavailableError("Managed WSL core runtime state escaped its private owner")

    return WindowsCoreRuntime(
        command=tuple(manager.command(
            python,
            ["-I", "-B", "-m", "potbot.wsl_core_runtime", "run", "--state", state],
        )),
        environment=_wsl_environment(token),
    )
