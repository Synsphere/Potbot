"""Native settings API client. No browser, native scanner or administrator DSN.

Connections are selected by the local operator. Every returned view is bounded
and projected before Qt sees it; credentials and upstream diagnostics stay out
of widgets and exception messages.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from potbot.domain.configuration import DEFAULTS
from potbot.domain.remote_workspaces import CONTROL_PERMISSIONS
from potbot.domain.settings_catalogue import CATEGORIES, FEATURES, PREFERENCE_DESCRIPTORS

PATHS = {
    "catalogue": "/v1/ui/settings/catalogue",
    "effective": "/v1/ui/settings/effective",
    "models": "/v1/ui/inventory/models",
    "tools": "/v1/ui/inventory/tools",
    "swarm": "/v1/ui/swarm/nodes",
    "preferences": "/v1/ui/preferences",
    "requests": "/v1/ui/swarm/revocations",
    "live_configuration": "/v1/ui/settings/live",
    "node_controls": "/v1/ui/swarm/control",
}
MAX_RESPONSE = 524288
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class NativeSettingsError(Exception):
    def __init__(self, code: str = "unavailable") -> None:
        self.code = code
        super().__init__(
            {
                "conflict": "Saved state changed. Refresh and review the original change.",
                "scope": "Workspace authorization changed or is unavailable. Reconnect before continuing.",
                "setup": "The native operator setup capability is unavailable.",
                "invalid_response": "The settings service returned an invalid scoped response.",
                "configuration": "Native workspace connections are unavailable or invalid.",
                "journal": "The exact review could not be retained safely. No request was sent.",
                "cancelled": "The previous view was superseded.",
            }.get(code, "The settings service is unavailable. No success has been confirmed.")
        )


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def _text(value: Any, limit: int = 240, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ValueError
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError
    return value


def _uuid(value: Any) -> str:
    text = _text(value, 36)
    identity = UUID(text)
    if str(identity) != text or identity.int == 0:
        raise ValueError
    return text


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value < 2**53:
        raise ValueError
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _choice(value: Any, choices: Any) -> str:
    text = _text(value)
    if text not in choices:
        raise ValueError
    return text


def _hash(value: Any) -> str:
    text = _text(value, 64)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError
    return text


def _date(value: Any) -> str:
    text = _text(value, 48)
    if "T" not in text or datetime.fromisoformat(text).utcoffset() is None:
        raise ValueError
    return text


def _rows(value: Any, maximum: int = 100) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError
    return [_object(row) for row in value]


def _unique(rows: list[dict[str, Any]], key: str = "id") -> list[dict[str, Any]]:
    if len({row[key] for row in rows}) != len(rows):
        raise ValueError
    return rows


def _setting_value(identity: str, value: Any) -> str:
    text = _text(value, 1024, True)
    if identity in {"configuration.secret_refs", "configuration.source_access"} and not (
        re.fullmatch(r"[0-9]{1,10} configured", text) or text == "Not recorded"
    ):
        raise ValueError
    return text


def _values(raw: Any) -> list[dict[str, Any]]:
    known = {"configuration." + name for name in DEFAULTS}
    result = []
    for row in _rows(raw, len(known)):
        identity = _choice(row["id"], known)
        result.append({"id": identity, "value": _setting_value(identity, row["value"])})
    if {row["id"] for row in result} != known:
        raise ValueError
    return _unique(result)


def project_preferences(raw: Any) -> dict[str, Any]:
    from potbot.domain.ui_preferences import validate_preferences

    value = _object(raw)
    return {
        "revision": _integer(value["revision"]),
        "settings": validate_preferences(value["settings"]),
    }


def project_document(
    resource: str, raw: Any, workspace_id: str, principal_id: str | None = None
) -> dict[str, Any]:
    """Allowlist the same versioned API used by Next.js; never render raw JSON."""
    try:
        value = _object(raw)
        if value["schema"] != "potbot-settings/v1":
            raise ValueError
        if _uuid(value["workspaceId"]) != workspace_id:
            raise NativeSettingsError("scope")
        principal = _uuid(value["principalId"])
        if principal_id is not None and principal != principal_id:
            raise NativeSettingsError("scope")
        result: dict[str, Any] = {
            "schema": "potbot-settings/v1",
            "workspaceId": workspace_id,
            "principalId": principal,
        }
        categories = {row[0] for row in CATEGORIES}
        if resource == "catalogue":
            if value["layerEditing"] is not False:
                raise ValueError
            category_rows = [
                {"id": _choice(row["id"], categories), "label": _text(row["label"], 100)}
                for row in _rows(value["categories"], 14)
            ]
            if {row["id"] for row in category_rows} != categories:
                raise ValueError
            features = [
                {
                    "id": _choice(row["id"], {item[0] for item in FEATURES}),
                    "label": _text(row["label"], 100),
                    "category": _choice(row["category"], categories),
                    "description": _text(row["description"], 512),
                }
                for row in _rows(value["features"], 29)
            ]
            if len(_unique(features)) != 29:
                raise ValueError
            expected = {"configuration." + key for key in DEFAULTS} | {
                "preferences." + row[0] for row in PREFERENCE_DESCRIPTORS
            }
            descriptors = []
            for row in _rows(value["descriptors"], 17):
                identity = _choice(row["id"], expected)
                editable = _boolean(row["editable"])
                if editable != identity.startswith("preferences."):
                    raise ValueError
                descriptors.append(
                    {
                        "id": identity,
                        "label": _text(row["label"], 100),
                        "category": _choice(row["category"], categories),
                        "editable": editable,
                        "defaultValue": _setting_value(identity, row["defaultValue"]),
                        "apply": _choice(row["apply"], {"client", "next_admission"}),
                        "owner": _text(row["owner"], 80),
                        "reason": _text(row["reason"], 512),
                    }
                )
            if {row["id"] for row in _unique(descriptors)} != expected:
                raise ValueError
            result.update(
                categories=_unique(category_rows),
                features=features,
                descriptors=descriptors,
                layerEditing=False,
            )
        elif resource == "effective":
            if value["liveLayersAvailable"] is not False:
                raise ValueError
            snapshot = value["latestRecordedSnapshot"]
            result.update(
                defaults=_values(value["defaults"]),
                liveLayersAvailable=False,
                latestRecordedSnapshot=None
                if snapshot is None
                else {
                    "id": _uuid(snapshot["id"]),
                    "sha256": _hash(snapshot["sha256"]),
                    "recordedAt": _date(snapshot["recordedAt"]),
                    "values": _values(snapshot["values"]),
                },
            )
        elif resource in {"models", "tools"}:
            if (
                value["kind"] != resource
                or value["runtimeProbed"] is not False
                or value["permissionEvaluated"] is not False
            ):
                raise ValueError
            local = _object(value["local"])
            result.update(
                kind=resource,
                observedAt=_date(value["observedAt"]),
                local={
                    "status": _choice(local["status"], {"not_configured", "observed", "partial"}),
                    "nodeLabel": _text(local["nodeLabel"], 120),
                    "profileSha256": None
                    if local["profileSha256"] is None
                    else _hash(local["profileSha256"]),
                    "truncated": _boolean(local["truncated"]),
                    "unavailableRoots": _integer(local["unavailableRoots"]),
                },
                recordsTruncated=_boolean(value["recordsTruncated"]),
                runtimeProbed=False,
                permissionEvaluated=False,
            )
            items = []
            for row in _rows(value["items"], 200):
                origin = _choice(
                    row["origin"],
                    {"local_file", "partial_file", "deployment_record"}
                    if resource == "models"
                    else {"package_metadata", "tool_release_record"},
                )
                native = origin in {"local_file", "partial_file", "package_metadata"}
                identity = _text(row["id"], 80)
                presence = _choice(row["presence"], {"observed", "partial", "not_observed"})
                qualification = _choice(
                    row["qualification"], {"unknown", "qualified", "unqualified"}
                )
                runtime = _choice(row["runtime"], {"unknown", "healthy", "degraded", "unavailable"})
                location = _text(row["location"], 512)
                if row["verification"] != "not_checked" or row["permission"] != "not_evaluated":
                    raise ValueError
                if native:
                    if (
                        not re.fullmatch(r"local:[0-9a-f]{64}", identity)
                        or presence != ("partial" if origin == "partial_file" else "observed")
                        or qualification != "unknown"
                        or runtime != "unknown"
                        or location.startswith("/")
                        or "\\" in location
                        or ":" in location
                        or any(part in {"", ".", ".."} for part in location.split("/"))
                    ):
                        raise ValueError
                elif (
                    _uuid(identity) != identity
                    or presence != "not_observed"
                    or row["bytes"] is not None
                    or location
                    != (
                        "Deployment registry"
                        if origin == "deployment_record"
                        else "Tool acquisition ledger"
                    )
                ):
                    raise ValueError
                items.append(
                    {
                        "id": identity,
                        "name": _text(row["name"]),
                        "version": _text(row["version"], 100, True),
                        "origin": origin,
                        "location": location,
                        "bytes": None if row["bytes"] is None else _integer(row["bytes"]),
                        "presence": presence,
                        "qualification": qualification,
                        "runtime": runtime,
                        "verification": "not_checked",
                        "permission": "not_evaluated",
                        "detail": _text(row["detail"], 512),
                    }
                )
            result["items"] = _unique(items)
        elif resource == "swarm":
            for key in (
                "liveCapacityAvailable",
                "workerTelemetryAvailable",
                "nativeNodeActionsAvailable",
            ):
                if value[key] is not False:
                    raise ValueError
                result[key] = False
            nodes = []
            for row in _rows(value["nodes"]):
                if (
                    row["state"] != "registered"
                    or row["runtimeQualified"] is not False
                    or row["lastAuthenticatedContact"] is not None
                ):
                    raise ValueError
                pin = _text(row["sshFingerprint"], 60)
                if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", pin):
                    raise ValueError
                nodes.append(
                    {
                        "id": _uuid(row["id"]),
                        "label": _text(row["label"], 253),
                        "generation": _integer(row["generation"], 1),
                        "state": "registered",
                        "sshFingerprint": pin,
                        "registeredAt": _date(row["registeredAt"]),
                        "runtimeQualified": False,
                        "lastAuthenticatedContact": None,
                    }
                )
            grants = []
            for row in _rows(value["grants"]):
                permissions = row["permissions"]
                if (
                    not isinstance(permissions, list)
                    or not permissions
                    or len(set(permissions)) != len(permissions)
                    or not set(permissions) <= CONTROL_PERMISSIONS
                ):
                    raise ValueError
                state = _choice(row["state"], {"active", "expired", "revoked"})
                can_request = _boolean(row["canRequestRevocation"])
                if state == "revoked" and can_request:
                    raise ValueError
                grants.append(
                    {
                        "id": _uuid(row["id"]),
                        "sourceWorkspaceId": _uuid(row["sourceWorkspaceId"]),
                        "revision": _integer(row["revision"], 1),
                        "revocationGeneration": _integer(row["revocationGeneration"], 1),
                        "sha256": _hash(row["sha256"]),
                        "permissions": permissions,
                        "expiresAt": _date(row["expiresAt"]),
                        "state": state,
                        "canRequestRevocation": can_request,
                    }
                )
            deployments = []
            for row in _rows(value["deployments"]):
                if (
                    row["workspaceId"] != workspace_id
                    or row["executionMode"] != "base_os"
                    or row["qualification"] != "unqualified"
                ):
                    raise ValueError
                deployments.append(
                    {
                        "workspaceId": workspace_id,
                        "nodeId": _uuid(row["nodeId"]),
                        "revision": _integer(row["revision"], 1),
                        "platform": _text(row["platform"]),
                        "executionMode": "base_os",
                        "qualification": "unqualified",
                    }
                )
            requests = []
            for row in _rows(value["requests"]):
                state = _choice(row["state"], {"requested", "applied", "needs_review"})
                grant = None if row["grantId"] is None else _uuid(row["grantId"])
                revision = (
                    None
                    if row["expectedRevision"] is None
                    else _integer(row["expectedRevision"], 1)
                )
                if state != "needs_review" and (grant is None or revision is None):
                    raise ValueError
                requests.append(
                    {
                        "requestId": _uuid(row["requestId"]),
                        "grantId": grant,
                        "expectedRevision": revision,
                        "state": state,
                        "recordedAt": _date(row["recordedAt"]),
                    }
                )
            result.update(
                nodes=_unique(nodes),
                grants=_unique(grants),
                deployments=deployments,
                requests=_unique(requests, "requestId"),
                observedAt=_date(value["observedAt"]),
                recordsTruncated=_boolean(value["recordsTruncated"]),
            )
        elif resource == "requests":
            if value["state"] != "requested":
                raise ValueError
            result.update(requestId=_uuid(value["requestId"]), state="requested")
        else:
            raise ValueError
        return result
    except NativeSettingsError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise NativeSettingsError("invalid_response") from None


def _base(value: str) -> str:
    parts = urllib.parse.urlsplit(_text(value, 512))
    if (
        parts.scheme != "http"
        or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
        or not parts.port
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError
    return parts.scheme + "://" + parts.netloc


def _token(value: Any) -> str:
    text = _text(value, 4096)
    if len(text) < 32 or not re.fullmatch(r"[!-~]+", text):
        raise ValueError
    return text


@dataclass(frozen=True)
class NativeConnection:
    workspace_id: str
    name: str
    base: str
    token: str = field(repr=False)
    origin: str = "http://127.0.0.1:3100"
    principal_id: str | None = None
    setup: Callable[[], str | None] = field(default=lambda: None, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            _uuid(self.workspace_id)
            _text(self.name, 80)
            if _base(self.base) != self.base or _base(self.origin) != self.origin:
                raise ValueError
            _token(self.token)
            if self.principal_id is not None:
                _uuid(self.principal_id)
        except (ValueError, TypeError, AttributeError):
            raise NativeSettingsError("configuration") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def http_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            if response.headers.get_content_type() != "application/json":
                raise NativeSettingsError("invalid_response")
            raw = bytearray()
            deadline = time.monotonic() + 10
            while True:
                chunk = response.read(min(16384, MAX_RESPONSE + 1 - len(raw)))
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE or time.monotonic() > deadline:
                    raise NativeSettingsError("invalid_response")
                if not chunk:
                    break
            return int(response.status), bytes(raw)
    except urllib.error.HTTPError as error:
        # Never read, reflect or log the potentially secret-bearing error body.
        status = error.code
        error.close()
        return status, b""
    except (OSError, ValueError, urllib.error.URLError):
        raise NativeSettingsError() from None


def _json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    if len(raw) > MAX_RESPONSE:
        raise ValueError
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )


class NativeSettingsClient:
    def __init__(self, connection: NativeConnection, transport: Transport = http_transport) -> None:
        self.connection = connection
        self.transport = transport

    def _call(self, resource: str, method: str = "GET", body: Any = None) -> Any:
        headers = {
            "Authorization": "Bearer " + self.connection.token,
            "Accept": "application/json",
            "X-Potbot-Workspace": self.connection.workspace_id,
        }
        encoded = None
        if method != "GET":
            headers.update({"Origin": self.connection.origin, "Content-Type": "application/json"})
            encoded = json.dumps(body, sort_keys=True, allow_nan=False).encode()
            if len(encoded) > (32768 if resource == "live_configuration" else 4096):
                raise NativeSettingsError("configuration")
        if method != "GET" and resource in {"requests", "live_configuration", "node_controls"}:
            try:
                headers["X-Potbot-Setup-Token"] = _token(self.connection.setup())
            except Exception:
                raise NativeSettingsError("setup") from None
        status, raw = self.transport(
            method, self.connection.base + PATHS[resource], headers, encoded
        )
        if status != (
            202
            if resource == "requests" or resource == "node_controls" and method == "POST"
            else 200
        ):
            raise NativeSettingsError(
                "conflict" if status == 409 else "scope" if status in {401, 403} else "unavailable"
            )
        try:
            return _json(raw)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise NativeSettingsError("invalid_response") from None

    def read(self, resource: str, principal_id: str | None = None) -> dict[str, Any]:
        if resource not in PATHS or resource == "requests":
            raise NativeSettingsError("configuration")
        if resource == "preferences":
            self.read("catalogue", principal_id)
        raw = self._call(resource)
        if resource == "preferences":
            try:
                return project_preferences(raw)
            except Exception:
                raise NativeSettingsError("invalid_response") from None
        return project_document(
            resource,
            raw,
            self.connection.workspace_id,
            principal_id or self.connection.principal_id,
        )

    def read_live(self, resource: str, principal_id: str | None = None) -> dict[str, Any]:
        from potbot.desktop_live_settings import project_live

        if resource not in {"configuration", "nodes"}:
            raise NativeSettingsError("configuration")
        actor = self.read("catalogue", principal_id)["principalId"]
        path = "live_configuration" if resource == "configuration" else "node_controls"
        return project_live(resource, self._call(path), self.connection.workspace_id, actor)

    def write_live(self, resource: str, principal_id: str, review: Any) -> dict[str, Any]:
        from potbot.desktop_live_settings import project_live
        from potbot.domain.live_controls import configuration_review, node_review

        if resource not in {"configuration", "nodes"}:
            raise NativeSettingsError("configuration")
        accepted = (
            configuration_review(review) if resource == "configuration" else node_review(review)
        )
        self.read("catalogue", principal_id)
        result = project_live(
            "configuration_saved" if resource == "configuration" else "node_requested",
            self._call(
                "live_configuration" if resource == "configuration" else "node_controls",
                "PUT" if resource == "configuration" else "POST",
                accepted,
            ),
            self.connection.workspace_id,
            principal_id,
        )
        if resource == "configuration":
            if (
                result["requestId"] != accepted["requestId"]
                or result["revision"] != accepted["expectedRevision"] + 1
                or result["values"] != accepted["values"]
            ):
                raise NativeSettingsError("invalid_response")
        elif result["review"] != accepted:
            raise NativeSettingsError("invalid_response")
        return result

    def save_preferences(self, principal_id: str, revision: int, settings: Any) -> dict[str, Any]:
        self.read("catalogue", principal_id)
        try:
            body = project_preferences({"revision": revision, "settings": settings})
            result = project_preferences(self._call("preferences", "PUT", body))
            if result["revision"] != revision + 1 or result["settings"] != body["settings"]:
                raise ValueError
            return result
        except NativeSettingsError:
            raise
        except Exception:
            raise NativeSettingsError("invalid_response") from None

    def request_revocation(self, principal_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from potbot.desktop_settings_state import validate_review

        body = validate_review(body)
        self.read("catalogue", principal_id)
        result = project_document(
            "requests",
            self._call("requests", "POST", body),
            self.connection.workspace_id,
            principal_id,
        )
        if result["requestId"] != body["requestId"]:
            raise NativeSettingsError("invalid_response")
        return result


def load_native_clients(environment: Mapping[str, str] | None = None) -> list[NativeSettingsClient]:
    """Reuse the installed service scope/vault and optional existing web allowlist."""
    from potbot.credentials import CredentialStore
    from potbot.service import ServiceConfig, config_path

    env = os.environ if environment is None else environment
    try:
        clients = []
        path = config_path()
        if path.is_file():
            config = ServiceConfig.load(path)
            store = CredentialStore(config.credential_profile)
            host = "[" + config.host + "]" if ":" in config.host else config.host
            base = _base(f"http://{host}:{config.port}")
            origin = next((item for item in config.allowed_origins if _base(item) == item), base)
            clients.append(
                NativeSettingsClient(
                    NativeConnection(
                        config.workspace_id,
                        "Local workspace",
                        base,
                        _token(store.get("api.access_token")),
                        origin,
                        config.principal_id,
                        lambda: store.get("api.setup_token"),
                    )
                )
            )
        raw = env.get("POTBOT_UI_WORKSPACES_JSON")
        if raw:
            if len(raw) > 30000:
                raise ValueError
            entries = _json(raw.encode())
            if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
                raise ValueError
            seen = set()
            for entry in entries:
                row = _object(entry)
                if set(row) - {"id", "name", "url", "token", "model"}:
                    raise ValueError
                identity = _uuid(row["id"])
                if identity in seen:
                    raise ValueError
                seen.add(identity)
                candidate = NativeConnection(
                    identity,
                    _text(row["name"], 80),
                    _base(row["url"]),
                    _token(row["token"]),
                    _base(env.get("POTBOT_UI_PUBLIC_ORIGIN", "http://127.0.0.1:3100")),
                    setup=lambda: env.get("POTBOT_UI_SETUP_TOKEN"),
                )
                existing = next(
                    (item for item in clients if item.connection.workspace_id == identity), None
                )
                if existing:
                    if (
                        existing.connection.base != candidate.base
                        or existing.connection.token != candidate.token
                    ):
                        raise ValueError
                else:
                    clients.append(NativeSettingsClient(candidate))
        if not 1 <= len(clients) <= 12:
            raise ValueError
        return clients
    except Exception:
        raise NativeSettingsError("configuration") from None
