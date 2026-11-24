"""Durable owner intents; only explicit native administration can revoke a grant.

The existing command journal stores intent, not authority or completion. A
request is fulfilled only when the protected grant ledger independently agrees.
Application connections never obtain an administrator DSN.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from uuid import UUID

from potbot.controller.remote_workspaces import _owner, install_control_grant
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.remote_workspaces import WorkspaceControlGrant
from potbot.domain.serialization import content_hash

OPERATION = "swarm.revoke.request"
SCHEMA = "potbot-swarm-revocation-request/v1"


def grant_from_row(row: Any) -> WorkspaceControlGrant:
    return WorkspaceControlGrant(
        id=row["id"],
        workspace_id=row["workspace_id"],
        issuer_principal_id=row["issuer_principal_id"],
        source_workspace_id=row["source_workspace_id"],
        principal_id=row["principal_id"],
        permissions=tuple(row["permissions"]),
        expires_at=row["expires_at"],
        revision=row["revision"],
        revocation_generation=row["revocation_generation"],
        revoked=row["revoked"],
    )


def _request(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema", "grantId", "expectedRevision", "expectedGeneration", "grantSha256"}
        or value["schema"] != SCHEMA
    ):
        raise InvalidInputError("Invalid owner revocation request")
    try:
        identity = UUID(value["grantId"])
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("Invalid control grant identity") from None
    if str(identity) != value["grantId"] or identity.int == 0:
        raise InvalidInputError("Invalid control grant identity")
    if any(
        type(value[key]) is not int or not 1 <= value[key] < 2**53
        for key in ("expectedRevision", "expectedGeneration")
    ):
        raise InvalidInputError("Invalid control grant revision")
    if not isinstance(value["grantSha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["grantSha256"]
    ):
        raise InvalidInputError("An exact reviewed grant digest is required")
    return dict(value)


def _original(connection: Any, workspace_id: UUID, value: dict[str, Any]) -> WorkspaceControlGrant:
    row = connection.execute(
        "SELECT * FROM workspace_control_grant_revisions "
        "WHERE workspace_id=%s AND id=%s AND revision=%s",
        (workspace_id, UUID(value["grantId"]), value["expectedRevision"]),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Control grant unavailable")
    grant = grant_from_row(row)
    if (
        grant.revoked
        or grant.revocation_generation != value["expectedGeneration"]
        or row["grant_sha256"] != value["grantSha256"]
        or content_hash(grant.to_dict()) != value["grantSha256"]
    ):
        raise RevisionConflictError("The reviewed control grant changed")
    return grant


def request_revocation(
    connection: Any, context: WorkspaceContext, request_id: UUID, value: dict[str, Any]
) -> dict[str, Any]:
    _owner(connection, context.workspace_id, context.principal_id)
    value = _request(value)
    if not isinstance(request_id, UUID) or request_id.int == 0:
        raise InvalidInputError("A nonzero request identity is required")
    digest = content_hash(value)
    existing = connection.execute(
        "SELECT id,request_hash,result,principal_id FROM command_results "
        "WHERE workspace_id=%s AND operation=%s AND idempotency_key=%s",
        (context.workspace_id, OPERATION, str(request_id)),
    ).fetchone()
    if existing is not None:
        if (
            existing["id"] != request_id
            or existing["principal_id"] != context.principal_id
            or existing["request_hash"] != digest
            or existing["result"] != value
        ):
            raise RevisionConflictError("Request identity already describes another review")
        return {"requestId": str(existing["id"]), "state": "requested"}
    original = _original(connection, context.workspace_id, value)
    if original.issuer_principal_id != context.principal_id:
        raise ScopeDeniedError("Only the target owner can request revocation")
    current = connection.execute(
        "SELECT revision FROM workspace_control_grant_revisions WHERE workspace_id=%s AND id=%s "
        "ORDER BY revision DESC LIMIT 1",
        (context.workspace_id, original.id),
    ).fetchone()
    if current["revision"] != original.revision:
        raise RevisionConflictError("The reviewed control grant changed")
    from psycopg.types.json import Jsonb

    connection.execute(
        "INSERT INTO command_results(workspace_id,id,operation,idempotency_key,request_hash,"
        "result,principal_id) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (workspace_id,operation,idempotency_key) DO NOTHING",
        (
            context.workspace_id,
            request_id,
            OPERATION,
            str(request_id),
            digest,
            Jsonb(value),
            context.principal_id,
        ),
    )
    saved = connection.execute(
        "SELECT id,request_hash,principal_id FROM command_results "
        "WHERE workspace_id=%s AND operation=%s AND idempotency_key=%s",
        (context.workspace_id, OPERATION, str(request_id)),
    ).fetchone()
    if (
        saved is None
        or saved["id"] != request_id
        or saved["request_hash"] != digest
        or saved["principal_id"] != context.principal_id
    ):
        raise RevisionConflictError("Request identity already describes another review")
    return {"requestId": str(saved["id"]), "state": "requested"}


def apply_revocation(
    connection: Any, *, workspace_id: UUID, owner_principal_id: UUID, request_id: UUID
) -> dict[str, Any]:
    """Use ONLY an explicit native admin_connection and local operator confirmation."""
    _owner(connection, workspace_id, owner_principal_id)
    saved = connection.execute(
        "SELECT * FROM command_results WHERE workspace_id=%s AND id=%s AND operation=%s",
        (workspace_id, request_id, OPERATION),
    ).fetchone()
    if saved is None or saved["principal_id"] != owner_principal_id:
        raise ScopeDeniedError("Owner request unavailable")
    value = _request(saved["result"])
    if saved["request_hash"] != content_hash(value):
        raise InvalidInputError("Owner request integrity check failed")
    original = _original(connection, workspace_id, value)
    if original.issuer_principal_id != owner_principal_id:
        raise ScopeDeniedError("Owner request unavailable")
    revoked = replace(
        original,
        revision=original.revision + 1,
        revocation_generation=original.revocation_generation + 1,
        revoked=True,
    )
    # The existing authority serializes graph writes and accepts identical replay.
    # A changed grant, terminal conflict or wrong DB role still fails closed.
    install_control_grant(connection, revoked)
    return {
        "requestId": str(request_id),
        "grantId": str(revoked.id),
        "revoked": True,
        "revision": revoked.revision,
        "revocationGeneration": revoked.revocation_generation,
        "grantSha256": content_hash(revoked.to_dict()),
    }


def request_state(connection: Any, context: WorkspaceContext, row: Any) -> dict[str, Any]:
    """Never trust journal content as a native completion or a control grant."""
    try:
        value = _request(row["result"])
        if row["request_hash"] != content_hash(value):
            raise ValueError
        original = _original(connection, context.workspace_id, value)
        latest = connection.execute(
            "SELECT * FROM workspace_control_grant_revisions WHERE workspace_id=%s AND id=%s "
            "ORDER BY revision DESC LIMIT 1",
            (context.workspace_id, original.id),
        ).fetchone()
        current = grant_from_row(latest)
        wanted = replace(
            original,
            revision=original.revision + 1,
            revocation_generation=original.revocation_generation + 1,
            revoked=True,
        )
        state = (
            "applied"
            if (current == wanted and latest["grant_sha256"] == content_hash(wanted.to_dict()))
            else "requested"
            if current == original
            else "needs_review"
        )
        return {
            "requestId": str(row["id"]),
            "grantId": str(original.id),
            "expectedRevision": original.revision,
            "state": state,
            "recordedAt": row["recorded_at"].isoformat(),
        }
    except (InvalidInputError, RevisionConflictError, ScopeDeniedError, ValueError, TypeError):
        return {
            "requestId": str(row["id"]),
            "grantId": None,
            "expectedRevision": None,
            "state": "needs_review",
            "recordedAt": row["recorded_at"].isoformat(),
        }
