"""Explicit, resumable controller steps for one routed reasoning invocation."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from potbot.controller.model_routing import ModelRoutingService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.jobs import ACTIVE_JOB_STATES, JobState
from potbot.persistence.database import Database


class CheckedReasoning:
    """Use the ordinary workbench and job worker for every external effect."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.routing = ModelRoutingService(database)
        self.jobs = self.routing.workbench.jobs

    def _recipe_role(self, actor: WorkspaceContext, policy_id: UUID) -> str:
        with self.database.transaction(actor) as conn:
            row = conn.execute(
                "SELECT r.recipe->>'role' AS role FROM model_route_prompt_recipes r "
                "JOIN model_route_policies p ON p.workspace_id=r.workspace_id AND p.id=r.policy_id "
                "WHERE p.workspace_id=%s AND p.id=%s AND p.principal_id=%s "
                "AND p.manifest->>'role'='reasoning'",
                (actor.workspace_id, policy_id, actor.principal_id),
            ).fetchone()
        if row is None:
            raise ScopeDeniedError("An owned compiled reasoning policy is required")
        return str(row["role"])

    def begin(
        self,
        actor: WorkspaceContext,
        policy_id: UUID,
        key: str,
        *,
        role: str,
        request: object,
        facts: object,
        setup_token: str,
    ) -> dict[str, Any]:
        """Freeze one call and queue its operation check under a stable key."""
        if not isinstance(policy_id, UUID) or not isinstance(role, str):
            raise InvalidInputError("Reasoning requires a policy UUID and declared role")
        if self._recipe_role(actor, policy_id) != role:
            raise StateMismatchError("Reasoning role differs from the frozen recipe")
        decision = self.routing.allocate_prompted(actor, policy_id, key, request, facts)
        identity = UUID(decision["id"])
        if decision["check_job_id"] is None:
            self.routing.check(
                actor, identity, f"reasoning-check:{identity}", setup_token=setup_token
            )
        return self.status(actor, identity)

    def status(self, actor: WorkspaceContext, decision_id: UUID) -> dict[str, Any]:
        """Inspect durable check or execution state without queuing work."""
        decision = self.routing.decision(actor, decision_id)
        role = self._recipe_role(actor, UUID(decision["policy_id"]))
        check_id, job_id = decision["check_job_id"], decision["job_id"]
        if job_id is not None:
            job = decision["job"]
            state = job["state"]
        elif check_id is not None:
            check = self.jobs.inspect(actor, UUID(check_id))
            if check["state"] in ACTIVE_JOB_STATES or check["state"] == JobState.QUEUED:
                state = "checking"
            elif check["state"] == JobState.SUCCEEDED:
                report = (check["result"] or {}).get("operation_check", {})
                state = "checked" if report.get("eligible") is True else "blocked"
            else:
                state = "check_" + check["state"]
        else:
            state = "allocated"
        return {
            "decision_id": str(decision_id),
            "role": role,
            "check_job_id": check_id,
            "job_id": job_id,
            "state": state,
        }

    def launch(
        self,
        actor: WorkspaceContext,
        decision_id: UUID,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        """Queue the first execution of this checked decision or recover its job."""
        status = self.status(actor, decision_id)
        if status["job_id"] is not None:
            return status
        if status["state"] != "checked":
            raise StateMismatchError("A completed eligible reasoning check is required")
        decision = self.routing.decision(actor, decision_id)
        policy = self.routing.inspect(actor, UUID(decision["policy_id"]))
        self.routing.workbench.launch(
            actor,
            policy["manifest"]["baseline"]["operation_id"],
            UUID(status["check_job_id"]),
            decision["request"],
            f"reasoning-launch:{decision_id}",
            setup_token=setup_token,
        )
        return self.status(actor, decision_id)

    def result(self, actor: WorkspaceContext, decision_id: UUID) -> dict[str, Any]:
        """Read the original completed answer through the shared role validator."""
        self._recipe_role(actor, UUID(self.routing.decision(actor, decision_id)["policy_id"]))
        return self.routing.reasoning_result(actor, decision_id)
