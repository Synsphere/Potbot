"""Scoped, signed live catalogue evidence for existing installed MCP owners.

Only explicit native reconciliation performs provider/process work. Public
reads consume bounded retained data; references do not create tool authority.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from potbot.context.service import native_skill_setup_token
from potbot.domain.auth import WorkspaceContext
from potbot.domain.discovery import text_value
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database, idempotent_command
from potbot.tools.mcp_discovery import McpDiscoveryCatalog
from potbot.tools.mcp_gateway import InstalledMcpTool
from potbot.tools.mcp_protocol import McpToolInfo

SCHEMA = "potbot-mcp-discovery/v1"
REFERENCE_SCHEMA = "potbot-mcp-reference/v1"
PAGE_BYTES = 1_572_864
MAX_BINDINGS = 16
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TABLES = {"binding": "mcp_discovery_bindings", "attempt": "mcp_discovery_attempts"}
_KINDS = {
    "tool": "tools",
    "resource": "resources",
    "resource_template": "resourceTemplates",
    "prompt": "prompts",
}
_FAILURES = frozenset(
    {
        "reconciliation_failed",
        "reconciliation_indeterminate",
        "reconciliation_cancelled",
        "catalog_changed",
        "runtime_changed",
        "authority_changed",
        "authentication_required",
        "unsupported_watch",
    }
)
_DENIED = "The MCP discovery record is unavailable in this scope"


def _uuid(value: Any) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("A live MCP discovery UUID is required")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("An exact lowercase SHA-256 is required")
    return value


def _key(context: WorkspaceContext, key: Any) -> str:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise InvalidInputError("A bounded stable MCP command key is required")
    return content_hash([str(context.principal_id), key])


def _token_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 4096
        and all(33 <= ord(c) <= 126 for c in value)
    )


def _body(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    value = {
        "workspaceId": str(row["workspace_id"]),
        "principalId": str(row["principal_id"]),
        "id": str(row["id"]),
        "revision": row["revision"],
        "state": row["state"],
    }
    if kind == "binding":
        value.update(
            config=row["config"],
            snapshotId=str(row["snapshot_id"]) if row["snapshot_id"] else None,
            activeAttemptId=str(row["active_attempt_id"]) if row["active_attempt_id"] else None,
            lastAttemptId=str(row["last_attempt_id"]) if row["last_attempt_id"] else None,
            lastErrorCode=row["last_error_code"],
        )
    else:
        value.update(
            bindingId=str(row["binding_id"]),
            agentId=str(row["agent_id"]),
            payload=row["payload"],
            workerToken=str(row["worker_token"]) if row["worker_token"] else None,
        )
    return value


def _mac(token: str, kind: str, value: Any) -> str:
    return hmac.new(
        token.encode(), canonical_json([SCHEMA, kind, value]).encode(), hashlib.sha256
    ).hexdigest()


def _snapshot_body(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspaceId": str(row["workspace_id"]),
        "principalId": str(row["principal_id"]),
        "id": str(row["id"]),
        "bindingId": str(row["binding_id"]),
        "attemptId": str(row["attempt_id"]),
        "configSha256": row["config_sha256"],
        "sha256": row["sha256"],
        "observation": row["observation"],
        "fetchedAt": row["fetched_at"].isoformat(),
        "expiresAt": row["expires_at"].isoformat(),
    }


def installed_identity(installed: InstalledMcpTool, context: WorkspaceContext) -> dict[str, Any]:
    """Copy existing owner identities; never reconstruct an owner from DB data."""
    if (
        not isinstance(installed, InstalledMcpTool)
        or installed.principal_id != context.principal_id
    ):
        raise ScopeDeniedError(
            "MCP discovery requires an explicitly principal-bound installed owner"
        )
    installed.authorize(context)
    return {
        "installationSha256": installed.installation.fingerprint,
        "definitionSha256": installed.definition.fingerprint,
        "toolName": installed.tool.name,
        "toolFingerprint": installed.tool.fingerprint,
        "capabilitySha256": installed.capability.fingerprint,
        "implementationSha256": installed.implementation.fingerprint,
        "policySha256": installed.installation.policy_revision_hash,
        "executableSha256": installed.executable_sha256,
        "transport": "streamable_http" if installed.http_provider else "stdio",
        "oauthRegistrationSha256": installed.http_provider.registration_fingerprint
        if installed.http_provider
        else None,
    }


def _config(value: Any) -> dict[str, Any]:
    fields = {
        "name",
        "agentId",
        "configurationSha256",
        "ttlSeconds",
        "watchMs",
        "installationSha256",
        "definitionSha256",
        "toolName",
        "toolFingerprint",
        "capabilitySha256",
        "implementationSha256",
        "policySha256",
        "executableSha256",
        "transport",
        "oauthRegistrationSha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ScopeDeniedError("MCP discovery configuration is inconsistent")
    text_value(value["name"], "MCP binding name", 96)
    text_value(value["toolName"], "MCP tool name", 128)
    if str(UUID(value["agentId"])) != value["agentId"]:
        raise InvalidInputError("MCP Agent identity must be canonical")
    for key in fields:
        if key.endswith("Sha256") and key != "oauthRegistrationSha256":
            _sha(value[key])
    _sha(value["toolFingerprint"])
    if value["oauthRegistrationSha256"] is not None:
        _sha(value["oauthRegistrationSha256"])
    if (
        type(value["ttlSeconds"]) is not int
        or not 60 <= value["ttlSeconds"] <= 3600
        or type(value["watchMs"]) is not int
        or not (value["watchMs"] == 0 or 250 <= value["watchMs"] <= 5000)
    ):
        raise InvalidInputError("MCP freshness or watch allowance is invalid")
    if value["transport"] not in {"stdio", "streamable_http"} or (
        (value["transport"] == "streamable_http") != (value["oauthRegistrationSha256"] is not None)
    ):
        raise InvalidInputError("MCP transport and provider identities differ")
    if value["watchMs"] and value["transport"] != "streamable_http":
        raise InvalidInputError("A bounded subscription watch requires the supported HTTP profile")
    return dict(value)


class McpDiscoveryService:
    def __init__(
        self,
        database: Database,
        *,
        setup_token: Callable[[], str | None] = native_skill_setup_token,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database, self.setup_token = database, setup_token
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _scope(context: WorkspaceContext) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        }

    def _token(self) -> str | None:
        try:
            value = self.setup_token()
            return value if _token_valid(value) else None
        except Exception:
            return None

    def _authorized(self, presented: str) -> str:
        expected = self._token()
        if (
            not expected
            or not _token_valid(presented)
            or not hmac.compare_digest(expected, presented)
        ):
            raise ScopeDeniedError(
                "MCP discovery controls require the current independent setup capability"
            )
        return expected

    @staticmethod
    def _row(
        conn: Any, context: WorkspaceContext, kind: str, identity: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT * FROM {_TABLES[kind]} WHERE workspace_id=%s AND principal_id=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (context.workspace_id, context.principal_id, _uuid(identity)),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError(_DENIED)
        return dict(row)

    @staticmethod
    def _valid(conn: Any, kind: str, row: dict[str, Any], token: str | None) -> bool:
        if not token or not hmac.compare_digest(row["mac"], _mac(token, kind, _body(kind, row))):
            return False
        event = conn.execute(
            "SELECT revision,binding_sha256 FROM mcp_discovery_events WHERE workspace_id=%s AND principal_id=%s AND subject_kind=%s AND subject_id=%s ORDER BY revision DESC LIMIT 1",
            (row["workspace_id"], row["principal_id"], kind, row["id"]),
        ).fetchone()
        return bool(
            event
            and event["revision"] == row["revision"]
            and event["binding_sha256"] == content_hash(_body(kind, row))
        )

    @staticmethod
    def _save(
        conn: Any, kind: str, row: dict[str, Any], token: str, action: str, *, insert: bool = False
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        fields = (
            (
                "config",
                "revision",
                "state",
                "snapshot_id",
                "active_attempt_id",
                "last_attempt_id",
                "last_error_code",
            )
            if kind == "binding"
            else ("binding_id", "agent_id", "revision", "state", "payload", "worker_token")
        )
        row["mac"] = _mac(token, kind, _body(kind, row))
        values = [
            Jsonb(row[name]) if name in {"config", "payload"} else row[name] for name in fields
        ]
        if insert:
            names = ("workspace_id", "id", "principal_id", *fields, "mac")
            saved = conn.execute(
                f"INSERT INTO {_TABLES[kind]} ({','.join(names)}) VALUES ({','.join(['%s'] * len(names))}) RETURNING *",
                (row["workspace_id"], row["id"], row["principal_id"], *values, row["mac"]),
            ).fetchone()
        else:
            saved = conn.execute(
                f"UPDATE {_TABLES[kind]} SET "
                + ",".join(f"{name}=%s" for name in fields)
                + ",mac=%s,updated_at=clock_timestamp() WHERE workspace_id=%s AND principal_id=%s AND id=%s RETURNING *",
                (*values, row["mac"], row["workspace_id"], row["principal_id"], row["id"]),
            ).fetchone()
        if saved is None:
            raise ScopeDeniedError(_DENIED)
        conn.execute(
            "INSERT INTO mcp_discovery_events(workspace_id,id,principal_id,subject_kind,subject_id,revision,action,binding_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["workspace_id"],
                uuid4(),
                row["principal_id"],
                kind,
                row["id"],
                row["revision"],
                action,
                content_hash(_body(kind, row)),
            ),
        )
        return dict(saved)

    @staticmethod
    def _expected(kind: str, row: dict[str, Any], revision: int, digest: str) -> None:
        if (
            type(revision) is not int
            or not 1 <= revision < 2**53 - 1
            or revision != row["revision"]
            or _sha(digest) != content_hash(_body(kind, row))
        ):
            raise RevisionConflictError("Review the current MCP record revision and digest")

    def _snapshot(
        self, conn: Any, context: WorkspaceContext, binding: dict[str, Any]
    ) -> dict[str, Any] | None:
        if binding["snapshot_id"] is None:
            return None
        row = conn.execute(
            "SELECT * FROM mcp_discovery_snapshots WHERE workspace_id=%s AND principal_id=%s AND id=%s AND binding_id=%s",
            (context.workspace_id, context.principal_id, binding["snapshot_id"], binding["id"]),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("MCP catalogue snapshot is unavailable")
        snapshot = dict(row)
        catalogue = McpDiscoveryCatalog.from_dict(snapshot["payload"])
        if catalogue.fingerprint != snapshot["sha256"] or snapshot["config_sha256"] != content_hash(
            binding["config"]
        ):
            raise ScopeDeniedError("MCP catalogue snapshot identity differs")
        return snapshot

    def _fresh(
        self, conn: Any, binding: dict[str, Any], snapshot: dict[str, Any] | None, token: str | None
    ) -> bool:
        return bool(
            token
            and snapshot
            and binding["state"] == "enabled"
            and self._valid(conn, "binding", binding, token)
            and binding["active_attempt_id"] is None
            and binding["last_error_code"] is None
            and hmac.compare_digest(
                snapshot["mac"], _mac(token, "snapshot", _snapshot_body(snapshot))
            )
            and snapshot["fetched_at"] <= self.clock() < snapshot["expires_at"]
        )

    def _binding_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        config = _config(row["config"])
        valid = self._valid(conn, "binding", row, token)
        try:
            snapshot = self._snapshot(conn, context, row)
        except DomainError:
            snapshot = None
        payload = snapshot["payload"] if snapshot else None
        counts = {kind: 0 for kind in _KINDS.values()}
        if payload:
            for item in payload["items"]:
                counts[_KINDS[item["kind"]]] += 1
        actions = []
        if row["state"] != "revoked":
            if valid:
                actions.append("disable" if row["state"] == "enabled" else "enable")
                if row["state"] == "enabled" and row["active_attempt_id"] is None:
                    actions.append("reconcile")
            actions.append("revoke")
        return {
            "id": str(row["id"]),
            **{
                key: config[key]
                for key in (
                    "name",
                    "agentId",
                    "installationSha256",
                    "configurationSha256",
                    "definitionSha256",
                    "toolName",
                    "toolFingerprint",
                    "transport",
                    "ttlSeconds",
                    "watchMs",
                )
            },
            "revision": row["revision"],
            "state": row["state"],
            "bindingSha256": content_hash(_body("binding", row)),
            "approvalValid": valid,
            "snapshotId": str(snapshot["id"]) if snapshot else None,
            "snapshotSha256": snapshot["sha256"] if snapshot else None,
            "fetchedAt": snapshot["fetched_at"].isoformat() if snapshot else None,
            "expiresAt": snapshot["expires_at"].isoformat() if snapshot else None,
            "stale": not self._fresh(conn, row, snapshot, token),
            "lastErrorCode": row["last_error_code"],
            "activeAttemptId": str(row["active_attempt_id"]) if row["active_attempt_id"] else None,
            "lastAttemptId": str(row["last_attempt_id"]) if row["last_attempt_id"] else None,
            "counts": counts,
            "catalogInfo": {
                key: payload[key]
                for key in (
                    "protocolVersion",
                    "serverInfo",
                    "capabilities",
                    "collections",
                    "notificationSupport",
                    "cacheTtlMs",
                )
            }
            if payload
            else None,
            "actions": actions,
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    def _attempt_view(self, conn: Any, row: dict[str, Any], token: str | None) -> dict[str, Any]:
        payload = row["payload"]
        valid = self._valid(conn, "attempt", row, token)
        # A rotated key leaves retained metadata readable, but never claimable.
        if not valid:
            event = conn.execute(
                "SELECT revision,binding_sha256 FROM mcp_discovery_events WHERE workspace_id=%s AND principal_id=%s AND subject_kind='attempt' AND subject_id=%s ORDER BY revision DESC LIMIT 1",
                (row["workspace_id"], row["principal_id"], row["id"]),
            ).fetchone()
            if (
                not event
                or event["revision"] != row["revision"]
                or event["binding_sha256"] != content_hash(_body("attempt", row))
            ):
                raise ScopeDeniedError("MCP attempt integrity differs")
        return {
            "id": str(row["id"]),
            "bindingId": str(row["binding_id"]),
            "agentId": str(row["agent_id"]),
            "revision": row["revision"],
            "bindingSha256": content_hash(_body("attempt", row)),
            "status": row["state"],
            **{
                key: payload[key]
                for key in (
                    "requestedBindingSha256",
                    "installationSha256",
                    "configurationSha256",
                    "snapshotId",
                    "snapshotSha256",
                    "changes",
                    "reasonCode",
                )
            },
            "actions": ["cancel"] if row["state"] in {"requested", "running"} else [],
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _page(limit: int, after: str | None) -> UUID | None:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidInputError("MCP discovery pages require one to fifty entries")
        if after is None:
            return None
        try:
            value = UUID(after)
            if str(value) != after:
                raise ValueError
            return value
        except (ValueError, TypeError, AttributeError):
            raise InvalidInputError("A canonical MCP discovery cursor is required") from None

    def binding(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "binding": self._binding_view(
                    conn, context, self._row(conn, context, "binding", identity), self._token()
                ),
            }

    def bindings(
        self, context: WorkspaceContext, *, limit: int = 25, after: str | None = None
    ) -> dict[str, Any]:
        cursor = self._page(limit, after)
        with self.database.transaction(context) as conn:
            token = self._token()
            rows = conn.execute(
                "SELECT * FROM mcp_discovery_bindings WHERE workspace_id=%s AND principal_id=%s AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (context.workspace_id, context.principal_id, cursor, cursor, limit + 1),
            ).fetchall()
            return self._list(
                context,
                [self._binding_view(conn, context, dict(row), token) for row in rows],
                limit,
            )

    def attempt(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "attempt": self._attempt_view(
                    conn, self._row(conn, context, "attempt", identity), self._token()
                ),
            }

    def attempts(
        self,
        context: WorkspaceContext,
        *,
        binding_id: UUID | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        cursor = self._page(limit, after)
        if binding_id is not None:
            _uuid(binding_id)
        with self.database.transaction(context) as conn:
            token = self._token()
            rows = conn.execute(
                "SELECT * FROM mcp_discovery_attempts WHERE workspace_id=%s AND principal_id=%s AND (%s::uuid IS NULL OR binding_id=%s) AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (
                    context.workspace_id,
                    context.principal_id,
                    binding_id,
                    binding_id,
                    cursor,
                    cursor,
                    limit + 1,
                ),
            ).fetchall()
            return self._list(
                context, [self._attempt_view(conn, dict(row), token) for row in rows], limit
            )

    def _list(
        self, context: WorkspaceContext, values: list[dict[str, Any]], limit: int
    ) -> dict[str, Any]:
        items, size = [], 0
        for item in values[:limit]:
            count = len(canonical_json(item).encode())
            if size + count > PAGE_BYTES - 1024:
                break
            items.append(item)
            size += count
        return {
            **self._scope(context),
            "items": items,
            "nextCursor": items[-1]["id"] if items and len(values) > len(items) else None,
        }

    @staticmethod
    def _entry_id(binding: UUID, snapshot: UUID, item: dict[str, Any]) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"potbot:mcp-discovery:{binding}:{snapshot}:"
            + content_hash([item["kind"], item["identity"]]),
        )

    @staticmethod
    def _configured_tool(config: dict[str, Any], item: dict[str, Any]) -> bool:
        if item["kind"] != "tool" or item["identity"] != config["toolName"]:
            return False
        try:
            return bool(
                McpToolInfo.from_wire(
                    item["definition"], allow_http_headers=config["transport"] == "streamable_http"
                ).fingerprint
                == config["toolFingerprint"]
            )
        except DomainError:
            return False

    def _entry_view(
        self, row: dict[str, Any], snapshot: dict[str, Any], item: dict[str, Any], fresh: bool
    ) -> dict[str, Any]:
        configured = self._configured_tool(row["config"], item)
        reasons = [] if fresh else ["catalogue_not_current"]
        if item["kind"] == "tool" and not configured:
            reasons.append("tool_requires_separate_admission")
        return {
            "id": str(self._entry_id(row["id"], snapshot["id"], item)),
            "bindingId": str(row["id"]),
            "snapshotId": str(snapshot["id"]),
            "snapshotSha256": snapshot["sha256"],
            **{
                key: item[key]
                for key in (
                    "kind",
                    "identity",
                    "name",
                    "description",
                    "definition",
                    "definitionSha256",
                )
            },
            "configuredTool": configured,
            "referenceAvailable": fresh,
            "reasonCodes": reasons,
            "executable": False,
        }

    def entries(
        self,
        context: WorkspaceContext,
        identity: UUID,
        *,
        kind: str | None = None,
        query: str = "",
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        cursor = self._page(limit, after)
        text_value(query, "MCP entry query", 256, empty=True)
        if kind is not None and kind not in _KINDS:
            raise InvalidInputError("Unknown MCP catalogue entry kind")
        with self.database.transaction(context) as conn:
            row = self._row(conn, context, "binding", identity)
            snapshot = self._snapshot(conn, context, row)
            if snapshot is None:
                if cursor:
                    raise RevisionConflictError("MCP catalogue changed; restart pagination")
                return self._list(context, [], limit)
            fresh = self._fresh(conn, row, snapshot, self._token())
            values = [
                self._entry_view(row, snapshot, item, fresh)
                for item in snapshot["payload"]["items"]
                if (kind is None or item["kind"] == kind)
                and (
                    not query
                    or query.casefold()
                    in (
                        item["name"] + " " + item["description"] + " " + item["identity"]
                    ).casefold()
                )
            ]
            values.sort(key=lambda item: item["id"])
            if cursor:
                if not any(item["id"] == str(cursor) for item in values):
                    raise RevisionConflictError("MCP catalogue changed; restart pagination")
                values = [item for item in values if item["id"] > str(cursor)]
            return self._list(context, values, limit)

    @staticmethod
    def _reference(
        context: WorkspaceContext,
        row: dict[str, Any],
        snapshot: dict[str, Any],
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema": REFERENCE_SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "bindingId": str(row["id"]),
            "bindingRevision": row["revision"],
            "bindingSha256": content_hash(_body("binding", row)),
            "installationSha256": row["config"]["installationSha256"],
            "snapshotId": str(snapshot["id"]),
            "snapshotSha256": snapshot["sha256"],
            "entryId": entry["id"],
            "kind": entry["kind"],
            "identity": entry["identity"],
            "definitionSha256": entry["definitionSha256"],
            "expiresAt": snapshot["expires_at"].isoformat(),
        }

    def reference(
        self, context: WorkspaceContext, identity: UUID, entry_id: UUID
    ) -> dict[str, Any]:
        _uuid(entry_id)
        with self.database.transaction(context) as conn:
            row = self._row(conn, context, "binding", identity)
            snapshot = self._snapshot(conn, context, row)
            if snapshot is None or not self._fresh(conn, row, snapshot, self._token()):
                raise ScopeDeniedError("MCP catalogue has no current signed observation")
            for item in snapshot["payload"]["items"]:
                entry = self._entry_view(row, snapshot, item, True)
                if entry["id"] == str(entry_id):
                    return {
                        **self._scope(context),
                        "reference": self._reference(context, row, snapshot, entry),
                    }
            raise ScopeDeniedError("The exact MCP entry is absent from the current snapshot")

    def validate_reference(
        self,
        context: WorkspaceContext,
        reference: dict[str, Any],
        installed: InstalledMcpTool | None = None,
        *,
        connection: Any = None,
    ) -> dict[str, Any]:
        try:
            if not isinstance(reference, dict):
                raise ValueError
            identity, entry_id = UUID(reference["bindingId"]), UUID(reference["entryId"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ScopeDeniedError("An exact retained MCP reference is required") from None
        with (
            self.database.transaction(context)
            if connection is None
            else nullcontext(connection) as conn
        ):
            row = self._row(conn, context, "binding", identity)
            snapshot = self._snapshot(conn, context, row)
            if snapshot is None or not self._fresh(conn, row, snapshot, self._token()):
                raise ScopeDeniedError("The MCP discovery reference is no longer current")
            for item in snapshot["payload"]["items"]:
                entry = self._entry_view(row, snapshot, item, True)
                if entry["id"] != str(entry_id):
                    continue
                if reference != self._reference(context, row, snapshot, entry):
                    raise ScopeDeniedError(
                        "The MCP reference differs from the exact retained entry"
                    )
                if installed is not None:
                    identity_value = installed_identity(installed, context)
                    if (
                        installed.discovery_binding_id != row["id"]
                        or installed.discovery_configuration_sha256
                        != row["config"]["configurationSha256"]
                        or any(row["config"][key] != value for key, value in identity_value.items())
                        or not entry["configuredTool"]
                    ):
                        raise ScopeDeniedError(
                            "MCP observation differs from the current installed tool owner"
                        )
                return entry
            raise ScopeDeniedError("The referenced MCP entry has been removed or replaced")

    def admit(
        self,
        context: WorkspaceContext,
        installed: InstalledMcpTool,
        agent_id: UUID,
        configuration_sha256: str,
        name: str,
        ttl_seconds: int,
        watch_ms: int,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        config = _config(
            {
                **installed_identity(installed, context),
                "agentId": str(_uuid(agent_id)),
                "configurationSha256": _sha(configuration_sha256),
                "name": name,
                "ttlSeconds": ttl_seconds,
                "watchMs": watch_ms,
            }
        )
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                conn.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                    (f"potbot:mcp-discovery:{context.workspace_id}:{context.principal_id}",),
                )
                count = conn.execute(
                    "SELECT count(*) AS n FROM mcp_discovery_bindings WHERE workspace_id=%s AND principal_id=%s AND state<>'revoked'",
                    (context.workspace_id, context.principal_id),
                ).fetchone()["n"]
                if count >= MAX_BINDINGS:
                    raise InvalidInputError("Revoke a retired MCP binding before admitting another")
                row = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": uuid4(),
                    "config": config,
                    "revision": 1,
                    "state": "enabled",
                    "snapshot_id": None,
                    "active_attempt_id": None,
                    "last_attempt_id": None,
                    "last_error_code": "not_observed",
                }
                saved = self._save(conn, "binding", row, token, "admitted", insert=True)
                return {
                    **self._scope(context),
                    "binding": self._binding_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "mcp.discovery.admit", _key(context, key), config, create
            )

    def control(
        self,
        context: WorkspaceContext,
        identity: UUID,
        action: str,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        choices = {"enable": "enabled", "disable": "disabled", "revoke": "revoked"}
        if action not in choices:
            raise InvalidInputError("Unsupported MCP discovery control")
        request = {
            "id": str(_uuid(identity)),
            "action": action,
            "revision": revision,
            "digest": _sha(digest),
        }
        with self.database.transaction(context) as conn:

            def change() -> dict[str, Any]:
                row = self._row(conn, context, "binding", identity, lock=True)
                self._expected("binding", row, revision, digest)
                if row["state"] == "revoked" or row["state"] == choices[action]:
                    raise RevisionConflictError("This MCP binding transition is unavailable")
                if action != "revoke" and not self._valid(conn, "binding", row, token):
                    raise ScopeDeniedError(
                        "Nonterminal MCP controls require the prior independent signature"
                    )
                if row["active_attempt_id"]:
                    attempt = self._row(
                        conn, context, "attempt", row["active_attempt_id"], lock=True
                    )
                    if attempt["state"] in {"requested", "running"} and self._valid(
                        conn, "attempt", attempt, token
                    ):
                        attempt.update(
                            state="cancelled", worker_token=None, revision=attempt["revision"] + 1
                        )
                        attempt["payload"] = {
                            **attempt["payload"],
                            "reasonCode": "authority_changed",
                        }
                        self._save(conn, "attempt", attempt, token, "authority_changed")
                row.update(
                    state=choices[action],
                    revision=row["revision"] + 1,
                    active_attempt_id=None,
                    last_error_code="revalidation_required"
                    if action == "enable"
                    else "binding_" + choices[action],
                )
                saved = self._save(conn, "binding", row, token, action)
                return {
                    **self._scope(context),
                    "binding": self._binding_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "mcp.discovery.control", _key(context, key), request, change
            )

    def request(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                binding = self._row(conn, context, "binding", identity, lock=True)
                self._expected("binding", binding, revision, digest)
                if (
                    binding["state"] != "enabled"
                    or not self._valid(conn, "binding", binding, token)
                    or binding["active_attempt_id"] is not None
                ):
                    raise ScopeDeniedError(
                        "MCP reconciliation requires one current enabled idle binding"
                    )
                config, attempt_id = _config(binding["config"]), uuid4()
                binding.update(
                    revision=binding["revision"] + 1,
                    active_attempt_id=attempt_id,
                    last_attempt_id=attempt_id,
                    last_error_code="reconciling",
                )
                payload = {
                    "requestedBindingSha256": digest,
                    "admissionSha256": content_hash(_body("binding", binding)),
                    "installationSha256": config["installationSha256"],
                    "configurationSha256": config["configurationSha256"],
                    "snapshotId": None,
                    "snapshotSha256": None,
                    "changes": None,
                    "reasonCode": None,
                }
                row = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": attempt_id,
                    "binding_id": identity,
                    "agent_id": UUID(config["agentId"]),
                    "revision": 1,
                    "state": "requested",
                    "worker_token": None,
                    "payload": payload,
                }
                saved = self._save(conn, "attempt", row, token, "requested", insert=True)
                self._save(conn, "binding", binding, token, "reconciliation_requested")
                return {**self._scope(context), "attempt": self._attempt_view(conn, saved, token)}

            return idempotent_command(
                conn, context, "mcp.discovery.request", _key(context, key), request, create
            )

    def _attempt_rows(
        self, conn: Any, context: WorkspaceContext, identity: UUID
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        observed = self._row(conn, context, "attempt", identity)
        binding = self._row(conn, context, "binding", observed["binding_id"], lock=True)
        attempt = self._row(conn, context, "attempt", identity, lock=True)
        if attempt["binding_id"] != binding["id"]:
            raise ScopeDeniedError("MCP attempt binding changed")
        return binding, attempt

    def _current(
        self, conn: Any, binding: dict[str, Any], attempt: dict[str, Any], token: str | None
    ) -> None:
        if (
            not self._valid(conn, "binding", binding, token)
            or not self._valid(conn, "attempt", attempt, token)
            or binding["state"] != "enabled"
            or binding["active_attempt_id"] != attempt["id"]
            or attempt["payload"]["admissionSha256"] != content_hash(_body("binding", binding))
            or attempt["payload"]["configurationSha256"] != binding["config"]["configurationSha256"]
            or attempt["payload"]["installationSha256"] != binding["config"]["installationSha256"]
        ):
            raise ScopeDeniedError("MCP reconciliation no longer has current independent admission")

    def execution_replay(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any] | None:
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:
            prior = conn.execute(
                "SELECT request_hash FROM command_results WHERE workspace_id=%s AND principal_id=%s AND operation='mcp.discovery.claim' AND idempotency_key=%s",
                (context.workspace_id, context.principal_id, _key(context, key)),
            ).fetchone()
            if prior is None:
                return None
            if prior["request_hash"] != content_hash(request):
                raise RevisionConflictError(
                    "This host command key belongs to another exact request"
                )
            return {
                **self._scope(context),
                "attempt": self._attempt_view(
                    conn, self._row(conn, context, "attempt", identity), token
                ),
            }

    def claim(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
        fresh_only: bool = False,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:
            created = False

            def acquire() -> dict[str, Any]:
                nonlocal created
                binding, row = self._attempt_rows(conn, context, identity)
                self._expected("attempt", row, revision, digest)
                self._current(conn, binding, row, token)
                if row["state"] != "requested":
                    raise RevisionConflictError(
                        "This MCP reconciliation already has a native attempt"
                    )
                row.update(state="running", worker_token=uuid4(), revision=row["revision"] + 1)
                saved = self._save(conn, "attempt", row, token, "claimed")
                created = True
                return {
                    "id": str(saved["id"]),
                    "bindingId": str(binding["id"]),
                    "agentId": str(saved["agent_id"]),
                    "workerToken": str(saved["worker_token"]),
                    "config": binding["config"],
                }

            result = idempotent_command(
                conn, context, "mcp.discovery.claim", _key(context, key), request, acquire
            )
            if fresh_only and not created:
                return {
                    "replay": {
                        **self._scope(context),
                        "attempt": self._attempt_view(
                            conn, self._row(conn, context, "attempt", identity), token
                        ),
                    }
                }
            return result

    def validate_claim(
        self,
        context: WorkspaceContext,
        identity: UUID,
        worker_token: UUID,
        *,
        connection: Any = None,
    ) -> dict[str, Any]:
        _uuid(worker_token)
        with (
            self.database.transaction(context)
            if connection is None
            else nullcontext(connection) as conn
        ):
            binding, row = self._attempt_rows(conn, context, identity)
            if row["state"] != "running" or row["worker_token"] != worker_token:
                raise ScopeDeniedError("The MCP reconciliation worker claim has been fenced")
            self._current(conn, binding, row, self._token())
            return {**row, "config": binding["config"]}

    def _observation(
        self, context: WorkspaceContext, config: dict[str, Any], value: Any
    ) -> tuple[dict[str, Any], datetime]:
        fields = {
            "schema",
            "workspaceId",
            "principalId",
            "agentId",
            "installationSha256",
            "configurationSha256",
            "definitionSha256",
            "executableSha256",
            "runtimeSha256",
            "imageSha256",
            "qualificationSha256",
            "agentHomeRevision",
            "observedAt",
            "completedAt",
            "isolationSha256",
            "stopReceiptSha256",
            "processesStopped",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != "potbot-mcp-observation/v1"
            or value["processesStopped"] is not True
        ):
            raise ScopeDeniedError(
                "MCP observation requires exact confirmed native lifetime evidence"
            )
        for key in fields:
            if key.endswith("Sha256"):
                _sha(value[key])
        expected = {
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            **{
                key: config[key]
                for key in (
                    "agentId",
                    "installationSha256",
                    "configurationSha256",
                    "definitionSha256",
                    "executableSha256",
                )
            },
        }
        if (
            any(value[key] != expected_value for key, expected_value in expected.items())
            or type(value["agentHomeRevision"]) is not int
            or not 1 <= value["agentHomeRevision"] < 2**53
        ):
            raise ScopeDeniedError("MCP native evidence differs from the exact admitted owner")
        try:
            observed, completed = (
                datetime.fromisoformat(value["observedAt"]),
                datetime.fromisoformat(value["completedAt"]),
            )
            now = self.clock()
            if (
                observed.utcoffset() != timedelta(0)
                or completed.utcoffset() != timedelta(0)
                or not observed <= completed <= now
                or completed - observed > timedelta(seconds=120)
                or now - completed > timedelta(seconds=30)
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise RevisionConflictError(
                "MCP native lifetime evidence is stale or inconsistent"
            ) from None
        return dict(value), observed

    def complete(
        self,
        context: WorkspaceContext,
        identity: UUID,
        worker_token: UUID,
        catalog: McpDiscoveryCatalog,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        if not isinstance(catalog, McpDiscoveryCatalog):
            raise InvalidInputError("A complete validated live MCP catalogue is required")
        catalog = McpDiscoveryCatalog.from_dict(catalog.to_dict())
        payload = catalog.to_dict()
        with self.database.transaction(context) as conn:
            self.validate_claim(context, identity, worker_token, connection=conn)
            binding, row = self._attempt_rows(conn, context, identity)
            observed, fetched = self._observation(context, binding["config"], observation)
            token = self._token()
            self._current(conn, binding, row, token)
            assert token is not None
            previous = self._snapshot(conn, context, binding)
            old = (
                {
                    (item["kind"], item["identity"]): item["definitionSha256"]
                    for item in previous["payload"]["items"]
                }
                if previous
                else {}
            )
            new = {
                (item["kind"], item["identity"]): item["definitionSha256"]
                for item in payload["items"]
            }
            common = old.keys() & new.keys()
            changes = {
                "added": len(new.keys() - old.keys()),
                "removed": len(old.keys() - new.keys()),
                "changed": sum(old[key] != new[key] for key in common),
                "unchanged": sum(old[key] == new[key] for key in common),
            }
            ttl = binding["config"]["ttlSeconds"] * 1000
            if payload["cacheTtlMs"] is not None:
                ttl = min(ttl, payload["cacheTtlMs"])
            snapshot = {
                "workspace_id": context.workspace_id,
                "principal_id": context.principal_id,
                "id": uuid4(),
                "binding_id": binding["id"],
                "attempt_id": row["id"],
                "config_sha256": content_hash(binding["config"]),
                "payload": payload,
                "observation": observed,
                "sha256": catalog.fingerprint,
                "fetched_at": fetched,
                "expires_at": fetched + timedelta(milliseconds=ttl),
            }
            snapshot["mac"] = _mac(token, "snapshot", _snapshot_body(snapshot))
            conn.execute(
                "INSERT INTO mcp_discovery_snapshots(workspace_id,id,principal_id,binding_id,attempt_id,config_sha256,payload,observation,sha256,mac,fetched_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    snapshot["id"],
                    context.principal_id,
                    binding["id"],
                    row["id"],
                    snapshot["config_sha256"],
                    Jsonb(payload),
                    Jsonb(observed),
                    snapshot["sha256"],
                    snapshot["mac"],
                    snapshot["fetched_at"],
                    snapshot["expires_at"],
                ),
            )
            row["payload"] = {
                **row["payload"],
                "snapshotId": str(snapshot["id"]),
                "snapshotSha256": snapshot["sha256"],
                "changes": changes,
                "reasonCode": None,
            }
            row.update(state="succeeded", worker_token=None, revision=row["revision"] + 1)
            saved = self._save(conn, "attempt", row, token, "observed")
            binding.update(
                revision=binding["revision"] + 1,
                snapshot_id=snapshot["id"],
                active_attempt_id=None,
                last_error_code=None,
            )
            self._save(conn, "binding", binding, token, "catalogue_reconciled")
            return {**self._scope(context), "attempt": self._attempt_view(conn, saved, token)}

    def fail(
        self, context: WorkspaceContext, identity: UUID, worker_token: UUID, reason: str
    ) -> dict[str, Any]:
        if reason not in _FAILURES:
            raise InvalidInputError("Unknown MCP reconciliation failure classification")
        with self.database.transaction(context) as conn:
            binding, row = self._attempt_rows(conn, context, identity)
            token = self._token()
            if (
                not token
                or row["state"] != "running"
                or row["worker_token"] != _uuid(worker_token)
                or not self._valid(conn, "attempt", row, token)
            ):
                raise ScopeDeniedError("MCP reconciliation has already been fenced")
            row.update(
                state="indeterminate" if reason == "reconciliation_indeterminate" else "failed",
                worker_token=None,
                revision=row["revision"] + 1,
            )
            row["payload"] = {**row["payload"], "reasonCode": reason}
            saved = self._save(conn, "attempt", row, token, reason)
            if binding["active_attempt_id"] == row["id"] and self._valid(
                conn, "binding", binding, token
            ):
                binding.update(
                    revision=binding["revision"] + 1, active_attempt_id=None, last_error_code=reason
                )
                self._save(conn, "binding", binding, token, "reconciliation_failed")
            return {**self._scope(context), "attempt": self._attempt_view(conn, saved, token)}

    def cancel(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:

            def retire() -> dict[str, Any]:
                binding, row = self._attempt_rows(conn, context, identity)
                self._expected("attempt", row, revision, digest)
                if row["state"] not in {"requested", "running"}:
                    raise RevisionConflictError(
                        "The MCP reconciliation attempt is already terminal"
                    )
                row.update(state="cancelled", worker_token=None, revision=row["revision"] + 1)
                row["payload"] = {**row["payload"], "reasonCode": "reconciliation_cancelled"}
                saved = self._save(conn, "attempt", row, token, "cancelled")
                if binding["active_attempt_id"] == row["id"] and self._valid(
                    conn, "binding", binding, token
                ):
                    binding.update(
                        revision=binding["revision"] + 1,
                        active_attempt_id=None,
                        last_error_code="reconciliation_cancelled",
                    )
                    self._save(conn, "binding", binding, token, "reconciliation_cancelled")
                return {**self._scope(context), "attempt": self._attempt_view(conn, saved, token)}

            return idempotent_command(
                conn, context, "mcp.discovery.cancel", _key(context, key), request, retire
            )
