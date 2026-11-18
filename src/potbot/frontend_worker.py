"""Install the pinned Next.js UI and serve it under native Potbot supervision.

No frontend credentials are written into .env.local or into a command line: they
are copied from the current user's native credential vault into child memory.
The Python supervisor owns the Node process. It is not an alternative tool/agent
worker and grants no capabilities to its browser clients.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from potbot.credentials import CredentialStore, credential_profile
from potbot.dependencies import project_root
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
from potbot.service import ServiceConfig, config_path

_NODE_VERSION = "22.16.0"
_NODE_ROOT = "https://nodejs.org/dist/v" + _NODE_VERSION + "/"
_MAX_DOWNLOAD = 100 * 1024 * 1024
_MAX_EXTRACTED = 600 * 1024 * 1024


def _safe_environment() -> dict[str, str]:
    """Do not expose database/admin or provider secrets to npm install scripts."""
    env = dict(os.environ)
    for name in list(env):
        if (
            name in {"POTBOT_DSN", "POTBOT_ADMIN_DSN", "GH_TOKEN", "GITHUB_TOKEN",
                     "HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "AZURE_CLIENT_SECRET"}
            or name.startswith("POTBOT_UI_")
            or name.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
        ):
            env.pop(name, None)
    return env


def _report(message: str) -> None:
    print("[Potbot GUI] " + message, file=sys.stderr, flush=True)


def _checked_version(executable: Path) -> bool:
    try:
        result = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True,
            timeout=10, check=False,
        )
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
        return bool(match and result.returncode == 0 and (int(match[1]), int(match[2])) >= (20, 9))
    except (OSError, subprocess.TimeoutExpired):
        return False


def _node_release_name() -> str:
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"aarch64", "arm64"} else "x64" if machine in {
        "x86_64", "amd64"
    } else None
    if architecture is None:
        raise CapabilityUnavailableError("No verified Node.js build for this CPU architecture")
    target = "win" if os.name == "nt" else "darwin" if sys.platform == "darwin" else (
        "linux" if sys.platform.startswith("linux") else None
    )
    if target is None:
        raise CapabilityUnavailableError("This system has no supported GUI Node.js build")
    extension = "zip" if target == "win" else "tar.gz" if target == "darwin" else "tar.xz"
    return f"node-v{_NODE_VERSION}-{target}-{architecture}.{extension}"


def _download(url: str, *, maximum: int) -> bytes:
    if not url.startswith(_NODE_ROOT) or not url.startswith("https://nodejs.org/"):
        raise InvalidInputError("Unexpected Node.js download host")
    req = urllib.request.Request(url, headers={"User-Agent": "potbot-ui-bootstrap/1"})
    with urllib.request.urlopen(req, timeout=35) as response:
        if response.geturl() != url:
            raise CapabilityUnavailableError("Node.js download redirected away from pinned URL")
        data = response.read(maximum + 1)
    if not data or len(data) > maximum:
        raise CapabilityUnavailableError("Node.js download exceeded the permitted size")
    return data


def _extract_release(archive: Path, target: Path, release: str) -> None:
    prefix = release.split(".tar", 1)[0].removesuffix(".zip") + "/"
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            total = 0
            for member in source.infolist():
                total += member.file_size
                if (
                    not member.filename.startswith(prefix)
                    or ".." in Path(member.filename).parts
                    or member.filename.startswith("/")
                    or (member.external_attr >> 16) & 0o170000 == 0o120000
                    or total > _MAX_EXTRACTED
                ):
                    raise CapabilityUnavailableError("Node.js archive has an unsafe entry")
            source.extractall(target)
    else:
        # Python 3.12's data filter disallows absolute paths, outside links,
        # device nodes and archive metadata that would escape the staging root.
        with tarfile.open(archive, "r:*") as source:
            members = source.getmembers()
            if (
                sum(member.size for member in members) > _MAX_EXTRACTED
                or any(not member.name.startswith(prefix) for member in members)
            ):
                raise CapabilityUnavailableError("Node.js archive has an unexpected layout")
            source.extractall(target, filter="data")


def ensure_node(root: Path | None = None) -> tuple[Path, Path, dict[str, str]]:
    """Use an existing supported Node/npm or acquire a pinned TLS+SHA256 release."""
    root = project_root() if root is None else root
    for executable in (shutil.which("node"),):
        if executable and _checked_version(Path(executable)):
            npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
            if npm:
                return Path(executable), Path(npm), _safe_environment()
    release = _node_release_name()
    location = root / ".potbot" / "runtime" / "node" / _NODE_VERSION
    binary = location / ("node.exe" if os.name == "nt" else "bin/node")
    npm = location / ("npm.cmd" if os.name == "nt" else "bin/npm")
    env = _safe_environment()
    env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
    if _checked_version(binary) and npm.exists():
        return binary, npm, env
    _report("Installing verified Node.js " + _NODE_VERSION)
    listing = _download(_NODE_ROOT + "SHASUMS256.txt", maximum=100_000).decode("ascii")
    matches = [line.split() for line in listing.splitlines() if line.endswith("  " + release)]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0][0]):
        raise CapabilityUnavailableError("Pinned Node.js checksum is unavailable")
    expected = matches[0][0]
    location.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="potbot-node-", dir=location.parent) as staging:
        temporary = Path(staging)
        data = _download(_NODE_ROOT + release, maximum=_MAX_DOWNLOAD)
        if hashlib.sha256(data).hexdigest() != expected:
            raise CapabilityUnavailableError("Node.js release does not match its checksum")
        payload = temporary / release
        payload.write_bytes(data)
        _extract_release(payload, temporary, release)
        extracted = temporary / release.removesuffix(".zip").split(".tar", 1)[0]
        if not _checked_version(extracted / ("node.exe" if os.name == "nt" else "bin/node")):
            raise CapabilityUnavailableError("Downloaded Node.js executable failed its version check")
        if location.exists():
            # A concurrent installer may have completed; never overlay a live tree.
            if _checked_version(binary) and npm.exists():
                return binary, npm, env
            raise CapabilityUnavailableError("Incomplete Node.js installation requires manual repair")
        extracted.rename(location)
    if not npm.exists():
        raise CapabilityUnavailableError("Node.js installation lacks npm")
    return binary, npm, env


def _frontend_fingerprint(frontend: Path) -> str:
    h = hashlib.sha256()
    seen = 0
    for path in sorted(frontend.rglob("*")):
        if {"node_modules", ".next", ".git", ".potbot"} & set(path.relative_to(frontend).parts):
            continue
        if path.is_symlink():
            raise InvalidInputError("Frontend source contains an unexpected symbolic link")
        if not path.is_file() or path.name == ".env.local":
            continue
        if path.suffix not in {".ts", ".tsx", ".json", ".js", ".mjs", ".css", ".html", ".svg"}:
            continue
        seen += 1
        if seen > 20_000:
            raise InvalidInputError("Frontend source inventory exceeds the supported limit")
        h.update(str(path.relative_to(frontend)).encode("utf-8") + b"\0")
        with path.open("rb") as reader:
            for chunk in iter(lambda: reader.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _run_npm(npm: Path, args: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    # Windows .cmd is explicitly invoked through cmd.exe, not shell=True with
    # caller-controlled text. Only fixed npm subcommands reach this boundary.
    command = (["cmd.exe", "/d", "/s", "/c", str(npm)] if os.name == "nt" else [str(npm)]) + args
    result = subprocess.run(command, cwd=cwd, env=env, check=False)
    if result.returncode:
        raise CapabilityUnavailableError("GUI dependency installation/build failed; inspect the log")


def prepare_frontend(root: Path | None = None) -> Path:
    root = project_root() if root is None else root
    frontend = root / "frontend"
    if not (frontend / "package-lock.json").is_file():
        raise CapabilityUnavailableError("Potbot frontend source or npm lockfile is missing")
    _node, npm, env = ensure_node(root)
    marker = root / ".potbot" / "service" / "frontend-build.json"
    fingerprint = _frontend_fingerprint(frontend)
    try:
        previous: Any = json.loads(marker.read_text("utf-8"))
    except (OSError, ValueError):
        previous = {}
    if (
        previous.get("source_sha256") == fingerprint
        and (frontend / "node_modules" / "next").is_dir()
        and (frontend / ".next" / "BUILD_ID").is_file()
    ):
        return frontend
    _report("Installing pinned UI packages (first start or source update)")
    _run_npm(npm, ["ci", "--no-audit", "--no-fund"], cwd=frontend, env=env)
    _report("Building the Potbot GUI")
    _run_npm(npm, ["run", "build"], cwd=frontend, env=env)
    if not (frontend / ".next" / "BUILD_ID").is_file():
        raise CapabilityUnavailableError("The GUI build did not produce a production output")
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".pending")
    temporary.write_text(json.dumps({"source_sha256": fingerprint}), encoding="utf-8")
    os.replace(temporary, marker)
    return frontend


def frontend_environment(config: ServiceConfig, store: CredentialStore | None = None) -> dict[str, str]:
    store = store or CredentialStore(credential_profile())
    access = store.get("api.access_token")
    password = store.get("ui.password")
    secret = store.get("ui.cookie_secret")
    if not all((access, password, secret)):
        raise CapabilityUnavailableError("The GUI's native credential setup is incomplete")
    env = _safe_environment()
    host = f"[{config.host}]" if ":" in config.host else config.host
    env.update({
        "POTBOT_UI_PUBLIC_ORIGIN": "http://127.0.0.1:3100",
        "POTBOT_UI_PASSWORD": password,
        "POTBOT_UI_COOKIE_SECRET": secret,
        "POTBOT_UI_BACKEND_URL": f"http://{host}:{config.port}",
        "POTBOT_UI_BACKEND_TOKEN": access,
        "POTBOT_UI_WORKSPACE_ID": config.workspace_id,
        "POTBOT_UI_WORKSPACE_NAME": "Potbot",
        "POTBOT_UI_MODEL": "potbot",
    })
    env.pop("POTBOT_UI_SETUP_TOKEN", None)
    setup = store.get("api.setup_token")
    if setup:
        env["POTBOT_UI_SETUP_TOKEN"] = setup
    # Do not let legacy .env.local or a previous workspace configuration select
    # a different set of backend credentials for this supervised single workspace.
    env.pop("POTBOT_UI_WORKSPACES_JSON", None)
    return env


def run_frontend(config: ServiceConfig) -> int:
    root = project_root()
    frontend = prepare_frontend(root)
    node, _npm, environment = ensure_node(root)
    environment.update(frontend_environment(config))
    entry = frontend / "node_modules" / "next" / "dist" / "bin" / "next"
    if not entry.is_file():
        raise CapabilityUnavailableError("Next.js executable is missing after the GUI build")
    process = subprocess.Popen(
        [str(node), str(entry), "start", "--hostname", "127.0.0.1", "--port", "3100"],
        cwd=frontend, env=environment,
    )

    def stop(_number: int, _frame: Any) -> None:
        if process.poll() is None:
            process.terminate()

    for key in ("SIGTERM", "SIGINT"):
        if hasattr(signal, key):
            signal.signal(getattr(signal, key), stop)
    try:
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    try:
        config = ServiceConfig.load(config_path())
        if not config.frontend_enabled:
            raise CapabilityUnavailableError("The installed service has not enabled the GUI")
        return run_frontend(config)
    except (CapabilityUnavailableError, InvalidInputError, OSError) as error:
        _report(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
