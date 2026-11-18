"""Windows DPAPI custody bridge for the signed Syn 0.11.0 operator.

The official Syn runtime remains installed through the signed release machinery.
Until Syn publishes a Potbot-specific host-custody artifact, Potbot stages only
the host-custody helper files from the exact signed release source commit and
verifies their Git blob identities before using Syn's own locked host project.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from potbot.dependencies import project_root
from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError

SYN_HOST_CUSTODY_VERSION = "0.11.0"
SYN_HOST_CUSTODY_SOURCE_COMMIT = "3ed8a43c7e08480b4e5ce13c9e4720c3001b70dc"
SYN_HOST_CUSTODY_MANIFEST_SHA256 = (
    "bb496a73ebd9d37018f6274a6dc29e4e20f03b96b9ab4e687289dcf6a2ac9a6e"
)
SYN_RELEASE_PUBLIC_KEY = "945786a5587110a061a6b73a02057b327f1ce819db4b0b5850201fbca41149cb"
SYN_RELEASE_KEY_SHA256 = "a47f15e010613c0a839a48f2035b8f71d6f9e46ee3df07ed58487079d56ae216"
UV_VERSION = "0.12.23"

CUSTODY_ENVIRONMENT = (
    "SYN_HOST_CUSTODY_PIPE",
    "SYN_HOST_CUSTODY_CLIENT_WIN",
    "SYN_HOST_CUSTODY_POWERSHELL",
)
REQUIRED_MARKER = "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED"

# Git blob identities from Syn v0.11.0 / source commit above. The signed release
# manifest independently pins that source commit. Runtime download is therefore
# bounded to this exact reviewed tree rather than a branch or "latest" URL.
_SOURCE_FILES: dict[str, tuple[str, int]] = {
    "pyproject.toml": ("42308fba45d4a2244502c673aef8e44f80878f5d", 64 * 1024),
    "uv.lock": ("c1b62d210da32188859a0444419566e45424f821", 2 * 1024 * 1024),
    "pipe-client.ps1": ("146c4f22fe9c5bb624b3aa12e74dd8bc5c78046b", 128 * 1024),
    "syn_host_custody/__init__.py": ("40d9b1144cffe344f4338133d0131274b54094c3", 64 * 1024),
    "syn_host_custody/broker.py": ("3eb38ac4ccc14650cab4ac217c1a15b4ed88deec", 256 * 1024),
    "syn_host_custody/core.py": ("5c11bef1449afac409c1ef075d1199e7ff244685", 256 * 1024),
    "syn_host_custody/merkle.py": ("e4d385993a1687517beba902d68f87f16640800e", 128 * 1024),
    "syn_host_custody/owner.py": ("6de5049d993b581435384d0d4428bd5fb770777f", 128 * 1024),
    "syn_host_custody/peer_record.py": ("e3ca5fc8881846fc35451e74d2a7677b32a2d971", 128 * 1024),
}
_RAW_ROOT = (
    "https://raw.githubusercontent.com/Synsphere/syn/"
    + SYN_HOST_CUSTODY_SOURCE_COMMIT
    + "/host-custody/"
)

RUNNER_SOURCE = r"""# Potbot-owned lifetime wrapper for Syn's Windows DPAPI custody broker.
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from ctypes import wintypes

ENV_NAMES = (
    "SYN_HOST_CUSTODY_PIPE",
    "SYN_HOST_CUSTODY_CLIENT_WIN",
    "SYN_HOST_CUSTODY_POWERSHELL",
    "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED",
)


