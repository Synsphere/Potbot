"""Workspace-scoped settings and inventory projections over existing owners."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from potbot.controller.swarm_requests import OPERATION, grant_from_row, request_state
from potbot.domain.auth import WorkspaceContext
from potbot.domain.configuration import DEFAULTS, ResolvedConfiguration
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.settings_catalogue import catalogue, display_value
from potbot.persistence.database import Database
from potbot.tools.local_inventory import InventoryProfile, inspect_local

LIMIT = 100


class SettingsService:
    def __init__(self, database: Database, inventory: InventoryProfile | None = None) -> None:
        self.database = database
        self.inventory = inventory

    @staticmethod
    def envelope(context: WorkspaceContext) -> dict[str, Any]:
        return {
            "schema": "potbot-settings/v1",
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        }

    def catalogue(self, context: WorkspaceContext) -> dict[str, Any]:
        with self.database.transaction(context):
            pass
        return {**self.envelope(context), **catalogue()}

    def effective(self, context: WorkspaceContext) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            latest = connection.execute(
                "SELECT id,content,content_hash,created_at FROM resolved_configurations "
                "WHERE workspace_id=%s ORDER BY created_at DESC,id DESC LIMIT 1",
                (context.workspace_id,),
            ).fetchone()
        snapshot = None
        if latest:
            accepted = ResolvedConfiguration(canonical_json(latest["content"]))
            if accepted.fingerprint != latest["content_hash"]:
                from potbot.domain.errors import InvalidInputError

                raise InvalidInputError("Recorded configuration integrity check failed")
            snapshot = {
                "id": str(latest["id"]),
                "sha256": latest["content_hash"],
                "recordedAt": latest["created_at"].isoformat(),
                "values": [
                    {"id": "configuration." + name, "value": display_value(name, value)}
                    for name, value in accepted.settings.items()
                ],
            }
        return {
            **self.envelope(context),
            "liveLayersAvailable": False,
            "defaults": [
                {"id": "configuration." + name, "value": display_value(name, value)}
                for name, value in DEFAULTS.items()
            ],
            "latestRecordedSnapshot": snapshot,
        }

    def inventory_view(self, context: WorkspaceContext, kind: str) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            if kind == "models":
                rows = connection.execute(
                    "SELECT d.id,d.canonical_model,d.content,"
                    "COALESCE(h.health,'unknown') AS health,q.qualified "
                    "FROM model_deployments d LEFT JOIN deployment_health h ON "
                    "(h.workspace_id,h.deployment_id)=(d.workspace_id,d.id) "
                    "LEFT JOIN deployment_qualifications q ON "
                    "(q.workspace_id,q.deployment_id)=(d.workspace_id,d.id) "
                    "WHERE d.workspace_id=%s ORDER BY d.id LIMIT %s",
                    (context.workspace_id, LIMIT + 1),
                ).fetchall()
                records = [
                    {
                        "id": str(row["id"]),
                        "name": row["canonical_model"],
                        "version": str(row["content"].get("quantization", "")),
                        "origin": "deployment_record",
                        "location": "Deployment registry",
                        "bytes": None,
                        "presence": "not_observed",
                        "verification": "not_checked",
                        "qualification": "qualified"
                        if row["qualified"]
                        else "unqualified"
                        if row["qualified"] is False
                        else "unknown",
                        "runtime": row["health"],
                        "permission": "not_evaluated",
                        "detail": "Registered deployment; current files and task admission not inspected.",
                    }
                    for row in rows[:LIMIT]
                ]
            else:
                rows = connection.execute(
                    "SELECT id,slug,new_commit,state,release_phase FROM ui_tool_upgrade_approvals "
                    "WHERE workspace_id=%s AND principal_id=%s ORDER BY created_at DESC,id LIMIT %s",
                    (context.workspace_id, context.principal_id, LIMIT + 1),
                ).fetchall()
                records = [
                    {
                        "id": str(row["id"]),
                        "name": row["slug"],
                        "version": row["new_commit"],
                        "origin": "tool_release_record",
                        "location": "Tool acquisition ledger",
                        "bytes": None,
                        "presence": "not_observed",
                        "verification": "not_checked",
                        "qualification": "unknown",
                        "runtime": "unknown",
                        "permission": "not_evaluated",
                        "detail": "Lifecycle: "
                        + row["state"]
                        + "; release: "
                        + row["release_phase"]
                        + ". Current files and execution permission not inspected.",
                    }
                    for row in rows[:LIMIT]
                ]
        # No native paths come from the caller or DB. The native operator pins
        # the profile once and it belongs to exactly one authenticated workspace.
        if self.inventory and self.inventory.workspace_id != context.workspace_id:
            from potbot.domain.errors import ScopeDeniedError

            raise ScopeDeniedError("Local inventory belongs to another workspace")
        local = inspect_local(self.inventory, kind)
        return {
            **self.envelope(context),
            "kind": kind,
            "observedAt": datetime.now(UTC).isoformat(),
            "local": {key: value for key, value in local.items() if key != "items"},
            "items": local["items"] + records,
            "recordsTruncated": len(rows) > LIMIT,
            "runtimeProbed": False,
            "permissionEvaluated": False,
        }

    def swarm(self, context: WorkspaceContext, *, request_available: bool) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            owner = (
                connection.execute(
                    "SELECT 1 FROM remote_workspace_owners WHERE workspace_id=%s AND principal_id=%s",
                    (context.workspace_id, context.principal_id),
                ).fetchone()
                is not None
            )
            nodes = connection.execute(
                "SELECT id,generation,registration,state,created_at FROM remote_nodes "
                "WHERE workspace_id=%s ORDER BY created_at,id LIMIT %s",
                (context.workspace_id, LIMIT + 1),
            ).fetchall()
            deployments = connection.execute(
                "SELECT DISTINCT ON (workspace_id) workspace_id,node_id,revision,"
                "execution_mode,qualification,environment FROM native_workspace_deployments "
                "WHERE workspace_id=%s ORDER BY workspace_id,revision DESC LIMIT %s",
                (context.workspace_id, LIMIT + 1),
            ).fetchall()
            grants = connection.execute(
                "SELECT DISTINCT ON (id) * FROM workspace_control_grant_revisions "
                "WHERE workspace_id=%s ORDER BY id,revision DESC LIMIT %s",
                (context.workspace_id, LIMIT + 1),
            ).fetchall()
            requests = connection.execute(
                "SELECT * FROM command_results WHERE workspace_id=%s AND principal_id=%s "
                "AND operation=%s ORDER BY recorded_at DESC,id LIMIT %s",
                (context.workspace_id, context.principal_id, OPERATION, LIMIT + 1),
            ).fetchall()
            request_views = [request_state(connection, context, row) for row in requests[:LIMIT]]
        now = datetime.now(UTC)
        grant_views = []
        for row in grants[:LIMIT]:
            grant = grant_from_row(row)
            grant_views.append(
                {
                    "id": str(grant.id),
                    "sourceWorkspaceId": str(grant.source_workspace_id),
                    "revision": grant.revision,
                    "revocationGeneration": grant.revocation_generation,
                    "sha256": content_hash(grant.to_dict()),
                    "permissions": sorted(grant.permissions),
                    "expiresAt": grant.expires_at.isoformat(),
                    "state": "revoked"
                    if grant.revoked
                    else "expired"
                    if grant.expires_at <= now
                    else "active",
                    "canRequestRevocation": owner and request_available and not grant.revoked,
                }
            )
        return {
            **self.envelope(context),
            "observedAt": now.isoformat(),
            "nodes": [
                {
                    "id": str(row["id"]),
                    "label": row["registration"]["hostname"],
                    "generation": row["generation"],
                    "state": row["state"],
                    "sshFingerprint": row["registration"]["ssh_fingerprint"],
                    "registeredAt": row["created_at"].isoformat(),
                    "lastAuthenticatedContact": None,
                    "runtimeQualified": False,
                }
                for row in nodes[:LIMIT]
            ],
            "deployments": [
                {
                    "workspaceId": str(row["workspace_id"]),
                    "nodeId": str(row["node_id"]),
                    "revision": row["revision"],
                    "executionMode": row["execution_mode"],
                    "qualification": row["qualification"],
                    "platform": " ".join(
                        str(row["environment"].get(key, "unknown"))
                        for key in ("os_id", "os_version", "architecture")
                    ),
                }
                for row in deployments[:LIMIT]
            ],
            "grants": grant_views,
            "requests": request_views,
            "recordsTruncated": any(
                len(items) > LIMIT for items in (nodes, deployments, grants, requests)
            ),
            "liveCapacityAvailable": False,
            "workerTelemetryAvailable": False,
            "nativeNodeActionsAvailable": False,
        }

    def request(
        self, context: WorkspaceContext, request_id: UUID, value: dict[str, Any]
    ) -> dict[str, Any]:
        from potbot.controller.swarm_requests import request_revocation

        with self.database.transaction(context) as connection:
            result = request_revocation(connection, context, request_id, value)
        return {**self.envelope(context), **result}
