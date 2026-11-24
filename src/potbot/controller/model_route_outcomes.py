"""Observe original jobs; explicitly share output; retain independent verdicts.

This service never invokes models, changes terminal jobs, releases reservations,
replays a lost reply or admits candidates. Hosted output events are supported;
native artifact contracts require a separate typed reader before acceptance.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.operation_workbench import operation_id
from potbot.domain.serialization import content_hash
from potbot.evaluation.model_route_outcomes import VERDICTS, assess_output, summarize_outcomes
from potbot.persistence.database import Database


def _uuid(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("Route outcomes require UUID identities")
    return value


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "potbot-route-terminal-outcome/v1",
        **{
            k: str(row[k])
            for k in ("workspace_id", "decision_id", "policy_id", "principal_id", "job_id")
        },
        "state": row["state"],
        "result": row["result"],
        "output_text": row["output_text"],
        "dispatched_at": row["dispatched_at"].astimezone(UTC).isoformat()
        if row["dispatched_at"]
        else None,
        "finished_at": row["finished_at"].astimezone(UTC).isoformat(),
        "output_contract": row["output_contract"],
        "provider_receipts": row["provider_receipts"],
    }


def _view(row: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(row)
    if content_hash(payload) != row["outcome_sha256"]:
        raise StateMismatchError("Route outcome content differs from its receipt")
    return {
        **payload,
        "sha256": row["outcome_sha256"],
        "assessment": assess_output(
            row["state"],
            row["result"],
            row["output_text"],
            row["output_contract"],
            row["provider_receipts"],
        ),
    }


def _read(conn: Any, context: WorkspaceContext, decision_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM model_route_outcomes WHERE workspace_id=%s AND decision_id=%s",
        (context.workspace_id, _uuid(decision_id)),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Route outcome is unavailable or not explicitly shared")
    return dict(row)


class ModelRouteOutcomeService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def capture(self, context: WorkspaceContext, decision_id: UUID) -> dict[str, Any]:
        """Copy one terminal original job, without demanding an active old route."""
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            decision = conn.execute(
                "SELECT * FROM model_route_decisions WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (context.workspace_id, _uuid(decision_id), context.principal_id),
            ).fetchone()
            if decision is None:
                raise ScopeDeniedError("Original route decision is unavailable")
            conn.execute(
                "SELECT model_route_lock(%s,%s)", (context.workspace_id, decision["policy_id"])
            )
            old = conn.execute(
                "SELECT * FROM model_route_outcomes WHERE workspace_id=%s AND decision_id=%s",
                (context.workspace_id, decision_id),
            ).fetchone()
            if old is not None:
                return _view(dict(old))
            job = conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=%s AND id=%s AND owner_principal_id=%s",
                (context.workspace_id, decision["job_id"], context.principal_id),
            ).fetchone()
            if (
                job is None
                or job["finished_at"] is None
                or job["state"] not in {"succeeded", "failed", "cancelled", "indeterminate"}
            ):
                raise StateMismatchError("Original route job has no terminal outcome")
            if job["job_type"] != "model.generate":
                raise InvalidInputError(
                    "Native artifact outcomes need their qualified typed reader"
                )
            text = conn.execute(
                "SELECT COALESCE(string_agg(data->>'text','' ORDER BY sequence),'') AS text FROM job_events WHERE workspace_id=%s AND job_id=%s AND event_type='job.output'",
                (context.workspace_id, job["id"]),
            ).fetchone()["text"]
            receipts = conn.execute(
                "SELECT h.id,i.status,i.result->>'finish_reason' AS finish_reason FROM hosted_requests h JOIN hosted_invocations i ON i.workspace_id=h.workspace_id AND i.id=h.id WHERE h.workspace_id=%s AND h.job_id=%s ORDER BY h.ordinal",
                (context.workspace_id, job["id"]),
            ).fetchall()
            recipe = conn.execute(
                "SELECT recipe FROM model_route_prompt_recipes WHERE workspace_id=%s AND policy_id=%s",
                (context.workspace_id, decision["policy_id"]),
            ).fetchone()
            row = {
                "workspace_id": context.workspace_id,
                "decision_id": decision_id,
                "policy_id": decision["policy_id"],
                "principal_id": context.principal_id,
                "job_id": job["id"],
                "state": job["state"],
                "result": job["result"],
                "output_text": text,
                "dispatched_at": job["dispatched_at"],
                "finished_at": job["finished_at"],
                "output_contract": recipe["recipe"]["output_contract"] if recipe else None,
                "provider_receipts": [{**dict(v), "id": str(v["id"])} for v in receipts],
            }
            assess_output(
                row["state"], row["result"], text, row["output_contract"], row["provider_receipts"]
            )
            row["outcome_sha256"] = content_hash(_payload(row))
            conn.execute(
                "INSERT INTO model_route_outcomes(workspace_id,decision_id,policy_id,principal_id,job_id,state,result,output_text,dispatched_at,finished_at,output_contract,outcome_sha256,provider_receipts) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    *[
                        row[k]
                        for k in (
                            "workspace_id",
                            "decision_id",
                            "policy_id",
                            "principal_id",
                            "job_id",
                            "state",
                        )
                    ],
                    Jsonb(row["result"]) if row["result"] is not None else None,
                    *[
                        row[k]
                        for k in (
                            "output_text",
                            "dispatched_at",
                            "finished_at",
                            "output_contract",
                            "outcome_sha256",
                        )
                    ],
                    Jsonb(row["provider_receipts"]),
                ),
            )
            return _view(row)

    def inspect(self, context: WorkspaceContext, decision_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return _view(_read(conn, context, decision_id))

    def share(
        self, context: WorkspaceContext, decision_id: UUID, *, expected_outcome_sha256: str
    ) -> dict[str, Any]:
        """Explicitly disclose this exact bounded output to its designated reviewer."""
        operation_id(expected_outcome_sha256)
        with self.database.transaction(context) as conn:
            row = _read(conn, context, decision_id)
            view = _view(row)
            if row["principal_id"] != context.principal_id:
                raise ScopeDeniedError("Only the outcome owner can share its output")
            if view["sha256"] != expected_outcome_sha256:
                raise StateMismatchError("The requested shared outcome changed")
            conn.execute("SELECT model_route_lock(%s,%s)", (context.workspace_id, row["policy_id"]))
            policy = conn.execute(
                "SELECT reviewer_id FROM model_route_policies WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, row["policy_id"]),
            ).fetchone()
            conn.execute(
                "INSERT INTO model_route_outcome_shares(workspace_id,decision_id,principal_id,reviewer_id,outcome_sha256) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    context.workspace_id,
                    decision_id,
                    context.principal_id,
                    policy["reviewer_id"],
                    view["sha256"],
                ),
            )
            return view

    def review(
        self,
        context: WorkspaceContext,
        decision_id: UUID,
        review_id: UUID,
        *,
        verdict: str,
        evidence_sha256: str,
        evaluator_revision: str,
    ) -> dict[str, Any]:
        if (
            verdict not in VERDICTS
            or not isinstance(evaluator_revision, str)
            or not 1 <= len(evaluator_revision.encode()) <= 128
        ):
            raise InvalidInputError(
                "Review requires a supported verdict and bounded evaluator revision"
            )
        operation_id(evidence_sha256)
        _uuid(review_id)
        with self.database.transaction(context) as conn:
            row = _read(conn, context, decision_id)
            conn.execute("SELECT model_route_lock(%s,%s)", (context.workspace_id, row["policy_id"]))
            view = _view(row)
            policy = conn.execute(
                "SELECT reviewer_id FROM model_route_policies WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, row["policy_id"]),
            ).fetchone()
            if policy["reviewer_id"] != context.principal_id:
                raise ScopeDeniedError("Only the designated independent reviewer may assess output")
            assessment = view["assessment"]
            if verdict == "acceptable" and (
                not assessment["completion_confirmed"]
                or row["dispatched_at"] is None
                or (
                    assessment["literal_contract"] is not None
                    and not assessment["literal_contract"]["syntax_valid"]
                )
            ):
                raise StateMismatchError(
                    "Unconfirmed or malformed output cannot receive an acceptable verdict"
                )
            values = (
                context.workspace_id,
                review_id,
                decision_id,
                row["policy_id"],
                context.principal_id,
                verdict,
                view["sha256"],
                evidence_sha256,
                evaluator_revision,
            )
            old = conn.execute(
                "SELECT * FROM model_route_outcome_reviews WHERE workspace_id=%s AND (id=%s OR decision_id=%s)",
                (context.workspace_id, review_id, decision_id),
            ).fetchone()
            keys = (
                "workspace_id",
                "id",
                "decision_id",
                "policy_id",
                "reviewer_id",
                "verdict",
                "outcome_sha256",
                "evidence_sha256",
                "evaluator_revision",
            )
            if old is not None:
                if tuple(old[k] for k in keys) != values:
                    raise StateMismatchError("Independent outcome reviews cannot be replaced")
            else:
                conn.execute(
                    "INSERT INTO model_route_outcome_reviews("
                    + ",".join(keys)
                    + ") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
            return {
                "id": str(review_id),
                "decision_id": str(decision_id),
                "verdict": verdict,
                "outcome_sha256": view["sha256"],
                "evidence_sha256": evidence_sha256,
                "evaluator_revision": evaluator_revision,
                "runtime_qualified": False,
            }

    def summary(self, context: WorkspaceContext, policy_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            policy = conn.execute(
                "SELECT principal_id FROM model_route_policies WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, _uuid(policy_id)),
            ).fetchone()
            if policy is None or policy["principal_id"] != context.principal_id:
                raise ScopeDeniedError(
                    "Only the route owner can inspect its complete outcome inventory"
                )
            count = conn.execute(
                "SELECT count(*) AS n FROM model_route_decisions WHERE workspace_id=%s AND policy_id=%s",
                (context.workspace_id, policy_id),
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT o.decision_id,o.state,r.verdict FROM model_route_outcomes o LEFT JOIN model_route_outcome_reviews r ON r.workspace_id=o.workspace_id AND r.decision_id=o.decision_id WHERE o.workspace_id=%s AND o.policy_id=%s ORDER BY o.decision_id LIMIT 1001",
                (context.workspace_id, policy_id),
            ).fetchall()
            return summarize_outcomes(
                [{**dict(r), "decision_id": str(r["decision_id"])} for r in rows], allocations=count
            )
