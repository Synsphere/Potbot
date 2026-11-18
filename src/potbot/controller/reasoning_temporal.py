"""R7.1 decisions made by the existing single reasoning owner.

PostgreSQL remains authoritative for intake, source identity and dispatch
fencing. The tree checkpoint retains the context actually used by prompts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from potbot.controller.reasoning_runtime import (
    PostgresReasoningStore,
    PostgresSteeringInbox,
    ReasoningDispatchInterrupted,
)
from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.auth import WorkspaceContext


class TemporalSteeringController:
    def __init__(
        self,
        store: PostgresReasoningStore,
        inbox: PostgresSteeringInbox,
        context: WorkspaceContext,
        run_id: str,
        token: UUID,
        tree: ReasoningTree,
        emit: Callable[..., None],
    ) -> None:
        self.store, self.inbox, self.context = store, inbox, context
        self.run_id, self.token, self.tree, self.emit = run_id, token, tree, emit

    def interrupted(self) -> bool:
        return bool(self.store.control_state(self.context, self.run_id)["interrupt_requested"])

    def _resolve(
        self, request: dict[str, Any], component: dict[str, Any], outcome: dict[str, Any]
    ) -> None:
        self.inbox.resolve_component(
            self.context, self.run_id, self.token, request, component["id"], outcome
        )
        self.emit(
            "reasoning.steering.component",
            run_id=self.run_id,
            prompt_id=request["id"],
            component_id=component["id"],
            timing=component["classification"]["timing"],
            status=outcome["status"],
            task_revision=self.tree.revision,
        )

    def background(self) -> bool:
        """Apply literal context before compiling the next affected prompt."""
        changed = False
        # Intake capacity is 128 plus one emergency stop. Fetching the complete
        # bounded set preserves waiting next-goal rows and avoids
        # an old waiting item starving a newer urgent/background component.
        for request in self.inbox.pending(self.context, self.run_id, limit=256):
            for component in request.get("components", ()):
                if component["id"] in request["component_results"]:
                    continue
                timing = component["classification"]["timing"]
                if timing == "background":
                    changed = self.tree.record_context(component) or changed
                    retained = next(
                        c
                        for c in self.tree.context_revisions
                        if c["component_id"] == component["id"]
                    )
                    self._resolve(
                        request,
                        component,
                        {
                            "status": "unchanged" if retained["duplicate"] else "recorded",
                            "context_epoch": retained["context_epoch"],
                            "task_revision": retained["revision"],
                            "requires_reassessment": retained["material"],
                            "source_sha256": retained["text_sha256"],
                        },
                    )
                elif timing == "clarify":
                    self._resolve(
                        request,
                        component,
                        {
                            "status": "needs_clarification",
                            "explanation": "The message is retained; clarify the requested change before execution.",
                        },
                    )
        return changed

    def check_interrupt(self) -> None:
        if not self.interrupted():
            return
        # Retain mixed-message background even when no more inference is
        # permitted. Deferred components stay pending on their predecessors.
        self.background()
        for request in self.inbox.pending(self.context, self.run_id, limit=256):
            for component in request.get("components", ()):
                if (
                    component["id"] not in request["component_results"]
                    and component["classification"]["timing"] == "interrupt"
                ):
                    self._resolve(
                        request,
                        component,
                        {
                            "status": "dispatch_fenced",
                            "explanation": "Further work in this run is fenced. Existing dispatch receipts retain any uncertain outcome.",
                            "in_flight_stop_confirmed": False,
                        },
                    )
        self.tree.interrupt()
        raise ReasoningDispatchInterrupted("user interrupt stopped further reasoning dispatch")

    def eligible(self) -> tuple[dict[str, Any], ...]:
        """Expose only dependency-ready same-goal requests to graph admission."""
        self.check_interrupt()
        self.background()
        result: list[dict[str, Any]] = []
        for request in self.inbox.pending(self.context, self.run_id, limit=256):
            if not request.get("components"):
                result.append(request)
                continue
            for component in request["components"]:
                if component["id"] in request["component_results"]:
                    continue
                if component["classification"]["timing"] != "deferred":
                    continue
                readiness = self.inbox.component_readiness(self.context, self.run_id, component)
                if readiness == "blocked":
                    self._resolve(
                        request,
                        component,
                        {
                            "status": "blocked",
                            "explanation": "The predecessor did not complete successfully; no automatic replay is permitted.",
                        },
                    )
                elif readiness == "ready" and component["scope"] == "current_goal":
                    result.append(
                        {
                            "id": component["id"],
                            "request_id": request["request_id"],
                            "prompt": component["text"],
                            "temporal_request": request,
                            "temporal_component": component,
                        }
                    )
        return tuple(result)

    def resolve(self, request: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        component = request["temporal_component"]
        self._resolve(
            request["temporal_request"],
            component,
            {
                "status": "needs_clarification"
                if decision["decision"] == "needs_clarification"
                else "applied",
                **decision,
                "predecessor": component["predecessor"],
            },
        )
        return {"id": component["id"], **decision}

    def prompt_context(self) -> dict[str, Any]:
        return {
            "context_epoch": self.tree.context_epoch,
            "user_context_revisions": self.tree.context_revisions,
            "user_context_rule": "User facts revise assumptions but are not independently verified source evidence; conflicting facts must remain explicit.",
        }
