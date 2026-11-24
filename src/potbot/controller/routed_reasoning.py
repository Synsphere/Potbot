"""Resume a bounded investigation through ordinary checked reasoning jobs."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from potbot.controller.checked_reasoning import CheckedReasoning
from potbot.controller.model_route_bindings import compile_route_request
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.model_routing import bind_route_request
from potbot.domain.operation_workbench import canonical_uuid, parse_request
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database

ROLES = ("hypothesis_generator", "critic", "synthesizer")
_BLOCKED = {
    "blocked": "check_blocked",
    "check_failed": "check_failed",
    "check_cancelled": "check_cancelled",
    "check_indeterminate": "check_indeterminate",
    "failed": "job_failed",
    "cancelled": "job_cancelled",
    "indeterminate": "job_indeterminate",
}


def _key(run_id: UUID, stage: int) -> str:
    return f"rrun-v1:{run_id}:{stage}"


def _plan(value: object) -> tuple[dict[str, Any], UUID]:
    if not isinstance(value, dict) or set(value) != {"task", "constraints", "evidence", "stages"}:
        raise InvalidInputError("Routed reasoning needs one task, evidence and its stage plan")
    try:
        raw = canonical_json(value)
        if len(raw.encode()) > 32768:
            raise ValueError
        plan: dict[str, Any] = json.loads(raw)
    except (UnicodeError, ValueError):
        raise InvalidInputError("Routed reasoning plan exceeds its JSON bounds") from None
    stages = plan["stages"]
    if not isinstance(stages, list) or len(stages) != len(ROLES):
        raise InvalidInputError("Routed reasoning requires the three ordered roles")
    episodes = set()
    policies = set()
    for role, stage in zip(ROLES, stages, strict=True):
        if (
            not isinstance(stage, dict)
            or set(stage) != {"role", "policy_id", "request"}
            or stage["role"] != role
        ):
            raise InvalidInputError("Stage differs from its declared reasoning role")
        policies.add(canonical_uuid(stage["policy_id"]))
        spec = parse_request(stage["request"])
        stage["request"] = spec.to_dict()
        episodes.add(spec.episode_id)
    if len(episodes) != 1 or len(policies) != len(ROLES):
        raise InvalidInputError("Stages require one shared task and distinct policies")
    return plan, episodes.pop()


def _facts(plan: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {key: plan[key] for key in ("task", "constraints", "evidence")} | {"context": context}


class RoutedReasoning:
    """A finite, resumable hypothesis, critique and synthesis sequence."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.checked = CheckedReasoning(database)

    @staticmethod
    def _read(conn: Any, actor: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        if not isinstance(run_id, UUID):
            raise InvalidInputError("Routed reasoning requires a run UUID")
        row = conn.execute(
            "SELECT r.*, d.id AS decision_id,d.check_job_id,d.job_id,"
            "(e.status='running' AND e.active_contract_id=r.contract_revision_id) AS current_contract "
            "FROM routed_reasoning_runs r JOIN episodes e "
            "ON e.workspace_id=r.workspace_id AND e.id=r.episode_id "
            "LEFT JOIN model_route_decisions d ON d.workspace_id=r.workspace_id "
            "AND d.principal_id=r.principal_id "
            "AND d.policy_id::text=(r.plan->'stages'->r.stage_index->>'policy_id') "
            "AND d.allocation_key='rrun-v1:'||r.id::text||':'||r.stage_index::text "
            "WHERE r.workspace_id=%s AND r.principal_id=%s AND r.id=%s",
            (actor.workspace_id, actor.principal_id, run_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Owned routed reasoning run is unavailable")
        return dict(row)

    def _read_owned(self, actor: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        with self.database.transaction(actor) as conn:
            return self._read(conn, actor, run_id)

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        index = row["stage_index"]
        return {
            "run_id": str(row["id"]),
            "state": row["state"],
            "stage_index": index,
            "phase": ROLES[index] if index < len(ROLES) else None,
            "decision_id": str(row["decision_id"]) if row["decision_id"] else None,
            "check_job_id": str(row["check_job_id"]) if row["check_job_id"] else None,
            "job_id": str(row["job_id"]) if row["job_id"] else None,
            "error_code": row["error_code"],
            "completed": row["results"],
        }

    def inspect(self, actor: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        return self._view(self._read_owned(actor, run_id))

    def start(self, actor: WorkspaceContext, run_id: UUID, plan: object) -> dict[str, Any]:
        """Freeze a plan under a running task and an explicit idempotency identity."""
        from psycopg.types.json import Jsonb

        if not isinstance(run_id, UUID):
            raise InvalidInputError("Routed reasoning requires a run UUID")
        selected, episode_id = _plan(plan)
        digest = content_hash(selected)
        with self.database.transaction(actor) as conn:
            # Serialize repeated starts even before a row exists. The task and
            # route triggers retain responsibility for actual admission.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{actor.workspace_id}/routed-reasoning/{run_id}",),
            )
            prior = conn.execute(
                "SELECT plan_sha256 FROM routed_reasoning_runs "
                "WHERE workspace_id=%s AND principal_id=%s AND id=%s",
                (actor.workspace_id, actor.principal_id, run_id),
            ).fetchone()
            if prior is not None:
                if prior["plan_sha256"] != digest:
                    raise StateMismatchError("A reasoning run cannot be rebound to a new plan")
                return self._view(self._read(conn, actor, run_id))

            for stage in selected["stages"]:
                policy = conn.execute(
                    "SELECT p.manifest,r.recipe FROM model_route_policies p "
                    "JOIN model_route_prompt_recipes r "
                    "ON r.workspace_id=p.workspace_id AND r.policy_id=p.id "
                    "WHERE p.workspace_id=%s AND p.principal_id=%s AND p.id=%s "
                    "AND p.manifest->>'role'='reasoning'",
                    (actor.workspace_id, actor.principal_id, stage["policy_id"]),
                ).fetchone()
                if policy is None:
                    raise ScopeDeniedError("Each stage requires an owned compiled reasoning policy")
                if policy["recipe"]["role"] != stage["role"]:
                    raise StateMismatchError("Stage role differs from its policy recipe")
                exact, _ = compile_route_request(
                    policy["recipe"], _facts(selected, {}), stage["request"], fill=True
                )
                bind_route_request(policy["manifest"]["baseline"], exact)
            created = conn.execute(
                "INSERT INTO routed_reasoning_runs "
                "(workspace_id,id,principal_id,episode_id,contract_revision_id,plan,plan_sha256) "
                "SELECT e.workspace_id,%s,%s,e.id,e.active_contract_id,%s,%s FROM episodes e "
                "WHERE e.workspace_id=%s AND e.id=%s AND e.status='running' "
                "AND e.active_contract_id IS NOT NULL ON CONFLICT DO NOTHING RETURNING id",
                (
                    run_id,
                    actor.principal_id,
                    Jsonb(selected),
                    digest,
                    actor.workspace_id,
                    episode_id,
                ),
            ).fetchone()
            if created is None:
                raise ScopeDeniedError("The running task or requested run identity is unavailable")
            return self._view(self._read(conn, actor, run_id))

    def _verified(self, actor: WorkspaceContext, receipt: dict[str, Any]) -> dict[str, Any]:
        result = self.checked.result(actor, UUID(receipt["decision_id"]))
        if result["output_sha256"] != receipt["output_sha256"]:
            raise StateMismatchError("The original reasoning result differs from its receipt")
        return result

    def _next_facts(self, actor: WorkspaceContext, row: dict[str, Any]) -> dict[str, Any]:
        receipts = row["results"]
        context: dict[str, Any] = {}
        if receipts:
            hypotheses = self._verified(actor, receipts[0])["parsed"]
            context["branches"] = [
                {"node_id": f"hypothesis-{index}", **proposal}
                for index, proposal in enumerate(hypotheses, start=1)
            ]
            context["stage_outputs"] = [item["output_sha256"] for item in receipts]
        if len(receipts) == 2:
            context["critiques"] = self._verified(actor, receipts[1])["parsed"]
        return _facts(row["plan"], context)

    def _check_facts(self, actor: WorkspaceContext, row: dict[str, Any], decision_id: UUID) -> None:
        facts = self._next_facts(actor, row)
        with self.database.transaction(actor) as conn:
            retained = conn.execute(
                "SELECT facts FROM model_route_compiled_prompts "
                "WHERE workspace_id=%s AND principal_id=%s AND decision_id=%s",
                (actor.workspace_id, actor.principal_id, decision_id),
            ).fetchone()
        if retained is None or canonical_json(retained["facts"]) != canonical_json(facts):
            raise StateMismatchError("Compiled facts differ from the investigation checkpoint")

    def _close(self, actor: WorkspaceContext, row: dict[str, Any], error: str) -> dict[str, Any]:
        with self.database.transaction(actor) as conn:
            conn.execute(
                "UPDATE routed_reasoning_runs SET state='blocked',error_code=%s "
                "WHERE workspace_id=%s AND principal_id=%s AND id=%s "
                "AND state='active' AND stage_index=%s",
                (error, actor.workspace_id, actor.principal_id, row["id"], row["stage_index"]),
            )
            return self._view(self._read(conn, actor, row["id"]))

    def _adopt(
        self, actor: WorkspaceContext, row: dict[str, Any], answer: dict[str, Any]
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        receipt = {"decision_id": answer["decision_id"], "output_sha256": answer["output_sha256"]}
        next_stage = row["stage_index"] + 1
        with self.database.transaction(actor) as conn:
            conn.execute(
                "UPDATE routed_reasoning_runs SET results=results||%s,stage_index=%s,state=%s "
                "WHERE workspace_id=%s AND principal_id=%s AND id=%s "
                "AND state='active' AND stage_index=%s",
                (
                    Jsonb([receipt]),
                    next_stage,
                    "completed" if next_stage == len(ROLES) else "active",
                    actor.workspace_id,
                    actor.principal_id,
                    row["id"],
                    row["stage_index"],
                ),
            )
            return self._view(self._read(conn, actor, row["id"]))

    def advance(self, actor: WorkspaceContext, run_id: UUID, *, setup_token: str) -> dict[str, Any]:
        """Take one queue/checkpoint step. External work remains with the job worker."""
        row = self._read_owned(actor, run_id)
        if row["state"] != "active":
            return self._view(row)
        if not row["current_contract"]:
            return self._close(actor, row, "contract_changed")
        stage_index = row["stage_index"]
        stage = row["plan"]["stages"][stage_index]
        try:
            decision_id = row["decision_id"]
            status = self.checked.status(actor, decision_id) if decision_id else None
            if status is None or status["state"] == "allocated":
                self.checked.begin(
                    actor,
                    UUID(stage["policy_id"]),
                    _key(run_id, stage_index),
                    role=stage["role"],
                    request=stage["request"],
                    facts=self._next_facts(actor, row),
                    setup_token=setup_token,
                )
            elif status["state"] == "checked" and decision_id is not None:
                self._check_facts(actor, row, decision_id)
                self.checked.launch(actor, decision_id, setup_token=setup_token)
            elif status["state"] == "succeeded" and decision_id is not None:
                try:
                    self._check_facts(actor, row, decision_id)
                    answer = self.checked.result(actor, decision_id)
                except (InvalidInputError, StateMismatchError, ScopeDeniedError):
                    return self._close(actor, row, "invalid_result")
                return self._adopt(actor, row, answer)
            elif status["state"] in _BLOCKED:
                return self._close(actor, row, _BLOCKED[status["state"]])
        except (InvalidInputError, RevisionConflictError, ScopeDeniedError, StateMismatchError):
            # Reconcile a concurrent advance or interrupt against durable state.
            # Never try a second key or authorize a different route here.
            current = self._read_owned(actor, run_id)
            if current["state"] != "active" or any(
                current[field] != row[field] for field in ("stage_index", "check_job_id", "job_id")
            ):
                return self._view(current)
            return self._close(
                actor,
                row,
                "route_unavailable" if current["current_contract"] else "contract_changed",
            )
        return self.inspect(actor, run_id)

    def interrupt(self, actor: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        """Commit the run fence before asking the existing queue to cancel."""
        with self.database.transaction(actor) as conn:
            self._read(conn, actor, run_id)
            conn.execute(
                "UPDATE routed_reasoning_runs SET state='interrupted' "
                "WHERE workspace_id=%s AND principal_id=%s AND id=%s AND state='active'",
                (actor.workspace_id, actor.principal_id, run_id),
            )
            row = self._read(conn, actor, run_id)
        if row["state"] == "interrupted":
            for job_id in (row["check_job_id"], row["job_id"]):
                if job_id is not None:
                    self.checked.jobs.cancel(actor, job_id, f"routed-reasoning-cancel:{job_id}")
        return self.inspect(actor, run_id)

    def result(self, actor: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        row = self._read_owned(actor, run_id)
        if row["state"] != "completed":
            raise StateMismatchError("A confirmed completed routed reasoning run is required")
        return self._verified(actor, row["results"][-1])
