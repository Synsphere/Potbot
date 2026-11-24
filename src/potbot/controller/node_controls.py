"""Owner/setup-authorized node intents and native runtime readback.

Only the native supervisor writes runtime facts. An application journal row is
neither authority to restart a process nor evidence that a restart completed.
"""

from __future__ import annotations

import hmac
from typing import Any
from uuid import UUID

from potbot.controller.live_configuration import is_owner, require_owner
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.domain.live_controls import NODE_OPERATION, SCHEMA, node_review, proof
from potbot.persistence.database import Database, idempotent_command


def lock_node(connection: Any, workspace_id: UUID, node_id: UUID) -> None:
    connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
        ("node-control:" + str(workspace_id) + ":" + str(node_id),),
    )


def pending(connection: Any, workspace_id: UUID, node_id: UUID) -> list[dict[str, Any]]:
    return list(
        connection.execute(
            "SELECT c.* FROM command_results c WHERE c.workspace_id=%s AND c.operation=%s "
            "AND c.result->'review'->>'nodeId'=%s AND c.principal_id=(SELECT principal_id "
            "FROM node_runtime_controls WHERE workspace_id=c.workspace_id AND node_id=%s) "
            "AND NOT EXISTS "
            "(SELECT 1 FROM node_control_events e WHERE e.workspace_id=c.workspace_id "
            "AND e.request_id::text=c.idempotency_key) ORDER BY c.recorded_at,c.id LIMIT 20",
            (workspace_id, NODE_OPERATION, str(node_id), node_id),
        ).fetchall()
    )


def admission_allowed(connection: Any, context: WorkspaceContext, worker_id: str) -> bool:
    runtime = connection.execute(
        "SELECT *,heartbeat_at>clock_timestamp()-interval '15 seconds' AS fresh "
        "FROM node_runtime_controls WHERE workspace_id=%s AND worker_id=%s",
        (context.workspace_id, worker_id),
    ).fetchone()
    if runtime is None or runtime["principal_id"] != context.principal_id:
        raise ScopeDeniedError("The managed node worker identity is unavailable")
    lock_node(connection, context.workspace_id, runtime["node_id"])
    runtime = connection.execute(
        "SELECT phase,desired,heartbeat_at>clock_timestamp()-interval '15 seconds' AS fresh "
        "FROM node_runtime_controls WHERE workspace_id=%s AND worker_id=%s FOR SHARE",
        (context.workspace_id, worker_id),
    ).fetchone()
    return bool(
        runtime
        and runtime["fresh"]
        and runtime["phase"] == "ready"
        and runtime["desired"] == "active"
        and not pending(connection, context.workspace_id, UUID(worker_id[12:44]))
    )


def unsettled(connection: Any, workspace_id: UUID, node_id: UUID, incarnation: UUID) -> int:
    # Retain the job link even after finish/expiry clears lease_owner. In
    # particular, an expired lease is indeterminate, never a drained receipt.
    jobs = connection.execute(
        "SELECT count(*) AS n FROM node_job_admissions a JOIN jobs j "
        "ON (j.workspace_id,j.id)=(a.workspace_id,a.job_id) "
        "WHERE a.workspace_id=%s AND a.node_id=%s AND a.incarnation=%s "
        "AND j.state NOT IN ('succeeded','failed','cancelled')",
        (workspace_id, node_id, incarnation),
    ).fetchone()["n"]
    holds = connection.execute(
        "SELECT count(*) AS n FROM budget_reservations b WHERE b.workspace_id=%s "
        "AND b.status IN ('reserved','indeterminate') AND EXISTS "
        "(SELECT 1 FROM node_job_admissions a JOIN jobs j ON "
        "(j.workspace_id,j.id)=(a.workspace_id,a.job_id) "
        "WHERE a.workspace_id=b.workspace_id AND j.episode_id=b.episode_id "
        "AND a.node_id=%s AND a.incarnation=%s)",
        (workspace_id, node_id, incarnation),
    ).fetchone()["n"]
    atomic = connection.execute(
        "SELECT count(*) AS n FROM atomic_graph_resource_holds h WHERE h.workspace_id=%s "
        "AND h.active AND EXISTS (SELECT 1 FROM node_job_admissions a "
        "WHERE (a.workspace_id,a.job_id)=(h.workspace_id,h.job_id) "
        "AND a.node_id=%s AND a.incarnation=%s)",
        (workspace_id, node_id, incarnation),
    ).fetchone()["n"]
    return int(jobs + holds + atomic)


