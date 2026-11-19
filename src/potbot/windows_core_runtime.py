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
import sys
import tarfile
import threading
from collections.abc import Sequence
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
_SANDBOX_IMAGE_INPUTS = (
    ".github/workflows/sandbox-system-image.yml",
    "sandbox",
    "src/potbot/tools/xpra_browser_worker.py",
    "src/potbot/tools/browser_https_routes.py",
    "src/potbot/tools/queue_transport_worker.py",
    "src/potbot/tools/python_profile_worker.py",
)
_SKIP_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git",
    ".venv", ".potbot", "node_modules", ".next",
}
_MAX_SOURCE_BYTES = 160 * 1024 * 1024
_MAX_PROCESS_LOG_BYTES = 128 * 1024
_MAX_WSL_TRANSFER_OUTPUT = 64 * 1024
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
progress() {
  stage="$1"
  printf '[Potbot WSL] %s\n' "$stage" >&2
}
progress "bootstrap"
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

progress "ubuntu-release"
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
export DEBIAN_FRONTEND=noninteractive

progress "ubuntu-repositories"
apt-get update
apt-get install --yes --no-install-recommends software-properties-common
command -v add-apt-repository >/dev/null
add-apt-repository --yes universe
apt-get update

progress "core-packages"
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

progress "runtime-directories"
install -d -m 0755 -o root -g root /usr/local/libexec
install -d -m 0755 -o root -g root /var/lib/potbot
install -d -m 0755 -o root -g root /var/lib/potbot/core-source

progress "uv"
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

progress "cosign"
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

progress "source-staging"
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
rm -f "$bundle"
test "$(cat "$marker")" = "$source_sha"

progress "service-runtime-permissions"
cd "$source_root"
service_gid="$(id -g synoperator)"
test "$service_gid" -gt 0
export UV_PROJECT_ENVIRONMENT="$source_root/.venv"
if [ -e "$UV_PROJECT_ENVIRONMENT" ]; then
  if (
    [ -L "$UV_PROJECT_ENVIRONMENT" ] ||
    [ -L "$UV_PROJECT_ENVIRONMENT/bin/python" ] ||
    [ ! -f "$UV_PROJECT_ENVIRONMENT/pyvenv.cfg" ] ||
    [ "$(stat -c %g "$UV_PROJECT_ENVIRONMENT")" -ne "$service_gid" ]
  ); then
    rm -rf "$UV_PROJECT_ENVIRONMENT"
  fi
fi
find "$source_root" -path "$UV_PROJECT_ENVIRONMENT" -prune -o -exec chgrp "$service_gid" {} +
find "$source_root" -path "$UV_PROJECT_ENVIRONMENT" -prune -o -exec chmod u+rwX,g+rX,g-w,o-rwx {} +
find "$source_root" -path "$UV_PROJECT_ENVIRONMENT" -prune -o -type d -exec chmod g+s {} +

progress "locked-python"
if [ ! -d "$UV_PROJECT_ENVIRONMENT" ]; then
  install -d -m 2750 -o root -g "$service_gid" "$UV_PROJECT_ENVIRONMENT"
  umask 0027
  /usr/bin/python3.12 -m venv --copies "$UV_PROJECT_ENVIRONMENT"
fi
test -f "$UV_PROJECT_ENVIRONMENT/bin/python"
test ! -L "$UV_PROJECT_ENVIRONMENT/bin/python"
umask 0027
"$uv_bin" sync --locked --no-dev --no-editable --link-mode copy \
  --extra web --extra security --extra semantics --extra credentials --extra skills \
  --python "$UV_PROJECT_ENVIRONMENT/bin/python"
test -x "$UV_PROJECT_ENVIRONMENT/bin/python"
test ! -L "$UV_PROJECT_ENVIRONMENT/bin/python"

progress "service-venv-permissions"
chgrp -hR "$service_gid" "$UV_PROJECT_ENVIRONMENT"
chmod -R u+rwX,g+rX,g-w,o-rwx "$UV_PROJECT_ENVIRONMENT"
find "$UV_PROJECT_ENVIRONMENT" -type d -exec chmod g+s {} +
test -z "$(find "$UV_PROJECT_ENVIRONMENT" ! -group "$service_gid" -print -quit)"

progress "service-runtime-probe"
/usr/sbin/runuser -u synoperator -- \
  "$source_root/.venv/bin/python" -I -B -c \
  'import potbot.tools.scoped_provision; import potbot.tools.scoped_launcher'

