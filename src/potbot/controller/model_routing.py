"""Persistent shadow selection through the ordinary checked workbench.

No candidate invocation, permission, retry, scheduler or budget authority.
Independent baseline acceptance must be supplied by the designated authenticated
reviewer. Public synthetic experiments alone cannot supply that acceptance.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.jobs import JobService
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.model_routing import bind_route_request, route_target
from potbot.domain.operation_workbench import operation_id, parse_request
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database


def _uuid(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("Model routing requires UUID identities")
    return value


class ModelRoutingService:
    """Immutable role/atomic-kind scope and single-use, exact baseline requests."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.workbench = OperationWorkbench(database)

    def create(
        self,
        context: WorkspaceContext,
        policy_id: UUID,
        *,
        reviewer_id: UUID,
        role: str,
        atomic_kind: str,
        baseline: object,
        candidate: object,
        prompt_contract_sha256: str,
        evaluation_plan_sha256: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        _uuid(policy_id)
        _uuid(reviewer_id)
        if reviewer_id == context.principal_id:
            raise ScopeDeniedError("A route owner cannot independently accept its baseline")
        if (
            role not in {"reasoning", "coding"}
            or not isinstance(atomic_kind, str)
            or not 1 <= len(atomic_kind) <= 64
        ):
            raise InvalidInputError("A route requires a bounded role and atomic kind")
        manifest: dict[str, Any] = {
            "schema": "potbot-model-shadow-route/v1",
            "role": role,
            "atomic_kind": atomic_kind,
            "baseline": route_target(baseline),
            "candidate": route_target(candidate),
            "prompt_contract_sha256": operation_id(prompt_contract_sha256),
            "evaluation_plan_sha256": operation_id(evaluation_plan_sha256),
        }
        for target in (manifest["baseline"], manifest["candidate"]):
            choices = target["executor"]["selected_models"]
            if choices and choices[0]["role"] != role:
                raise InvalidInputError("Route scope differs from the selected model role")
        digest = content_hash(manifest)
        with self.database.transaction(context) as conn:
            conn.execute("SELECT model_route_lock(%s,%s)", (context.workspace_id, policy_id))
            existing = conn.execute(
                "SELECT * FROM model_route_policies WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, policy_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["principal_id"] != context.principal_id
                    or existing["manifest"] != manifest
                    or existing["reviewer_id"] != reviewer_id
                ):
                    raise StateMismatchError(
                        "A route identity cannot be reused for different content"
                    )
            else:
                conn.execute(
                    "INSERT INTO model_route_policies(workspace_id,id,principal_id,reviewer_id,manifest,manifest_sha256) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        policy_id,
                        context.principal_id,
                        reviewer_id,
                        Jsonb(manifest),
                        digest,
                    ),
                )
        return self.inspect(context, policy_id)

    def inspect(self, context: WorkspaceContext, policy_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM model_route_policies WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, _uuid(policy_id)),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Model route is unavailable")
            accepted = conn.execute(
                "SELECT model_route_current(%s,%s) AS id", (context.workspace_id, policy_id)
            ).fetchone()["id"]
            return {
                "id": str(row["id"]),
                "stage": row["stage"],
                "epoch": row["epoch"],
                "manifest": row["manifest"],
                "manifest_sha256": row["manifest_sha256"],
                "reviewer_id": str(row["reviewer_id"]),
                "baseline_review_id": str(accepted) if accepted else None,
                "candidate_execution_enabled": False,
            }

    def review_baseline(
        self,
        context: WorkspaceContext,
        policy_id: UUID,
        review_id: UUID,
        *,
        accepted: bool,
        evidence_sha256: str,
        valid_until: datetime,
    ) -> dict[str, Any]:
        if (
            type(accepted) is not bool
            or not isinstance(valid_until, datetime)
            or valid_until.utcoffset() is None
        ):
            raise InvalidInputError(
                "Independent review requires a verdict and timezone-aware expiry"
            )
        operation_id(evidence_sha256)
        _uuid(policy_id)
        _uuid(review_id)
        with self.database.transaction(context) as conn:
            conn.execute("SELECT model_route_lock(%s,%s)", (context.workspace_id, policy_id))
            row = conn.execute(
                "SELECT * FROM model_route_reviews WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, review_id),
            ).fetchone()
            if row is not None:
                if (
                    row["policy_id"],
                    row["reviewer_id"],
                    row["accepted"],
                    row["evidence_sha256"],
                    row["valid_until"],
                ) != (policy_id, context.principal_id, accepted, evidence_sha256, valid_until):
                    raise StateMismatchError("An independent review cannot be replaced")
            else:
                conn.execute(
                    "INSERT INTO model_route_reviews(workspace_id,id,policy_id,reviewer_id,revision,accepted,evidence_sha256,valid_until) VALUES(%s,%s,%s,%s,1,%s,%s,%s)",
                    (
                        context.workspace_id,
                        review_id,
                        policy_id,
                        context.principal_id,
                        accepted,
                        evidence_sha256,
                        valid_until,
                    ),
                )
        return self.inspect(context, policy_id)

    def transition(
        self, context: WorkspaceContext, policy_id: UUID, *, expected_epoch: int, stage: str
    ) -> dict[str, Any]:
        if (
            stage not in {"shadow", "blocked", "disabled"}
            or type(expected_epoch) is not int
            or expected_epoch < 1
        ):
            raise InvalidInputError("Only the implemented shadow lifecycle can be activated")
        with self.database.transaction(context) as conn:
            conn.execute("SELECT model_route_lock(%s,%s)", (context.workspace_id, _uuid(policy_id)))
            row = conn.execute(
                "UPDATE model_route_policies SET stage=%s,epoch=epoch+1 WHERE workspace_id=%s AND id=%s AND principal_id=%s AND epoch=%s RETURNING id",
                (stage, context.workspace_id, policy_id, context.principal_id, expected_epoch),
            ).fetchone()
            if row is None:
                raise StateMismatchError("The owned route epoch changed")
        return self.inspect(context, policy_id)

    def allocate(
        self,
        context: WorkspaceContext,
        policy_id: UUID,
        allocation_key: str,
        request: object,
        *,
        prompt_facts: object = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        spec = parse_request(request)
        if not isinstance(allocation_key, str) or not 1 <= len(allocation_key.encode()) <= 160:
            raise InvalidInputError("Model route allocation key exceeds its bound")
        with self.database.transaction(context) as conn:
            conn.execute("SELECT model_route_lock(%s,%s)", (context.workspace_id, _uuid(policy_id)))
            policy = conn.execute(
                "SELECT * FROM model_route_policies WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (context.workspace_id, policy_id, context.principal_id),
            ).fetchone()
            if policy is None:
                raise ScopeDeniedError("Model route is unavailable")
            bind_route_request(policy["manifest"]["baseline"], request)
            from potbot.controller.model_route_bindings import prepare_compilation

            compilation = prepare_compilation(conn, context, policy, request, prompt_facts)
            row = conn.execute(
                "SELECT * FROM model_route_decisions WHERE workspace_id=%s AND policy_id=%s AND allocation_key=%s",
                (context.workspace_id, policy_id, allocation_key),
            ).fetchone()
            if row is not None:
                if row["request"] != spec.to_dict():
                    raise StateMismatchError("An allocation cannot be rebound to another request")
            else:
                row = conn.execute(
                    "INSERT INTO model_route_decisions(workspace_id,id,policy_id,principal_id,epoch,review_id,allocation_key,request,episode_id,contract_revision_id,expires_at) SELECT %s,%s,%s,%s,%s,model_route_current(%s,%s),%s,%s,%s,e.active_contract_id,clock_timestamp()+interval '5 minutes' FROM episodes e WHERE e.workspace_id=%s AND e.id=%s RETURNING *",
                    (
                        context.workspace_id,
                        uuid4(),
                        policy_id,
                        context.principal_id,
                        policy["epoch"],
                        context.workspace_id,
                        policy_id,
                        allocation_key,
                        Jsonb(spec.to_dict()),
                        spec.episode_id,
                        context.workspace_id,
                        spec.episode_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ScopeDeniedError("The task is unavailable")
            from potbot.controller.model_route_bindings import persist_compilation

            persist_compilation(conn, context, row, compilation)
            return self._decision(row)

    def configure_prompt(
        self, context: WorkspaceContext, policy_id: UUID, recipe: object
    ) -> dict[str, Any]:
        from potbot.controller.model_route_bindings import configure_recipe

        return configure_recipe(self.database, context, _uuid(policy_id), recipe)

    def allocate_prompted(
        self,
        context: WorkspaceContext,
        policy_id: UUID,
        allocation_key: str,
        request: object,
        facts: object,
    ) -> dict[str, Any]:
        from potbot.controller.model_route_bindings import compile_route_request

        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT recipe FROM model_route_prompt_recipes WHERE workspace_id=%s AND policy_id=%s",
                (context.workspace_id, _uuid(policy_id)),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Frozen route prompt recipe is unavailable")
        exact, _identity = compile_route_request(row["recipe"], facts, request, fill=True)
        return self.allocate(context, policy_id, allocation_key, exact, prompt_facts=facts)

    @staticmethod
    def _decision(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "policy_id": str(row["policy_id"]),
            "epoch": row["epoch"],
            "selected": "baseline",
            "candidate_invoked": False,
            "request": json.loads(canonical_json(row["request"])),
            "check_job_id": str(row["check_job_id"]) if row["check_job_id"] else None,
            "job_id": str(row["job_id"]) if row["job_id"] else None,
        }

    def decision(self, context: WorkspaceContext, decision_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM model_route_decisions WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, _uuid(decision_id)),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Model route decision is unavailable")
            result = self._decision(row)
        if row["job_id"] is not None:
            result["job"] = JobService(self.database).inspect(context, row["job_id"])
        return result

    def check(
        self,
        context: WorkspaceContext,
        decision_id: UUID,
        key: str,
        *,
        setup_token: str,
        **bindings: Any,
    ) -> dict[str, Any]:
        decision = self.decision(context, decision_id)
        policy = self.inspect(context, UUID(decision["policy_id"]))
        return self.workbench.check(
            context,
            policy["manifest"]["baseline"]["operation_id"],
            decision["request"],
            key,
            setup_token=setup_token,
            routing_decision_id=decision_id,
            **bindings,
        )
