"""R7.2-C: bounded, opt-in ticks for the existing checked atomic graph owner.

The pump is not a second execution system: only AtomicGraphService.launch_ready
can insert a job, and migration 49 rechecks dependencies, locks and interrupts
in the same database transaction as the existing operation-workbench launch.
The pump never retries an uncertain database outcome or an external effect.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from potbot.controller.atomic_graph_runtime import AtomicGraphService
from potbot.controller.jobs import _page_cursor, _parse_cursor
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)


def _page_limit(limit: int, *, maximum: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise InvalidInputError("Atomic graph page limit is outside the allowed range")
    return limit


class AtomicGraphPump:
    """Explicit-operator scheduling and monitor over migration-49 graph records.

    Calls are safe to run from several processes: advisory reads never grant an
    effect. The existing checked launch and PostgreSQL node trigger are the
    linearization points. An ambiguous commit raises rather than being retried.
    """

    def __init__(self, service: AtomicGraphService) -> None:
        if not isinstance(service, AtomicGraphService):
            raise InvalidInputError("Atomic graph pump requires the durable graph service")
        self.service = service
        self.database = service.database

    def list(
        self,
        context: WorkspaceContext,
        *,
        after: str | None = None,
        limit: int = 25,
        active_only: bool = False,
    ) -> dict[str, Any]:
        """Private bounded listing. A cursor cannot grant another actor's graph."""
        _page_limit(limit)
        if type(active_only) is not bool:
            raise InvalidInputError("Atomic graph state filter is invalid")
        cursor = _parse_cursor(after)
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT id,created_at,objective,state,max_parallel,manifest_hash "
                "FROM atomic_graph_runs WHERE workspace_id=%s AND principal_id=%s "
                "AND (%s=false OR state='active') "
                "AND (%s::timestamptz IS NULL OR (created_at,id)>(%s::timestamptz,%s::uuid)) "
                "ORDER BY created_at,id LIMIT %s",
                (
                    context.workspace_id,
                    context.principal_id,
                    active_only,
                    cursor[0] if cursor else None,
                    cursor[0] if cursor else None,
                    cursor[1] if cursor else None,
                    limit + 1,
                ),
            ).fetchall()
        page = rows[:limit]
        return {
            "schema": "potbot-atomic-graph-list/v1",
            "workspace_id": str(context.workspace_id),
            "principal_id": str(context.principal_id),
            "items": [
                {
                    "graph_id": str(row["id"]),
                    "objective": row["objective"],
                    "state": row["state"],
                    "max_parallel": row["max_parallel"],
                    "manifest_hash": row["manifest_hash"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in page
            ],
            "next_cursor": _page_cursor(dict(page[-1])) if page else after,
            "has_more": len(rows) > limit,
        }

    def tick_graph(
        self,
        context: WorkspaceContext,
        graph_id: UUID,
        *,
        setup_token: str,
        limit: int = 8,
    ) -> dict[str, Any]:
        """One bounded attempt; a race is reconciled only with a fresh read.

        The operator token never appears in output, events, exception strings,
        or stored graph state. A stale/expired check is NOT silently refreshed.
        """
        _page_limit(limit, maximum=64)
        before = self.service.inspect(context, graph_id)
        if before["state"] != "active" or before["outcome"] not in {"ready", "running", "waiting"}:
            return {
                "graph_id": str(graph_id),
                "decision": "no_dispatch",
                "launched": [],
                "status": before,
            }
        try:
            jobs = self.service.launch_ready(
                context, graph_id, setup_token=setup_token, limit=limit
            )
        except IndeterminateExecutionError:
            # Even checking a possibly committed result must not be mistaken for
            # proof that no effect was dispatched. Stop automatic progression.
            raise
        except (RevisionConflictError, StateMismatchError):
            # Another worker may have committed our exact idempotency key. Never
            # issue a second launch in the same pass; refresh public state.
            current = self.service.inspect(context, graph_id)
            return {
                "graph_id": str(graph_id),
                "decision": "reconciled_race",
                "launched": [],
                "status": current,
            }
        except ScopeDeniedError:
            # An expired check, changed installation or missing operator grant
            # is an admission blocker, not a reason to create replacement work.
            current = self.service.inspect(context, graph_id)
            return {
                "graph_id": str(graph_id),
                "decision": "admission_blocked",
                "launched": [],
                "status": current,
            }
        current = self.service.inspect(context, graph_id)
        return {
            "graph_id": str(graph_id),
            "decision": "launched" if jobs else "no_dispatch",
            "launched": [
                {"node_id": item["node_id"], "job_id": item["job"]["id"]} for item in jobs
            ],
            "status": current,
        }

    def pass_once(
        self,
        context: WorkspaceContext,
        *,
        setup_token: str,
        after: str | None = None,
        graphs: int = 20,
        leaves_per_graph: int = 8,
    ) -> dict[str, Any]:
        """Bounded fair scan; caller carries next_cursor between passes.

        When the final page is exhausted the next pass wraps to the first page.
        A worker never advances past an indeterminate commit by guessing.
        """
        _page_limit(graphs)
        _page_limit(leaves_per_graph, maximum=64)
        result = self.list(context, after=after, limit=graphs, active_only=True)
        if not result["items"] and after is not None:
            result = self.list(context, limit=graphs, active_only=True)
        decisions = []
        for graph in result["items"]:
            decisions.append(
                self.tick_graph(
                    context,
                    UUID(graph["graph_id"]),
                    setup_token=setup_token,
                    limit=leaves_per_graph,
                )
            )
        # Wrap immediately when the last visited page has no successor. All
        # graph identity/launch idempotency is still enforced by the DB owner.
        return {
            "schema": "potbot-atomic-graph-tick/v1",
            "workspace_id": str(context.workspace_id),
            "principal_id": str(context.principal_id),
            "decisions": decisions,
            "next_cursor": result["next_cursor"] if result["has_more"] else None,
        }