progress "complete"
printf '%s\n' "$source_root/.venv/bin/python"
trap - 0
"""


_WSL_FILE_RECEIVER = r"""
import hashlib, os, pathlib, sys
target = pathlib.Path(sys.argv[1])
limit = int(sys.argv[2])
expected = sys.argv[3]
if (
    not target.is_absolute()
    or ".." in target.parts
    or limit <= 0
    or len(expected) != 64
    or any(c not in "0123456789abcdef" for c in expected)
):
    raise SystemExit(64)
parent = target.parent
parent.mkdir(mode=0o700, parents=True, exist_ok=True)
info = parent.stat()
if (
    parent.is_symlink()
    or parent.resolve(strict=True) != parent
    or info.st_uid != 0
    or info.st_mode & 0o022
    or target.is_symlink()
):
    raise SystemExit(65)
payload = sys.stdin.buffer.read(limit + 1)
if len(payload) > limit or hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit(66)
temporary = target.with_name("." + target.name + ".pending")
try:
    temporary.unlink(missing_ok=True)
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.chown(temporary, 0, 0)
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
finally:
    temporary.unlink(missing_ok=True)
"""


def _copy_private_file_to_wsl(
    manager: "WslOperatorHost",
    source: Path,
    target: str,
    *,
    limit: int,
) -> None:
    from potbot.syn_release.host import DISTRO, windows_environment

    if (
        not source.is_file()
        or source.is_symlink()
        or not target.startswith("/var/lib/potbot/bootstrap/")
        or "\x00" in target
    ):
        raise CapabilityUnavailableError("WSL private bootstrap transfer is invalid")
    payload = source.read_bytes()
    if len(payload) > limit:
        raise CapabilityUnavailableError("WSL private bootstrap transfer exceeds its bound")
    expected = hashlib.sha256(payload).hexdigest()
    result = subprocess.run(
        [
            manager.wsl,
            "--distribution",
            DISTRO,
            "--user",
            "root",
            "--exec",
            "/usr/bin/python3.12",
            "-I",
            "-B",
            "-c",
            _WSL_FILE_RECEIVER,
            target,
            str(limit),
            expected,
        ],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
        env=windows_environment(),
    )
    if len(result.stdout) + len(result.stderr) > _MAX_WSL_TRANSFER_OUTPUT:
        raise CapabilityUnavailableError("WSL bootstrap transfer diagnostics exceeded their bound")
    if result.returncode:
        raise CapabilityUnavailableError("WSL private bootstrap transfer failed")


@dataclass(frozen=True)
class WindowsCoreRuntime:
    command: tuple[str, ...]
    environment: dict[str, str]


def _supervisor_command(python: str, state: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-B",
        "-m",
        "potbot.windows_core_runtime",
        "supervise",
        "--python",
        python,
        "--state",
        state,
    )


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


def _stream_process(
    argv: list[str],
    *,
    timeout: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    assert process.stdout is not None and process.stderr is not None
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}
    lock = threading.Lock()

    def pump(name: str, stream, sink) -> None:
        for line in iter(stream.readline, ""):
            print(line, end="", file=sink, flush=True)
            with lock:
                captured[name].append(line)
                sizes[name] += len(line.encode("utf-8", errors="replace"))
                while sizes[name] > _MAX_PROCESS_LOG_BYTES and captured[name]:
                    removed = captured[name].pop(0)
                    sizes[name] -= len(removed.encode("utf-8", errors="replace"))

    threads = (
        threading.Thread(target=pump, args=("stdout", process.stdout, sys.stdout), daemon=True),
        threading.Thread(target=pump, args=("stderr", process.stderr, sys.stderr), daemon=True),
    )
    for thread in threads:
        thread.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(
        argv,
        return_code,
        stdout="".join(captured["stdout"]),
        stderr="".join(captured["stderr"]),
    )


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
    # The next owned WSL command boots it again. Do not start a Linux credential
    # service here: Windows operator custody remains in the DPAPI host broker.


def _head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    value = result.stdout.strip()
    if result.returncode or _COMMIT.fullmatch(value) is None:
        raise CapabilityUnavailableError("Current Potbot Git commit could not be identified")
    return value


def _sandbox_image_source(root: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "-1",
            "--format=%H",
            "--",
            *_SANDBOX_IMAGE_INPUTS,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode or _COMMIT.fullmatch(value) is None:
        raise CapabilityUnavailableError(
            "Sandbox image source commit could not be identified from Git history"
        )
    return value


def _runtime_secret_environment(token: str, application_dsn: str) -> dict[str, str]:
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or "\n" in token or "\r" in token:
        raise StateMismatchError("Xpra bridge token is invalid")
    if (
        not isinstance(application_dsn, str)
        or not application_dsn
        or len(application_dsn) > 4096
        or "\x00" in application_dsn
        or "\n" in application_dsn
        or "\r" in application_dsn
    ):
        raise StateMismatchError("Restricted PostgreSQL application credential is invalid")
    from potbot.syn_release.host import windows_environment

    env = windows_environment()
    env.update(
        {
            "POTBOT_XPRA_BRIDGE_TOKEN": token,
            "POTBOT_DSN": application_dsn,
            "POTBOT_NON_INTERACTIVE": "1",
        }
    )
    return env


def _wsl_environment(
    custody_environment: dict[str, str], token: str, application_dsn: str
) -> dict[str, str]:
    env = dict(custody_environment)
    env.update(_runtime_secret_environment(token, application_dsn))
    existing = [item for item in custody_environment.get("WSLENV", "").split(":") if item]
    for item in (
        "POTBOT_XPRA_BRIDGE_TOKEN/u",
        "POTBOT_DSN/u",
        "POTBOT_NON_INTERACTIVE/u",
    ):
        if item not in existing:
            existing.append(item)
    env["WSLENV"] = ":".join(existing)
    return env


def _supervise_windows_core_runtime(python: str, state: str) -> int:
    if os.name != "nt":
        raise CapabilityUnavailableError("Windows core runtime supervisor requires Windows")
    if not python.startswith("/") or not state.startswith("/home/synoperator/"):
        raise StateMismatchError("Managed WSL core runtime command escaped its owned paths")
    token = os.environ.get("POTBOT_XPRA_BRIDGE_TOKEN", "")
    application_dsn = os.environ.get("POTBOT_DSN", "")
    from potbot.syn_release.host import WslOperatorHost
    from potbot.syn_release.windows_custody import windows_host_custody

    manager = WslOperatorHost()
    with windows_host_custody(manager) as custody_environment:
        environment = _wsl_environment(custody_environment, token, application_dsn)
        result = subprocess.run(
            manager.command(
                python,
                ["-I", "-B", "-m", "potbot.wsl_core_runtime", "run", "--state", state],
            ),
            check=False,
            env=environment,
        )
    return result.returncode


def prepare_windows_core_runtime(config: "ServiceConfig", token: str) -> WindowsCoreRuntime:
    if os.name != "nt":
        raise CapabilityUnavailableError("Windows core runtime handoff requires Windows")
    from potbot.credentials import resolve_postgres_dsn
    from potbot.syn_release.host import DISTRO, WslOperatorHost, run_host

    root = project_root()
    files = _source_files(root)
    source_sha = _source_digest(root, files)
    if _HEX.fullmatch(source_sha) is None:
        raise AssertionError("source digest is not canonical")
    image_source = _sandbox_image_source(root)
    manager = WslOperatorHost()
    manager.setup()
    _ensure_required_wsl_kernel(manager)

    staging = manager.root / "core-runtime" / source_sha
    bundle = staging / "source.tar.gz"
    _write_bundle(root, files, bundle)
    native = f"/var/lib/potbot/bootstrap/source-{source_sha}.tar.gz"
    _copy_private_file_to_wsl(
        manager,
        bundle,
        native,
        limit=_MAX_SOURCE_BYTES,
    )

    root_command = [
        manager.wsl, "--distribution", DISTRO, "--user", "root", "--exec",
        "/bin/sh", "-c", _ROOT_BOOTSTRAP, "potbot-core",
        native, source_sha, UV_URL, UV_SHA256, COSIGN_URL, COSIGN_SHA256,
    ]
    print("[Potbot startup] WSL bootstrap: preparing the managed Linux runtime", flush=True)
    result = _stream_process(
        root_command,
        timeout=1800,
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

    print(
        "[Potbot startup] WSL qualification: validating launcher, sandbox image and workspace",
        flush=True,
    )
    print(
        f"[Potbot startup] Sandbox image source commit: {image_source[:12]}",
        flush=True,
    )
    prepare = _stream_process(
        [
            manager.wsl, "--distribution", DISTRO, "--user", "root", "--exec",
            python, "-I", "-B", "-m", "potbot.wsl_core_runtime", "prepare",
            "--workspace-id", config.workspace_id,
            "--principal-id", config.principal_id,
            "--source-root", f"/var/lib/potbot/core-source/{source_sha}",
            "--source-sha256", source_sha,
            "--consumer-commit", _head(root),
            "--image-source", image_source,
        ],
        timeout=1800,
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

    application_dsn = resolve_postgres_dsn("application")
    return WindowsCoreRuntime(
        command=_supervisor_command(python, state),
        environment=_runtime_secret_environment(token, application_dsn),
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    supervise = commands.add_parser("supervise")
    supervise.add_argument("--python", required=True)
    supervise.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "supervise":
            return _supervise_windows_core_runtime(args.python, args.state)
    except (CapabilityUnavailableError, StateMismatchError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
