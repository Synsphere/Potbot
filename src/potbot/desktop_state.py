"""Native desktop readiness projection independent of any X application window."""

from __future__ import annotations

import json
import socket
import urllib.request
from dataclasses import dataclass
from typing import Any

from potbot.credentials import CredentialStore, credential_profile
from potbot.domain.errors import DomainError

WEB_UI_URL = "http://127.0.0.1:3100"
XPRA_HOST = "127.0.0.1"
XPRA_PORT = 8766


@dataclass(frozen=True)
class DesktopStatus:
    configured: bool
    backend: bool
    xpra: bool
    web: bool
    workspace_id: str | None = None

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
            return "Backend online · starting Xpra application services…"
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


def _xpra_ready() -> bool:
    try:
        with socket.create_connection((XPRA_HOST, XPRA_PORT), timeout=0.25):
            return True
    except OSError:
        return False


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
    return DesktopStatus(
        configured=True,
        backend=_backend_ready(config),
        xpra=_xpra_ready(),
        web=_web_ready(),
        workspace_id=config.workspace_id,
    )