def creation_filetime() -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    values = [wintypes.FILETIME() for _ in range(4)]
    handle = kernel.GetCurrentProcess()
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def health(powershell: str, client: str, pipe: str) -> bool:
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                client,
                "-Pipe",
                pipe,
            ],
            input='{"op":"health"}\n',
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode or len(result.stdout) > 4096:
            return False
        body = json.loads(result.stdout)
        return (
            body.get("ok") is True
            and body.get("result", {}).get("profile") == "syn.host-custody/1"
            and body.get("result", {}).get("ready") is True
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wsl", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--client", required=True)
    args = parser.parse_args()
    project = Path(args.project).resolve(strict=True)
    client = Path(args.client).resolve(strict=True)
    powershell = str(Path(args.wsl).parent / "WindowsPowerShell/v1.0/powershell.exe")
    pipe = "SynHostCustody-" + secrets.token_hex(16)
    broker = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "syn_host_custody.broker",
            "--pipe",
            pipe,
            "--owner-pid",
            str(os.getpid()),
            "--owner-created",
            str(creation_filetime()),
        ],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        for _ in range(100):
            if broker.poll() is not None:
                return 3
            if health(powershell, str(client), pipe):
                break
            time.sleep(0.1)
        else:
            return 4

        environment = dict(os.environ)
        environment.update(
            {
                "SYN_HOST_CUSTODY_PIPE": pipe,
                "SYN_HOST_CUSTODY_CLIENT_WIN": str(client),
                "SYN_HOST_CUSTODY_POWERSHELL": powershell,
                "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED": "1",
                "WSLENV": (
                    "SYN_HOST_CUSTODY_PIPE/u:"
                    "SYN_HOST_CUSTODY_CLIENT_WIN/u:"
                    "SYN_HOST_CUSTODY_POWERSHELL/pu:"
                    "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED/u"
                ),
            }
        )
        prefix = [
            args.wsl,
            "--distribution",
            "Potbot-SynOperator",
            "--user",
            "synoperator",
            "--exec",
            "/usr/bin/systemctl",
            "--user",
        ]
        imported = subprocess.run(
            [*prefix, "import-environment", *ENV_NAMES],
            env=environment,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if imported.returncode:
            return imported.returncode
        service = subprocess.run(
            [*prefix, "--wait", "restart", "potbot-syn-operator.service"],
            env=environment,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return service.returncode
    finally:
        if broker.poll() is None:
            broker.terminate()
            try:
                broker.wait(timeout=8)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
"""


def require_windows_custody_policy(selected: dict[str, object]) -> None:
    expected = {
        "repository": "Synsphere/syn",
        "version": SYN_HOST_CUSTODY_VERSION,
        "minimum_version": SYN_HOST_CUSTODY_VERSION,
        "source_commit": SYN_HOST_CUSTODY_SOURCE_COMMIT,
        "manifest_sha256": SYN_HOST_CUSTODY_MANIFEST_SHA256,
        "public_key": SYN_RELEASE_PUBLIC_KEY,
        "release_key_sha256": SYN_RELEASE_KEY_SHA256,
        "mode": "operator",
    }
    if any(selected.get(name) != value for name, value in expected.items()):
        raise StateMismatchError(
            "Windows DPAPI operator custody requires the exact signed Syn 0.11.0 policy"
        )


def _git_blob_sha1(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode("ascii") + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def _verify_source(path: Path, relative: str) -> None:
    expected, limit = _SOURCE_FILES[relative]
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise StateMismatchError("Syn host-custody source staging is incomplete or redirected")
    if _git_blob_sha1(path.read_bytes()) != expected:
        raise StateMismatchError("Syn host-custody source differs from the signed release commit")


def _download_source(relative: str) -> bytes:
    expected, limit = _SOURCE_FILES[relative]
    url = _RAW_ROOT + relative
    request = urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "potbot-syn-host-custody/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if (
                response.status != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
                or response.geturl() != url
            ):
                raise CapabilityUnavailableError(
                    "Syn host-custody source download was redirected or encoded unexpectedly"
                )
            data = response.read(limit + 1)
    except OSError as error:
        raise CapabilityUnavailableError(
            "Syn 0.11.0 Windows host-custody source could not be downloaded"
        ) from error
    if not 0 < len(data) <= limit or _git_blob_sha1(data) != expected:
        raise StateMismatchError("Downloaded Syn host-custody source differs from its pinned blob")
    return data


def _stage_source(host_root: Path) -> Path:
    parent = host_root / "host-custody"
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise StateMismatchError("Windows host-custody cache is redirected")
    target = parent / SYN_HOST_CUSTODY_SOURCE_COMMIT
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise StateMismatchError("Windows host-custody source cache is invalid")
        for relative in _SOURCE_FILES:
            _verify_source(target / relative, relative)
        return target

    pending = parent / (".pending-" + uuid4().hex)
    pending.mkdir()
    try:
        for relative in _SOURCE_FILES:
            path = pending / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(_download_source(relative))
        for relative in _SOURCE_FILES:
            _verify_source(pending / relative, relative)
        os.rename(pending, target)
    finally:
        shutil.rmtree(pending, ignore_errors=True)
    return target


def _uv_executable() -> Path:
    candidates: list[Path] = []
    supplied = os.environ.get("POTBOT_UV_EXECUTABLE")
    if supplied:
        candidates.append(Path(supplied))
    candidates.append(project_root() / ".potbot/runtime/uv/uv.exe")
    found = shutil.which("uv")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            selected = candidate.resolve(strict=True)
            result = subprocess.run(
                [str(selected), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip().startswith("uv " + UV_VERSION):
            return selected
    raise CapabilityUnavailableError(
        "Windows DPAPI custody requires Potbot's pinned uv 0.12.23 launcher"
    )


@contextmanager
def _custody_setup_lock(host_root: Path):
    host_root.mkdir(parents=True, exist_ok=True)
    path = host_root / ".host-custody.lock"
    if path.is_symlink():
        raise StateMismatchError("Windows host-custody setup lock is redirected")
    with path.open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
            os.fsync(lock.fileno())
        lock.seek(0)
        try:
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise StateMismatchError("Another Windows host-custody setup is in progress") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def prepare_windows_host_custody(host_root: Path) -> dict[str, Path | str]:
    if os.name != "nt":
        raise CapabilityUnavailableError("Windows host custody preparation requires Windows")
    with _custody_setup_lock(host_root):
        source = _stage_source(host_root)
        uv = _uv_executable()
        environment = dict(os.environ)
        environment["UV_PROJECT_ENVIRONMENT"] = str(source / ".venv")
        result = subprocess.run(
            [
                str(uv),
                "sync",
                "--project",
                str(source),
                "--locked",
                "--no-dev",
                "--python",
                "3.12",
                "--managed-python",
            ],
            env=environment,
            capture_output=True,
            timeout=600,
            check=False,
        )
        if result.returncode:
            raise CapabilityUnavailableError(
                "Syn 0.11.0 Windows host-custody locked environment could not be prepared"
            )
        python = source / ".venv/Scripts/python.exe"
        client = source / "pipe-client.ps1"
        if not python.is_file():
            raise CapabilityUnavailableError("Syn Windows host-custody Python is unavailable")
        _verify_source(client, "pipe-client.ps1")
        runner = source / "potbot-host-custody-runner.py"
        raw = RUNNER_SOURCE.encode("utf-8")
        if runner.exists() or runner.is_symlink():
            if runner.is_symlink() or runner.read_bytes() != raw:
                raise StateMismatchError("Existing Potbot host-custody lifetime runner differs")
        else:
            with runner.open("xb") as output:
                output.write(raw)
    return {
        "profile": "potbot.syn-windows-host-custody/1",
        "version": SYN_HOST_CUSTODY_VERSION,
        "source_commit": SYN_HOST_CUSTODY_SOURCE_COMMIT,
        "project": source,
        "python": python,
        "client": client,
        "runner": runner,
    }


def _process_creation_filetime() -> int:
    if os.name != "nt":
        raise CapabilityUnavailableError("Windows process identity requires Windows")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    values = [wintypes.FILETIME() for _ in range(4)]
    handle = kernel.GetCurrentProcess()
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def _health(powershell: Path, client: Path, pipe: str) -> bool:
    try:
        result = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(client),
                "-Pipe",
                pipe,
            ],
            input='{"op":"health"}\n',
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        body = (
            json.loads(result.stdout)
            if result.returncode == 0 and len(result.stdout) <= 4096
            else {}
        )
        return (
            body.get("ok") is True
            and body.get("result", {}).get("profile") == "syn.host-custody/1"
            and body.get("result", {}).get("ready") is True
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


@contextmanager
def windows_host_custody(manager):
    if os.name != "nt":
        raise CapabilityUnavailableError("Windows host custody requires Windows")
    runtime = prepare_windows_host_custody(manager.root)
    project = Path(runtime["project"])
    python = Path(runtime["python"])
    client = Path(runtime["client"])
    powershell = Path(manager.wsl).parent / "WindowsPowerShell/v1.0/powershell.exe"
    pipe = "SynHostCustody-" + secrets.token_hex(16)
    broker = subprocess.Popen(
        [
            str(python),
            "-B",
            "-m",
            "syn_host_custody.broker",
            "--pipe",
            pipe,
            "--owner-pid",
            str(os.getpid()),
            "--owner-created",
            str(_process_creation_filetime()),
        ],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        for _ in range(100):
            if broker.poll() is not None:
                raise CapabilityUnavailableError(
                    "Syn Windows host-custody broker exited during startup"
                )
            if _health(powershell, client, pipe):
                break
            time.sleep(0.1)
        else:
            raise CapabilityUnavailableError("Syn Windows host-custody broker did not become ready")
        from .host import windows_environment

        environment = windows_environment()
        environment.update(
            {
                "SYN_HOST_CUSTODY_PIPE": pipe,
                "SYN_HOST_CUSTODY_CLIENT_WIN": str(client),
                "SYN_HOST_CUSTODY_POWERSHELL": str(powershell),
                REQUIRED_MARKER: "1",
                "WSLENV": (
                    "SYN_HOST_CUSTODY_PIPE/u:"
                    "SYN_HOST_CUSTODY_CLIENT_WIN/u:"
                    "SYN_HOST_CUSTODY_POWERSHELL/pu:"
                    "POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED/u"
                ),
            }
        )
        yield environment
    finally:
        if broker.poll() is None:
            broker.terminate()
            try:
                broker.wait(timeout=8)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait(timeout=5)