class NodeControls:
    def __init__(self, database: Database) -> None:
        self.database = database

    def read(self, context: WorkspaceContext, *, setup_available: bool) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            owner = is_owner(connection, context)
            rows = connection.execute(
                "SELECT n.id,n.generation,n.registration,c.phase,c.desired,c.revision,c.incarnation,"
                "c.heartbeat_at,c.registration_generation,c.principal_id,"
                "c.heartbeat_at>clock_timestamp()-interval '15 seconds' AS operator_fresh "
                "FROM remote_nodes n LEFT JOIN node_runtime_controls c ON "
                "(c.workspace_id,c.node_id)=(n.workspace_id,n.id) "
                "WHERE n.workspace_id=%s ORDER BY n.id LIMIT 101",
                (context.workspace_id,),
            ).fetchall()
            requests = connection.execute(
                "SELECT c.idempotency_key,c.result,e.stage,e.reason,e.incarnation,e.revision "
                "FROM command_results c LEFT JOIN LATERAL "
                "(SELECT * FROM node_control_events x WHERE x.workspace_id=c.workspace_id "
                "AND x.request_id::text=c.idempotency_key "
                "ORDER BY (x.stage='accepted'),x.created_at DESC LIMIT 1) e ON true "
                "WHERE c.workspace_id=%s AND c.principal_id=%s AND c.operation=%s "
                "ORDER BY c.recorded_at DESC,c.id LIMIT 101",
                (context.workspace_id, context.principal_id, NODE_OPERATION),
            ).fetchall()
            nodes = []
            for row in rows[:100]:
                fresh = bool(row["operator_fresh"])
                matched = row["registration_generation"] == row["generation"]
                nodes.append(
                    {
                        "id": str(row["id"]),
                        "label": row["registration"]["hostname"],
                        "generation": row["generation"],
                        "managed": row["incarnation"] is not None,
                        "revision": row["revision"],
                        "incarnation": str(row["incarnation"]) if row["incarnation"] else None,
                        "phase": row["phase"] or "unmanaged",
                        "desired": row["desired"],
                        "operatorFresh": fresh,
                        "canManage": bool(
                            owner
                            and setup_available
                            and fresh
                            and matched
                            and row["phase"] in {"ready", "drained"}
                            and not pending(connection, context.workspace_id, row["id"])
                        ),
                        "unsettled": unsettled(
                            connection, context.workspace_id, row["id"], row["incarnation"]
                        )
                        if row["incarnation"]
                        else None,
                    }
                )
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "nodes": nodes,
            "requests": [self._request_view(row) for row in requests[:100]],
            "recordsTruncated": len(rows) > 100 or len(requests) > 100,
            "scope": "potbot_worker_service",
        }

    @staticmethod
    def _request_view(row: dict[str, Any]) -> dict[str, Any]:
        review = node_review(row["result"]["review"])
        return {
            "requestId": review["requestId"],
            "nodeId": review["nodeId"],
            "review": review,
            "action": review["action"],
            "state": row["stage"] or "requested",
            "reason": row["reason"] or "awaiting_native_operator",
            "expectedRevision": review["expectedRevision"],
            "expectedIncarnation": review["expectedIncarnation"],
            "observedIncarnation": str(row["incarnation"]) if row["incarnation"] else None,
            "observedRevision": row["revision"],
        }

    def request(self, context: WorkspaceContext, value: Any, setup_token: str) -> dict[str, Any]:
        review = node_review(value)
        node_id = UUID(review["nodeId"])
        with self.database.transaction(context) as connection:
            require_owner(connection, context)
            lock_node(connection, context.workspace_id, node_id)

            def create() -> dict[str, Any]:
                runtime = connection.execute(
                    "SELECT c.*,n.generation,c.heartbeat_at>clock_timestamp()-interval '15 seconds' AS fresh "
                    "FROM node_runtime_controls c JOIN remote_nodes n "
                    "ON (n.workspace_id,n.id)=(c.workspace_id,c.node_id) "
                    "WHERE c.workspace_id=%s AND c.node_id=%s FOR SHARE OF c",
                    (context.workspace_id, node_id),
                ).fetchone()
                if (
                    runtime is None
                    or runtime["principal_id"] != context.principal_id
                    or runtime["generation"] != review["expectedGeneration"]
                    or runtime["registration_generation"] != review["expectedGeneration"]
                    or runtime["revision"] != review["expectedRevision"]
                    or str(runtime["incarnation"]) != review["expectedIncarnation"]
                    or runtime["phase"] not in {"ready", "drained"}
                    or not runtime["fresh"]
                    or pending(connection, context.workspace_id, node_id)
                ):
                    raise RevisionConflictError(
                        "Node service changed or has a pending review; refresh"
                    )
                return {
                    "schema": SCHEMA,
                    "workspaceId": str(context.workspace_id),
                    "principalId": str(context.principal_id),
                    "review": review,
                    "proof": proof(context.workspace_id, context.principal_id, review, setup_token),
                }

            result = idempotent_command(
                connection, context, NODE_OPERATION, review["requestId"], review, create
            )
            if result.get("principalId") != str(context.principal_id):
                raise ScopeDeniedError("The node review belongs to another principal")
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "review": node_review(result["review"]),
            "state": "requested",
        }


def authenticate_intent(
    row: dict[str, Any], workspace_id: UUID, principal_id: UUID, token: str
) -> dict[str, Any]:
    result = row["result"]
    review = node_review(result["review"])
    if (
        row["principal_id"] != principal_id
        or row["idempotency_key"] != review["requestId"]
        or result.get("workspaceId") != str(workspace_id)
        or result.get("principalId") != str(principal_id)
        or not hmac.compare_digest(
            str(result.get("proof", "")), proof(workspace_id, principal_id, review, token)
        )
    ):
        raise ScopeDeniedError("The native node review is not authenticated")
    return review
