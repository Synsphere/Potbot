"""Native desktop readiness projection independent of any X application window."""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from potbot.credentials import CredentialStore, credential_profile
from potbot.domain.errors import DomainError

WEB_UI_URL = "http://127.0.0.1:3100"
XPRA_HOST = "127.0.0.1"
XPRA_PORT = 8766
_XPRA_MESSAGES = {
    "custody-quiesce": "Retiring obsolete managed operator services",
    "custody-prepare": "Preparing Windows keystore authority",
    "custody-network": "Checking managed WSL network identity",
    "custody-authority": "Launching Windows keystore authority",
    "custody-firewall": "Checking Windows Firewall for the WSL keystore connection",
    "custody-firewall-admin": "Approve the Windows administrator prompt to repair the firewall",
    "custody-firewall-declined": "Firewall repair was declined; restart Potbot to try again",
    "custody-firewall-failed": "Windows Firewall repair failed; inspect the custody log",
    "custody-tcp": "Connecting WSL to Windows keystore",
    "custody-auth": "Authenticating the parent keystore connection",
    "custody-tcp-failed": "WSL cannot reach Windows keystore; inspect custody log",
    "custody-auth-failed": "Parent keystore authentication/health check failed",
    "custody-ready": "Windows keystore connection verified",
    "database-prepare": "Preparing the operator connection to the Windows database",
    "database-firewall": "Checking Windows Firewall for the WSL database connection",
    "database-firewall-admin": "Approve the Windows administrator prompt for the database connection",
    "database-firewall-declined": "Database firewall repair was declined; restart Potbot to try again",
    "database-firewall-failed": "Database firewall repair failed; inspect the supervisor log",
    "database-health": "Checking the application database login from WSL",
    "database-ready": "Application database connection verified from WSL",
    "database-failed": "WSL application database connection failed; inspect the supervisor log",
    "wsl-launch": "Launching managed WSL runtime",
    "secret-service": "Starting parent-backed Secret Service",
    "operator-starting": "Starting managed operator",
    "operator-health": "Waiting for managed operator health",
    "xpra-starting": "Starting Xpra bridge",
    "xpra-wsl-ready": "Xpra ready in WSL",
    "host-bridge-unreachable": "Xpra is ready in WSL but unreachable from Windows",
    "runtime-failed": "Managed WSL runtime failed; inspect supervisor log",
    "restart-limit": "Xpra restart limit reached; restart Potbot after inspecting logs",
    "startup-timeout": "Xpra startup timed out; inspect supervisor log",
    "stopped": "Xpra supervisor stopped",
}


@dataclass(frozen=True)
class DesktopStatus:
    configured: bool
    backend: bool
    xpra: bool
    web: bool
    workspace_id: str | None = None
    xpra_detail: str = ""
    xpra_failed: bool = False

    @property
    def workspace_renderable(self) -> bool:
        return True

    @property
    def banner(self) -> str:
        if not self.configured:
            return "Preparing Potbot workspace configuration…"
        if not self.backend:
            return "Starting Potbot backend and managed WSL…"
        if not self.xpra:
            return self.xpra_detail or "Backend online · starting Xpra application services…"
        if not self.web:
            return "Local workspace ready · remote web interface is starting…"
        return "Potbot is ready"


def _read_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=0.8) as response:
            raw = response.read(8193)
            if response.status != 200 or len(raw) > 8192:
                return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _backend_ready(config: Any) -> bool:
    try:
        token = CredentialStore(credential_profile()).get("api.access_token")
        if not token:
            return False
        host = f"[{config.host}]" if ":" in config.host else config.host
        document = _read_json(
            f"http://{host}:{config.port}/v1/ui/workspace",
            headers={"Authorization": "Bearer " + token},
        )
        workspace = document.get("workspace") if document else None
        return isinstance(workspace, dict) and workspace.get("id") == config.workspace_id
    except DomainError:
        return False


def _xpra_status(path: Path, workspace: str) -> tuple[bool, str, bool]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        document = json.loads(raw) if len(raw) <= 8192 else None
        if not isinstance(document, dict) or document.get("workspace") != workspace:
            return False, "", False
        age = time.time() - float(document["updated"])
        if not 0 <= age <= 20:
            return False, "Xpra supervisor status is stale; inspect supervisor log", True
        stage = document.get("stage")
        if stage == "ready" and document.get("ready") is True:
            return True, "", False
        detail = _XPRA_MESSAGES.get(str(stage), "")
        failed = str(stage).endswith("-failed") or stage in {
            "restart-limit", "startup-timeout", "stopped", "host-bridge-unreachable",
            "custody-firewall-declined",
            "database-firewall-declined",
        }
        return False, detail, failed
    except (OSError, ValueError, KeyError, TypeError):
        return False, "", False


def _web_ready() -> bool:
    document = _read_json(WEB_UI_URL + "/api/ui/auth/status")
    return bool(document and document.get("configured") is True)


def probe_status() -> DesktopStatus:
    from potbot import service

    path = service.config_path()
    if not path.is_file():
        return DesktopStatus(False, False, False, False)
    try:
        config = service.ServiceConfig.load(path)
    except DomainError:
        return DesktopStatus(False, False, False, False)
    xpra, detail, failed = _xpra_status(
        service.service_home() / "xpra-status.json", config.workspace_id
    )
    return DesktopStatus(
        configured=True,
        backend=_backend_ready(config),
        xpra=xpra,
        web=_web_ready(),
        workspace_id=config.workspace_id,
        xpra_detail=detail,
        xpra_failed=failed,
    )
