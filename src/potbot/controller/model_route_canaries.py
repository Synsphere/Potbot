"""Explicit canary selection and rollback through normal checked model routes."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from potbot.controller.model_route_bindings import compile_route_request
from potbot.controller.model_routing import ModelRoutingService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.operation_workbench import parse_request
from potbot.domain.serialization import content_hash
from potbot.persistence.database import Database


class ModelRouteCanaries:
    """Own a sampling window. Checks, dispatch, budget and outcomes stay with jobs."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.routing = ModelRoutingService(database)

    @staticmethod
    def _read(conn: Any, actor: WorkspaceContext, canary_id: UUID) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM model_route_canaries WHERE workspace_id=%s AND id=%s AND principal_id=%s",
            (actor.workspace_id, canary_id, actor.principal_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Owned canary is unavailable")
        return dict(row)

    def start(
        self,
        actor: WorkspaceContext,
        canary_id: UUID,
        *,
        baseline_id: UUID,
        candidate_id: UUID,
        sample_bps: int,
        max_candidate_allocations: int,
        valid_until: datetime,
    ) -> dict[str, Any]:
        if (
            not all(isinstance(key, UUID) for key in (canary_id, baseline_id, candidate_id))
            or baseline_id == candidate_id
            or type(sample_bps) is not int
            or not 1 <= sample_bps <= 1000
            or type(max_candidate_allocations) is not int
            or not 1 <= max_candidate_allocations <= 100
            or not isinstance(valid_until, datetime)
            or valid_until.utcoffset() is None
        ):
            raise InvalidInputError(
                "Canary needs two policies and bounded sampling, count and expiry"
            )
        with self.database.transaction(actor) as conn:
            conn.execute("SELECT model_route_lock(%s,%s)", (actor.workspace_id, candidate_id))
            conn.execute("SELECT model_route_lock(%s,%s)", (actor.workspace_id, baseline_id))
            row = conn.execute(
                "SELECT * FROM model_route_canaries WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (actor.workspace_id, canary_id, actor.principal_id),
            ).fetchone()
            limits = {
                "baseline_id": baseline_id,
                "candidate_id": candidate_id,
                "sample_bps": sample_bps,
                "max_candidate_allocations": max_candidate_allocations,
                "valid_until": valid_until,
            }
            if row is not None:
                if any(row[key] != value for key, value in limits.items()):
                    raise StateMismatchError("A canary window cannot be rebound or extended")
                return dict(row)
            row = conn.execute(
                """INSERT INTO model_route_canaries
                (workspace_id,id,principal_id,baseline_id,candidate_id,baseline_epoch,candidate_epoch,
                 baseline_review_id,candidate_review_id,sample_bps,max_candidate_allocations,valid_until)
                SELECT b.workspace_id,%s,b.principal_id,b.id,c.id,b.epoch,c.epoch,
                  model_route_current(b.workspace_id,b.id),model_route_current(c.workspace_id,c.id),
                  %s,%s,%s
                FROM model_route_policies b JOIN model_route_policies c ON c.workspace_id=b.workspace_id
                WHERE b.workspace_id=%s AND b.principal_id=%s AND c.principal_id=b.principal_id
                  AND b.id=%s AND c.id=%s RETURNING *""",
                (
                    canary_id,
                    sample_bps,
                    max_candidate_allocations,
                    valid_until,
                    actor.workspace_id,
                    actor.principal_id,
                    baseline_id,
                    candidate_id,
                ),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Both canary routes must belong to the operator")
            return dict(row)

    def allocate(
        self,
        actor: WorkspaceContext,
        canary_id: UUID,
        allocation_key: str,
        *,
        baseline_request: object,
        candidate_request: object,
        facts: object,
    ) -> dict[str, Any]:
        if not isinstance(allocation_key, str) or not 1 <= len(allocation_key.encode()) <= 160:
            raise InvalidInputError("Canary requires a bounded allocation key")
        digest = content_hash([baseline_request, candidate_request, facts])
        key_digest = hashlib.sha256(allocation_key.encode()).hexdigest()
        decision_key = f"canary/{canary_id}/{key_digest}"
        with self.database.transaction(actor) as conn:
            canary = self._read(conn, actor, canary_id)
            conn.execute(
                "SELECT model_route_lock(%s,%s)", (actor.workspace_id, canary["candidate_id"])
            )
            prior = conn.execute(
                """SELECT a.input_sha256,d.* FROM model_route_canary_allocations a
                JOIN model_route_decisions d ON d.workspace_id=a.workspace_id
                  AND d.policy_id=a.policy_id AND d.allocation_key=a.decision_key
                WHERE a.workspace_id=%s AND a.canary_id=%s AND a.allocation_key=%s""",
                (actor.workspace_id, canary_id, allocation_key),
            ).fetchone()
            if prior is not None:
                if prior["input_sha256"] != digest:
                    raise StateMismatchError(
                        "Canary allocation inputs differ from the retained request"
                    )
                return self._view(
                    self.routing._decision(prior), prior["policy_id"] == canary["candidate_id"]
                )
            selected = conn.execute(
                """SELECT model_route_canary_choice(c,%s) AS policy_id FROM model_route_canaries c
                WHERE workspace_id=%s AND id=%s""",
                (allocation_key, actor.workspace_id, canary_id),
            ).fetchone()["policy_id"]
            recipes = {
                row["policy_id"]: row["recipe"]
                for row in conn.execute(
                    """SELECT policy_id,recipe FROM model_route_prompt_recipes
                    WHERE workspace_id=%s AND policy_id IN (%s,%s)""",
                    (actor.workspace_id, canary["baseline_id"], canary["candidate_id"]),
                ).fetchall()
            }
            exact, _ = compile_route_request(
                recipes[canary["baseline_id"]], facts, baseline_request, fill=True
            )
            candidate, _ = compile_route_request(
                recipes[canary["candidate_id"]], facts, candidate_request, fill=True
            )
            if parse_request(exact).episode_id != parse_request(candidate).episode_id:
                raise InvalidInputError("Both canary arms must bind the same task")
            conn.execute(
                """INSERT INTO model_route_canary_allocations
                (workspace_id,canary_id,principal_id,allocation_key,input_sha256,policy_id,decision_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    actor.workspace_id,
                    canary_id,
                    actor.principal_id,
                    allocation_key,
                    digest,
                    selected,
                    decision_key,
                ),
            )
            use_candidate = selected == canary["candidate_id"]
            decision = self.routing.allocate_in_transaction(
                conn,
                actor,
                selected,
                decision_key,
                candidate if use_candidate else exact,
                prompt_facts=facts,
            )
            return self._view(decision, use_candidate)

    @staticmethod
    def _view(decision: dict[str, Any], use_candidate: bool) -> dict[str, Any]:
        decision.pop("candidate_invoked")
        decision["selected"] = "candidate" if use_candidate else "baseline"
        return decision

    def rollback(self, actor: WorkspaceContext, canary_id: UUID) -> dict[str, Any]:
        """Fence pending candidate effects. Preserve decisions, jobs and usage."""
        with self.database.transaction(actor) as conn:
            canary = self._read(conn, actor, canary_id)
            conn.execute(
                "SELECT model_route_lock(%s,%s)", (actor.workspace_id, canary["candidate_id"])
            )
            conn.execute(
                """UPDATE model_route_canaries SET rolled_back_at=clock_timestamp()
                WHERE workspace_id=%s AND id=%s AND principal_id=%s AND rolled_back_at IS NULL""",
                (actor.workspace_id, canary_id, actor.principal_id),
            )
            return self._read(conn, actor, canary_id)
