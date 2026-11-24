"""Explicit operator administration for the native workspace planning slice.

Use an administrative transaction, never an application-request connection.
The existing restricted application roles are read-only under RLS. These
functions do not enroll, install, dispatch or confer transport authenticity.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.remote_workspaces import (
    RemoteNode,
    WorkspaceControlGrant,
    WorkspaceDeployment,
    _text,
    _uuid,
)
from potbot.domain.serialization import content_hash


def _lock(connection: Any, key: str) -> None:
    isolation = connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
    if isolation != "read committed":
        raise InvalidInputError("Remote administration requires READ COMMITTED transactions.")
    connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended("
        "pg_catalog.current_schema() || ':remote-workspaces:' || %s,0))",
        (key,),
    )


def _owner(connection: Any, workspace_id: UUID, principal_id: UUID) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM remote_workspace_owners WHERE workspace_id=%s AND principal_id=%s",
            (workspace_id, principal_id),
        ).fetchone()
        is None
    ):
        raise ScopeDeniedError("Remote workspace ownership requires an explicit operator binding.")


def install_workspace_owner(connection: Any, workspace_id: UUID, principal_id: UUID) -> None:
    _uuid(workspace_id)
    _uuid(principal_id)
    _lock(connection, f"owner:{workspace_id}")
    row = connection.execute(
        "SELECT principal_id FROM remote_workspace_owners WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchone()
    if row is not None:
        if row["principal_id"] != principal_id:
            raise RevisionConflictError(
                "Workspace ownership is already bound to another principal."
            )
        return
    connection.execute(
        "INSERT INTO remote_workspace_owners(workspace_id,principal_id) VALUES (%s,%s)",
        (workspace_id, principal_id),
    )


def register_node(connection: Any, node: RemoteNode) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    _owner(connection, node.workspace_id, node.owner_principal_id)
    _lock(connection, f"node:{node.workspace_id}:{node.id}")
    record = node.to_dict()
    digest = content_hash(record)
    row = connection.execute(
        "SELECT registration_sha256 FROM remote_nodes WHERE workspace_id=%s AND id=%s",
        (node.workspace_id, node.id),
    ).fetchone()
    if row is not None:
        if row["registration_sha256"] != digest:
            raise RevisionConflictError("Node registration changed; explicit rotation is required.")
        return record
    connection.execute(
        "INSERT INTO remote_nodes(workspace_id,id,owner_principal_id,generation,"
        "registration,registration_sha256) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            node.workspace_id,
            node.id,
            node.owner_principal_id,
            node.generation,
            Jsonb(record),
            digest,
        ),
    )
    return record


def register_deployment(connection: Any, deployment: WorkspaceDeployment) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    _owner(connection, deployment.workspace_id, deployment.owner_principal_id)
    _owner(connection, deployment.node_workspace_id, deployment.owner_principal_id)
    _lock(connection, f"deployment:{deployment.workspace_id}")
    if (
        connection.execute(
            "SELECT 1 FROM workspace_sandboxes WHERE workspace_id=%s",
            (deployment.workspace_id,),
        ).fetchone()
        is not None
    ):
        raise RevisionConflictError(
            "An existing sandbox registration cannot become a native deployment."
        )
    record = deployment.to_dict()
    digest = content_hash(record)
    current = connection.execute(
        "SELECT revision,deployment_sha256 FROM native_workspace_deployments "
        "WHERE workspace_id=%s ORDER BY revision DESC LIMIT 1",
        (deployment.workspace_id,),
    ).fetchone()
    if current is not None and current["revision"] == deployment.revision:
        if current["deployment_sha256"] != digest:
            raise RevisionConflictError("Deployment revision already identifies different content.")
        return record
    if deployment.revision != (current["revision"] + 1 if current else 1):
        raise RevisionConflictError("Native deployment revisions must be contiguous.")
    connection.execute(
        "INSERT INTO native_workspace_deployments(workspace_id,revision,owner_principal_id,"
        "node_workspace_id,node_id,execution_mode,root_path,execution_account,environment,"
        "environment_sha256,deployment_sha256) VALUES (%s,%s,%s,%s,%s,'base_os',%s,%s,%s,%s,%s)",
        (
            deployment.workspace_id,
            deployment.revision,
            deployment.owner_principal_id,
            deployment.node_workspace_id,
            deployment.node_id,
            deployment.root_path,
            deployment.execution_account,
            Jsonb(deployment.environment.to_dict()),
            deployment.environment.sha256,
            digest,
        ),
    )
    return record


def creates_control_cycle(source: UUID, target: UUID, edges: Iterable[tuple[UUID, UUID]]) -> bool:
    adjacency: dict[UUID, set[UUID]] = {}
    for origin, destination in edges:
        adjacency.setdefault(origin, set()).add(destination)
    pending, visited = [target], set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node not in visited:
            visited.add(node)
            pending.extend(adjacency.get(node, ()))
    return False


def install_control_grant(connection: Any, grant: WorkspaceControlGrant) -> dict[str, Any]:
    """Append a direct owner-issued grant or terminal revocation, serializing graph writes."""
    _owner(connection, grant.workspace_id, grant.issuer_principal_id)
    # One graph lock across workspaces. The following SQL command obtains a fresh
    # READ COMMITTED snapshot after the wait; concurrent A->B/B->A cannot both pass.
    _lock(connection, "control-graph")
    row = connection.execute(
        "SELECT * FROM workspace_control_grant_revisions WHERE workspace_id=%s AND id=%s "
        "ORDER BY revision DESC LIMIT 1",
        (grant.workspace_id, grant.id),
    ).fetchone()
    record, digest = grant.to_dict(), content_hash(grant.to_dict())
    if row is not None and row["revision"] == grant.revision:
        if row["grant_sha256"] != digest:
            raise RevisionConflictError("Grant revision already identifies different authority.")
        return record
    if row is None:
        if grant.revision != 1 or grant.revocation_generation != 1 or grant.revoked:
            raise RevisionConflictError("A new grant must start at revision and generation one.")
    elif (
        row["revoked"]
        or grant.revision != row["revision"] + 1
        or grant.revocation_generation != row["revocation_generation"] + 1
        or row["source_workspace_id"] != grant.source_workspace_id
        or row["principal_id"] != grant.principal_id
        or row["issuer_principal_id"] != grant.issuer_principal_id
    ):
        raise RevisionConflictError("Grant revision, identity or revocation generation is stale.")
    now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    if not grant.revoked:
        if grant.expires_at <= now:
            raise InvalidInputError("A newly issued control grant must expire in the future.")
        current = connection.execute(
            "SELECT source_workspace_id,workspace_id FROM (SELECT DISTINCT ON (workspace_id,id) "
            "* FROM workspace_control_grant_revisions ORDER BY workspace_id,id,revision DESC) g "
            "WHERE NOT revoked AND expires_at > clock_timestamp() "
            "AND NOT (workspace_id=%s AND id=%s)",
            (grant.workspace_id, grant.id),
        ).fetchall()
        if creates_control_cycle(
            grant.source_workspace_id,
            grant.workspace_id,
            ((item["source_workspace_id"], item["workspace_id"]) for item in current),
        ):
            raise ScopeDeniedError("Workspace control cycles are not supported.")
    connection.execute(
        "INSERT INTO workspace_control_grant_revisions(workspace_id,id,revision,issuer_principal_id,"
        "source_workspace_id,principal_id,permissions,expires_at,revocation_generation,revoked,"
        "grant_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            grant.workspace_id,
            grant.id,
            grant.revision,
            grant.issuer_principal_id,
            grant.source_workspace_id,
            grant.principal_id,
            sorted(grant.permissions),
            grant.expires_at,
            grant.revocation_generation,
            grant.revoked,
            digest,
        ),
    )
    return record


def record_provisioning_plan(
    connection: Any,
    node: RemoteNode,
    plan: dict[str, Any],
    *,
    reviewed_manifest_sha256: str,
    idempotency_key: str,
) -> UUID:
    from psycopg.types.json import Jsonb

    _text(idempotency_key, "Idempotency key", 128)
    _owner(connection, node.workspace_id, node.owner_principal_id)
    manifest = plan.get("manifest")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "workspace.provisioning-manifest/1"
        or manifest.get("node") != node.to_dict()
        or manifest.get("apply_available") is not False
        or content_hash(manifest) != reviewed_manifest_sha256
        or plan.get("manifest_sha256") != reviewed_manifest_sha256
    ):
        raise InvalidInputError("The exact reviewed planning manifest is required.")
    # Reconstruct the allowlisted public contract before storing it. An admin
    # cannot accidentally persist arbitrary added credential fields in a plan.
    from potbot.tools.remote_preflight import HostObservation, ReleasePin, provisioning_manifest

    try:
        expected = provisioning_manifest(
            node,
            HostObservation(**manifest["observation"]),
            tuple(ReleasePin(**item) for item in manifest["releases"]),
            target_workspace_id=UUID(manifest["target_workspace_id"]),
            project_root=manifest["project_root"],
            minimum_disk_bytes=manifest["minimum_disk_bytes"],
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        raise InvalidInputError(
            "Only the public provisioning manifest contract may be stored."
        ) from None
    if expected != plan:
        raise InvalidInputError("Only the public provisioning manifest contract may be stored.")
    _lock(connection, f"attempt:{node.workspace_id}:{idempotency_key}")
    row = connection.execute(
        "SELECT id,manifest_sha256 FROM remote_provisioning_attempts "
        "WHERE workspace_id=%s AND idempotency_key=%s",
        (node.workspace_id, idempotency_key),
    ).fetchone()
    if row is not None:
        if row["manifest_sha256"] != reviewed_manifest_sha256:
            raise RevisionConflictError(
                "This provisioning key already identifies another manifest."
            )
        identity: UUID = row["id"]
        return identity
    identity = uuid4()
    connection.execute(
        "INSERT INTO remote_provisioning_attempts(workspace_id,id,node_id,idempotency_key,manifest,"
        "manifest_sha256) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            node.workspace_id,
            identity,
            node.id,
            idempotency_key,
            Jsonb(manifest),
            reviewed_manifest_sha256,
        ),
    )
    return identity


def current_control_grant(
    connection: Any, workspace_id: UUID, grant_id: UUID
) -> WorkspaceControlGrant:
    """Load current target authority; source-side copies are never admission authority."""
    _uuid(workspace_id)
    _uuid(grant_id)
    row = connection.execute(
        "SELECT * FROM workspace_control_grant_revisions WHERE workspace_id=%s AND id=%s "
        "ORDER BY revision DESC LIMIT 1",
        (workspace_id, grant_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("The requested remote workspace control is not authorized.")
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
