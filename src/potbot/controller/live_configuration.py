"""Revisioned workspace overrides consumed by actual future task admissions."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.configuration import ResolvedConfiguration, resolve_configuration
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.domain.live_controls import EDITABLE, SCHEMA, configuration_review, overrides
from potbot.domain.serialization import content_hash
from potbot.persistence.database import Database, idempotent_command


def is_owner(connection: Any, context: WorkspaceContext) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM remote_workspace_owners WHERE workspace_id=%s AND principal_id=%s",
            (context.workspace_id, context.principal_id),
        ).fetchone()
        is not None
    )


def require_owner(connection: Any, context: WorkspaceContext) -> None:
    if not is_owner(connection, context):
        raise ScopeDeniedError("The native workspace owner is required")


def current(connection: Any, context: WorkspaceContext) -> dict[str, Any]:
    row = connection.execute(
        "SELECT revision,overrides,overrides_sha256,request_id FROM workspace_configuration_revisions "
        "WHERE workspace_id=%s ORDER BY revision DESC LIMIT 1",
        (context.workspace_id,),
    ).fetchone()
    if row is None:
        return {"revision": 0, "values": {}, "sha256": content_hash({}), "requestId": None}
    accepted = overrides(row["overrides"])
    if content_hash(accepted) != row["overrides_sha256"]:
        raise RevisionConflictError("Live configuration integrity check failed")
    return {
        "revision": row["revision"],
        "values": accepted,
        "sha256": row["overrides_sha256"],
        "requestId": str(row["request_id"]),
    }


def resolve_live(
    connection: Any,
    context: WorkspaceContext,
    base: Mapping[str, Mapping[str, object]] | None = None,
) -> ResolvedConfiguration:
    layers: dict[str, Any] = copy.deepcopy(dict(base or {}))
    workspace = dict(layers.get("workspace", {}))
    for name, value in current(connection, context)["values"].items():
        workspace[name] = (
            {**workspace.get(name, {}), **value} if name == "policy_versions" else value
        )
    layers["workspace"] = workspace
    return resolve_configuration(layers)


class LiveConfiguration:
    def __init__(self, database: Database) -> None:
        self.database = database

    def read(self, context: WorkspaceContext, *, setup_available: bool) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            saved = current(connection, context)
            effective = resolve_live(connection, context)
            owner = is_owner(connection, context)
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "scope": "workspace",
            **saved,
            "canEdit": owner and setup_available,
            "editableFields": list(EDITABLE),
            "apply": "next_admission",
            "effective": {name: effective.settings[name] for name in EDITABLE},
            "managedElsewhere": ["secret_refs", "source_access"],
        }

    def save(self, context: WorkspaceContext, value: Any) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        review = configuration_review(value)
        with self.database.transaction(context) as connection:
            require_owner(connection, context)

            def apply() -> dict[str, Any]:
                connection.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                    ("live-configuration:" + str(context.workspace_id),),
                )
                previous = current(connection, context)
                if (
                    previous["revision"] != review["expectedRevision"]
                    or previous["sha256"] != review["expectedSha256"]
                ):
                    raise RevisionConflictError(
                        "Workspace configuration changed; reload and review"
                    )
                fingerprint = content_hash(review["values"])
                connection.execute(
                    "INSERT INTO workspace_configuration_revisions "
                    "(workspace_id,revision,predecessor_revision,principal_id,request_id,overrides,overrides_sha256) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        previous["revision"] + 1,
                        previous["revision"] or None,
                        context.principal_id,
                        UUID(review["requestId"]),
                        Jsonb(review["values"]),
                        fingerprint,
                    ),
                )
                return {
                    "schema": SCHEMA,
                    "workspaceId": str(context.workspace_id),
                    "principalId": str(context.principal_id),
                    "requestId": review["requestId"],
                    "revision": previous["revision"] + 1,
                    "sha256": fingerprint,
                    "values": review["values"],
                    "apply": "next_admission",
                }

            # Principal is part of the key, so another member cannot obtain an owner's result.
            return idempotent_command(
                connection,
                context,
                "save_live_configuration",
                str(context.principal_id) + ":" + review["requestId"],
                review,
                apply,
            )
