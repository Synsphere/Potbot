"""Mandatory core workspace runtime wiring for the normal Potbot application.

Xpra, terminal/browser applications, the managed operator and the atomic graph
scheduler are core Potbot services.  This module never invents an image digest
or qualification receipt: it reuses the exact already-qualified operator
manifest and fails startup when that runtime is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from potbot.dependencies import project_root
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

if TYPE_CHECKING:
    from potbot.service import ServiceConfig

XPRA_PORT = 8766
UI_ORIGIN = "http://127.0.0.1:3100"
XPRA_HTML5_COMMIT = "3752e59b7bb3120a5f20794ce754b3b76ac96c4b"


def _private_write(path: Path, document: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise InvalidInputError("Core runtime policy path cannot be a symbolic link")
    payload = json.dumps(document, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(path.name + ".pending")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def ensure_xpra_html5(root: Path | None = None) -> Path:
    """Prepare the exact pinned Xpra HTML5 client used by the core GUI."""
    root = project_root() if root is None else root
    output = root / "frontend" / "public" / "xpra-html5"
    revision = output / "UPSTREAM-REVISION"
    if revision.is_file() and revision.read_text("utf-8").strip() == XPRA_HTML5_COMMIT:
        return output
    git = shutil.which("git")
    script = root / "frontend" / "scripts" / "prepare-xpra-html5.py"
    if not git or not script.is_file():
        raise CapabilityUnavailableError("Core Xpra HTML5 client cannot be prepared")
    source = root / ".potbot" / "runtime" / "xpra-html5" / XPRA_HTML5_COMMIT
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        result = subprocess.run(
            [
                git,
                "clone",
                "--no-checkout",
                "https://github.com/Xpra-org/xpra-html5.git",
                str(source),
            ],
            cwd=root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        if result.returncode:
            raise CapabilityUnavailableError("Pinned Xpra HTML5 source download failed")
    checkout = subprocess.run(
        [git, "-C", str(source), "checkout", "--detach", XPRA_HTML5_COMMIT],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if checkout.returncode:
        raise CapabilityUnavailableError("Pinned Xpra HTML5 revision is unavailable")
    prepared = subprocess.run(
        [sys.executable, str(script), "--upstream", str(source), "--output", str(output)],
        cwd=root,
        check=False,
        timeout=120,
    )
    if (
        prepared.returncode
        or not revision.is_file()
        or revision.read_text("utf-8").strip() != XPRA_HTML5_COMMIT
    ):
        raise CapabilityUnavailableError("Pinned Xpra HTML5 preparation failed")
    return output


def prepare_core_runtime(config: ServiceConfig) -> tuple[Path, Path]:
    """Validate the pinned Linux runtime and emit its private Xpra app policy."""
    if not sys.platform.startswith("linux"):
        raise CapabilityUnavailableError(
            "Full Potbot runtime requires its managed Linux/WSL workspace; "
            "native Windows setup must provision that runtime before Potbot is ready"
        )
    if config.operator_manifest is None or not config.scoped_worker_launcher:
        raise CapabilityUnavailableError(
            "Full Potbot runtime requires the qualified operator manifest and scoped worker launcher"
        )
    from potbot.service import service_home
    from potbot.tools.operator_cli import parse_manifest

    selected = parse_manifest(Path(config.operator_manifest))
    if str(selected.workspace_id) != config.workspace_id:
        raise InvalidInputError("Core runtime operator manifest belongs to another workspace")
    for command in ("xpra", "xauth", "bwrap"):
        if shutil.which(command) is None:
            raise CapabilityUnavailableError(f"Core runtime dependency is missing: {command}")
    required_guest = (
        selected.rootfs / "usr/bin/xterm",
        selected.rootfs / "opt/potbot/browser/bin/python",
        selected.rootfs / "opt/potbot/browser_worker.py",
    )
    if not all(path.is_file() and not path.is_symlink() for path in required_guest):
        raise CapabilityUnavailableError(
            "Qualified sandbox image lacks the core terminal/controlled-browser runtime"
        )
    ensure_xpra_html5()
    document: dict[str, object] = {
        "workspaceId": config.workspace_id,
        "principalId": config.principal_id,
        "ledger": str(selected.controller_ledger),
        "runtime": {
            "imageDigest": selected.image_digest,
            "qualificationSha256": selected.qualification_sha256,
            "rootfs": str(selected.rootfs),
            "home": str(selected.home),
            "modelStaging": str(selected.model_staging),
            "sharedModels": str(selected.shared_models),
            "sharedTools": str(selected.shared_tools),
            "aiConfig": str(selected.ai_config_dir),
            "aiPast": str(selected.ai_history_dir),
        },
        "browserLeases": True,
        "strictXpraInput": True,
        "recipes": [
            {
                "kind": "terminal",
                "title": "Bash (xterm)",
                "argv": ["/usr/bin/xterm", "-fa", "Monospace", "-e", "/bin/bash"],
            },
            {
                "kind": "controlled-browser",
                "title": "Controlled Chromium",
                "argv": [
                    "/opt/potbot/browser/bin/python",
                    "/opt/potbot/browser_worker.py",
                    "--engine",
                    "playwright",
                ],
            },
        ],
    }
    return _private_write(service_home() / "xpra-apps.json", document), selected.home


def frontend_bridge_environment(config: ServiceConfig, token: str) -> dict[str, str]:
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or "\n" in token or "\r" in token:
        raise InvalidInputError("Xpra bridge token is invalid")
    return {
        "POTBOT_UI_XPRA_WORKSPACE_ID": config.workspace_id,
        "POTBOT_UI_XPRA_BRIDGE_URL": f"http://127.0.0.1:{XPRA_PORT}/",
        "POTBOT_UI_XPRA_BRIDGE_TOKEN": token,
        "POTBOT_UI_XPRA_PUBLIC_WS_URL": f"ws://127.0.0.1:{XPRA_PORT}/v1/xpra/ws",
    }
