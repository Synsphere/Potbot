"""PostgreSQL-fenced reasoning snapshots and workspace-scoped steering.

This boundary serializes *one writer per reasoning run*, even across API
processes. Expired runs are marked indeterminate; no uncertain model/tool call
is automatically repeated. Stored snapshots are retained as audit evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.steering_triage import split_steering
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.persistence.database import Database

_RUN = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class ReasoningContextChanged(StateMismatchError):
    """A new context revision arrived before the possible-dispatch boundary."""


class ReasoningDispatchInterrupted(StateMismatchError):
    """An accepted user interrupt fenced a subsequent dispatch."""


def _run_id(value: str) -> UUID:
    if not isinstance(value, str) or not _RUN.fullmatch(value):
        raise InvalidInputError("reasoning run ID must be a 32-character hex identifier")
    return UUID(hex=value)


def _record(row: Any) -> dict[str, Any]:
    if row is None:
        raise ScopeDeniedError("reasoning run is unavailable")
    return dict(row)


def _locked_run(conn: Any, context: WorkspaceContext, run_id: str, token: UUID) -> dict[str, Any]:
    row = _record(
        conn.execute(
            "SELECT *,lease_until>clock_timestamp() AS lease_valid FROM reasoning_runtime_runs "
            "WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (context.workspace_id, _run_id(run_id)),
        ).fetchone()
    )
    if row["state"] != "running" or row["lease_token"] != token or not row["lease_valid"]:
        raise StateMismatchError("reasoning lease expired or was superseded")
    return row


class PostgresReasoningStore:
    """Run-scoped lease with transactional snapshots and committed event replay."""

    def __init__(self, database: Database, *, lease_seconds: int = 600) -> None:
        if type(lease_seconds) is not int or not 60 <= lease_seconds <= 3600:
            raise InvalidInputError("reasoning lease must be 60-3600 seconds")
        self.database = database
        self.lease_seconds = lease_seconds

    def start(self, context: WorkspaceContext, run_id: str) -> UUID:
        token = uuid4()
        with self.database.transaction(context) as conn:
            conn.execute(
                "INSERT INTO reasoning_runtime_runs "
                "(workspace_id,id,principal_id,state,lease_token,lease_until) "
                "VALUES (%s,%s,%s,'running',%s,clock_timestamp()+(%s * interval '1 second'))",
                (
                    context.workspace_id,
                    _run_id(run_id),
                    context.principal_id,
                    token,
                    self.lease_seconds,
                ),
            )
        return token

    def checkpoint(
        self, context: WorkspaceContext, run_id: str, token: UUID, snapshot: dict[str, Any]
    ) -> None:
        """Fenced checkpoint and event append commit atomically.

        An expired writer cannot publish a late model result. A later operator
        must reconcile the uncertain invocation rather than blindly replay it.
        """
        if not isinstance(snapshot, dict) or len(json.dumps(snapshot).encode()) > 4_194_304:
            raise InvalidInputError("reasoning snapshot exceeds the allowed bound")
        events = snapshot.get("events")
        if not isinstance(events, list) or len(events) > 10000:
            raise InvalidInputError("reasoning event history is invalid")
        status = snapshot.get("run_status", "running")
        if status not in {"running", "completed", "failed", "interrupted"}:
            raise InvalidInputError("reasoning run status is invalid")
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            row = _record(
                conn.execute(
                    "SELECT state,lease_token,event_cursor,interrupt_requested FROM reasoning_runtime_runs "
                    "WHERE workspace_id=%s AND id=%s FOR UPDATE",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )
            valid = conn.execute(
                "SELECT 1 FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s "
                "AND state='running' AND lease_token=%s AND lease_until > clock_timestamp()",
                (context.workspace_id, _run_id(run_id), token),
            ).fetchone()
            if valid is None:
                raise StateMismatchError("reasoning lease expired or was superseded")
            if status == "completed" and row["interrupt_requested"]:
                raise ReasoningDispatchInterrupted("interrupted reasoning cannot publish success")
            cursor = row["event_cursor"]
            if cursor > len(events):
                raise StateMismatchError("reasoning events cannot be truncated")
            for index, event in enumerate(events[cursor:], start=cursor + 1):
                if not isinstance(event, dict) or event.get("sequence") != index:
                    raise InvalidInputError("reasoning events must be append-only and ordered")
                conn.execute(
                    "INSERT INTO reasoning_runtime_events "
                    "(workspace_id,run_id,sequence,type,data) VALUES (%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        _run_id(run_id),
                        index,
                        event.get("type", ""),
                        Jsonb(event),
                    ),
                )
            closed = status != "running"
            conn.execute(
                "UPDATE reasoning_runtime_runs SET snapshot=%s,checkpoint=checkpoint+1, "
                "event_cursor=%s,state=%s,lease_token=%s,lease_until="
                "CASE WHEN %s THEN NULL ELSE clock_timestamp()+(%s * interval '1 second') END, "
                "accepting_steering=CASE WHEN %s THEN false ELSE accepting_steering END, "
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (
                    Jsonb(snapshot),
                    len(events),
                    status,
                    None if closed else token,
                    closed,
                    self.lease_seconds,
                    closed,
                    context.workspace_id,
                    _run_id(run_id),
                ),
            )

    def inspect(self, context: WorkspaceContext, run_id: str) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = _record(
                conn.execute(
                    "SELECT state,generation,checkpoint,accepting_steering,snapshot,"
                    "interrupt_requested,context_epoch,active_dispatch_id "
                    "FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )
        if row["active_dispatch_id"] is not None:
            row["active_dispatch_id"] = str(row["active_dispatch_id"])
        return {"run_id": run_id, **row}

    def control_state(self, context: WorkspaceContext, run_id: str) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return _record(
                conn.execute(
                    "SELECT state,interrupt_requested,context_epoch,checkpoint "
                    "FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )

    def begin_dispatch(
        self,
        context: WorkspaceContext,
        run_id: str,
        token: UUID,
        *,
        node_id: str,
        kind: str,
        context_epoch: int,
    ) -> UUID:
        """Linearize one possible dispatch against concurrent steering intake.

        Once committed, this invocation may have started. Cancellation cannot
        claim otherwise without a separately observed not-started outcome.
        """
        if kind not in {"model", "evidence"} or type(context_epoch) is not int or context_epoch < 0:
            raise InvalidInputError("invalid reasoning dispatch contract")
        try:
            node = UUID(node_id)
        except (TypeError, ValueError):
            raise InvalidInputError("invalid reasoning dispatch node") from None
        identity = uuid4()
        with self.database.transaction(context) as conn:
            run = _locked_run(conn, context, run_id, token)
            if run["interrupt_requested"]:
                raise ReasoningDispatchInterrupted("user interrupt fenced reasoning dispatch")
            if run["context_epoch"] != context_epoch:
                raise ReasoningContextChanged(
                    "new background context requires prompt recompilation"
                )
            if run["active_dispatch_id"] is not None:
                raise StateMismatchError("previous reasoning invocation requires reconciliation")
            nodes = run["snapshot"].get("nodes", ())
            if not any(n.get("id") == str(node) and n.get("state") == "running" for n in nodes):
                raise StateMismatchError("reasoning dispatch requires an exact checkpointed claim")
            conn.execute(
                "INSERT INTO reasoning_runtime_dispatches "
                "(workspace_id,run_id,id,node_id,lease_token,kind,context_epoch,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'dispatched')",
                (context.workspace_id, _run_id(run_id), identity, node, token, kind, context_epoch),
            )
            conn.execute(
                "UPDATE reasoning_runtime_runs SET active_dispatch_id=%s WHERE workspace_id=%s AND id=%s",
                (identity, context.workspace_id, _run_id(run_id)),
            )
        return identity

    def finish_dispatch(
        self,
        context: WorkspaceContext,
        run_id: str,
        token: UUID,
        identity: UUID,
        *,
        outcome: str,
    ) -> None:
        """Retain an observation even if the writer lease expired during I/O.

        An observation cannot authorize another call or publish a late result.
        Repeating the same observation is safe; contradicting it is rejected.
        """
        if outcome not in {"completed", "indeterminate", "not_started"}:
            raise InvalidInputError("invalid reasoning dispatch outcome")
        with self.database.transaction(context) as conn:
            _record(
                conn.execute(
                    "SELECT id FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s FOR UPDATE",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )
            row = _record(
                conn.execute(
                    "SELECT status,lease_token FROM reasoning_runtime_dispatches "
                    "WHERE workspace_id=%s AND run_id=%s AND id=%s FOR UPDATE",
                    (context.workspace_id, _run_id(run_id), identity),
                ).fetchone()
            )
            if row["lease_token"] != token:
                raise StateMismatchError("reasoning dispatch belongs to another writer")
            if row["status"] != "dispatched":
                if row["status"] != outcome:
                    raise StateMismatchError("reasoning dispatch outcome is immutable")
                return
            conn.execute(
                "UPDATE reasoning_runtime_dispatches SET status=%s,finished_at=clock_timestamp() "
                "WHERE workspace_id=%s AND run_id=%s AND id=%s",
                (outcome, context.workspace_id, _run_id(run_id), identity),
            )
            conn.execute(
                "UPDATE reasoning_runtime_runs SET active_dispatch_id=NULL "
                "WHERE workspace_id=%s AND id=%s AND active_dispatch_id=%s",
                (context.workspace_id, _run_id(run_id), identity),
            )

    def events(
        self, context: WorkspaceContext, run_id: str, *, after: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInputError("invalid reasoning event cursor")
        with self.database.transaction(context) as conn:
            _record(
                conn.execute(
                    "SELECT id FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )
            rows = conn.execute(
                "SELECT sequence,type,data FROM reasoning_runtime_events "
                "WHERE workspace_id=%s AND run_id=%s AND sequence>%s "
                "ORDER BY sequence LIMIT %s",
                (context.workspace_id, _run_id(run_id), after, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def expire(self, context: WorkspaceContext, *, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInputError("invalid recovery limit")
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT id FROM reasoning_runtime_runs WHERE workspace_id=%s "
                "AND state='running' AND lease_until <= clock_timestamp() "
                "ORDER BY lease_until,id FOR UPDATE SKIP LOCKED LIMIT %s",
                (context.workspace_id, limit),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE reasoning_runtime_runs SET state='indeterminate',lease_token=NULL, "
                    "lease_until=NULL,accepting_steering=false,updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, row["id"]),
                )
        return len(rows)


class PostgresSteeringInbox:
    """Durable concurrent intake; only the run owner publishes steering decisions."""

    uses_context = True
    supports_temporal = True

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _key(request_id: str) -> str:
        if not isinstance(request_id, str) or not _KEY.fullmatch(request_id):
            raise InvalidInputError("steering request ID is invalid")
        return hashlib.sha256(request_id.encode()).hexdigest()

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "request_id": row["request_id"],
            "prompt": row["prompt"],
            "status": row["status"],
            "created_ns": int(row["created_at"].timestamp() * 1_000_000_000),
        }
        if row.get("components"):
            result["components"] = row["components"]
            result["component_results"] = row["component_results"]
            result["principal_id"] = str(row["principal_id"])
            result["acknowledgement"] = {
                "dispatch_fenced": any(
                    c["classification"]["timing"] == "interrupt" for c in row["components"]
                ),
                "in_flight_stop_confirmed": False,
                "deferred_components": sum(
                    c["classification"]["timing"] == "deferred" for c in row["components"]
                ),
                "context_components": sum(
                    c["classification"]["timing"] == "background" for c in row["components"]
                ),
                "blocked_components": sum(
                    outcome.get("status") == "blocked"
                    for outcome in row["component_results"].values()
                ),
            }
            if len(row["components"]) == 1 and row["component_results"]:
                outcome = next(iter(row["component_results"].values()))
                result.update({key: value for key, value in outcome.items() if key != "status"})
        if row["decision"] is not None:
            result.update(row["decision"])
        return result

    def submit(
        self, context: WorkspaceContext, run_id: str, *, request_id: str, prompt: str
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 8192:
            raise InvalidInputError("steering prompt must be 1-8192 UTF-8 bytes")
        key = self._key(request_id)
        parsed = split_steering(prompt)
        urgent = any(c.classification.timing == "interrupt" for c in parsed)
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            run = _record(
                conn.execute(
                    "SELECT state,accepting_steering,checkpoint,active_dispatch_id,context_epoch,interrupt_requested "
                    "FROM reasoning_runtime_runs "
                    "WHERE workspace_id=%s AND id=%s FOR UPDATE",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )
            existing = conn.execute(
                "SELECT * FROM reasoning_runtime_steering "
                "WHERE workspace_id=%s AND run_id=%s AND id=%s",
                (context.workspace_id, _run_id(run_id), key),
            ).fetchone()
            if existing:
                if existing["prompt"] != prompt or existing["request_id"] != request_id:
                    raise InvalidInputError("steering idempotency conflict")
                return self._view(existing)
            if run["state"] != "running" or not run["accepting_steering"]:
                raise InvalidInputError("reasoning run is no longer accepting steering")
            count = conn.execute(
                "SELECT count(*) AS count FROM reasoning_runtime_steering WHERE workspace_id=%s AND run_id=%s",
                (context.workspace_id, _run_id(run_id)),
            ).fetchone()
            if count["count"] >= 128 and (not urgent or run["interrupt_requested"]):
                raise InvalidInputError("reasoning steering capacity exhausted")
            retained_context = conn.execute(
                "SELECT count(*) AS count,COALESCE(sum(octet_length(c->>'text')),0) AS bytes "
                "FROM reasoning_runtime_steering s, jsonb_array_elements(s.components) c "
                "WHERE s.workspace_id=%s AND s.run_id=%s "
                "AND c->'classification'->>'timing'='background' "
                "AND COALESCE(s.component_results->(c->>'id')->>'status','')<>'blocked'",
                (context.workspace_id, _run_id(run_id)),
            ).fetchone()
            context_full = (
                retained_context["count"]
                + sum(c.classification.timing == "background" for c in parsed)
                > 128
                or retained_context["bytes"]
                + sum(
                    len(c.text.encode()) for c in parsed if c.classification.timing == "background"
                )
                > 32768
            )
            if context_full and not urgent:
                raise InvalidInputError("reasoning background context capacity exhausted")
            epoch = run["context_epoch"] + int(
                not context_full and any(c.classification.timing == "background" for c in parsed)
            )
            components: list[dict[str, Any]] = []
            initial_results: dict[str, Any] = {}
            for part in parsed:
                component = part.to_dict()
                component["id"] = hashlib.sha256(f"{key}:{part.ordinal}".encode()).hexdigest()
                component["context_epoch"] = epoch
                component["source_request_id"] = key
                component["source_principal_id"] = str(context.principal_id)
                component["predecessor"] = (
                    {"kind": "run", "id": run_id}
                    if part.scope == "next_goal"
                    else {"kind": "dispatch", "id": str(run["active_dispatch_id"])}
                    if run["active_dispatch_id"] is not None
                    else {"kind": "checkpoint", "number": run["checkpoint"]}
                )
                components.append(component)
                if context_full and part.classification.timing == "background":
                    initial_results[component["id"]] = {
                        "status": "blocked",
                        "explanation": "Background context capacity is exhausted; the urgent stop was still accepted.",
                    }
            conn.execute(
                "UPDATE reasoning_runtime_runs SET interrupt_requested=interrupt_requested OR %s,"
                "context_epoch=%s,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (
                    urgent,
                    epoch,
                    context.workspace_id,
                    _run_id(run_id),
                ),
            )
            row = conn.execute(
                "INSERT INTO reasoning_runtime_steering "
                "(workspace_id,run_id,id,request_id,prompt,status,principal_id,components,component_results) "
                "VALUES (%s,%s,%s,%s,%s,'queued',%s,%s,%s) RETURNING *",
                (
                    context.workspace_id,
                    _run_id(run_id),
                    key,
                    request_id,
                    prompt,
                    context.principal_id,
                    Jsonb(components),
                    Jsonb(initial_results),
                ),
            ).fetchone()
            return self._view(_record(row))

    def pending(
        self, context: WorkspaceContext, run_id: str, *, limit: int = 16
    ) -> tuple[dict[str, Any], ...]:
        if type(limit) is not int or not 1 <= limit <= 256:
            raise InvalidInputError("invalid steering page size")
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                "AND run_id=%s AND status='queued' ORDER BY created_at,id LIMIT %s",
                (context.workspace_id, _run_id(run_id), limit),
            ).fetchall()
        return tuple(self._view(dict(row)) for row in rows)

    def component_readiness(
        self, context: WorkspaceContext, run_id: str, component: dict[str, Any]
    ) -> str:
        """A predecessor must have a confirmed successful terminal outcome."""
        predecessor = component["predecessor"]
        with self.database.transaction(context) as conn:
            run = _record(
                conn.execute(
                    "SELECT state,checkpoint FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )
            if predecessor["kind"] == "run":
                return (
                    "ready_for_new_goal"
                    if run["state"] == "completed"
                    else ("waiting" if run["state"] == "running" else "blocked")
                )
            if predecessor["kind"] == "checkpoint":
                if run["state"] != "running":
                    return "blocked"
                return "ready" if run["checkpoint"] >= predecessor["number"] else "waiting"
            row = _record(
                conn.execute(
                    "SELECT status FROM reasoning_runtime_dispatches "
                    "WHERE workspace_id=%s AND run_id=%s AND id=%s",
                    (context.workspace_id, _run_id(run_id), UUID(predecessor["id"])),
                ).fetchone()
            )
        if run["state"] != "running":
            return "blocked"
        return {"completed": "ready", "dispatched": "waiting"}.get(row["status"], "blocked")

    def resolve_component(
        self,
        context: WorkspaceContext,
        run_id: str,
        token: UUID,
        request: dict[str, Any],
        component_id: str,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one controller decision under the existing live writer lease."""
        if (
            not isinstance(outcome, dict)
            or outcome.get("status")
            not in {
                "applied",
                "recorded",
                "unchanged",
                "needs_clarification",
                "dispatch_fenced",
                "blocked",
            }
            or len(json.dumps(outcome).encode()) > 4096
        ):
            raise InvalidInputError("invalid temporal steering outcome")
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            run = _locked_run(conn, context, run_id, token)
            row = _record(
                conn.execute(
                    "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s AND run_id=%s AND id=%s FOR UPDATE",
                    (context.workspace_id, _run_id(run_id), request["id"]),
                ).fetchone()
            )
            component = next((c for c in row["components"] if c["id"] == component_id), None)
            if component is None:
                raise ScopeDeniedError("steering component is unavailable")
            results = dict(row["component_results"])
            if component_id in results:
                if results[component_id] != outcome:
                    raise StateMismatchError("temporal steering outcome is immutable")
                return self._view(row)
            if outcome["status"] == "applied":
                predecessor = component["predecessor"]
                if (
                    component["classification"]["timing"] != "deferred"
                    or component["scope"] != "current_goal"
                    or run["interrupt_requested"]
                ):
                    raise StateMismatchError(
                        "steering component is not eligible for current-goal execution"
                    )
                if predecessor["kind"] == "dispatch":
                    previous = conn.execute(
                        "SELECT status FROM reasoning_runtime_dispatches WHERE workspace_id=%s AND run_id=%s AND id=%s",
                        (context.workspace_id, _run_id(run_id), UUID(predecessor["id"])),
                    ).fetchone()
                    if previous is None or previous["status"] != "completed":
                        raise StateMismatchError(
                            "deferred predecessor has not completed successfully"
                        )
                elif (
                    predecessor["kind"] != "checkpoint" or run["checkpoint"] < predecessor["number"]
                ):
                    raise StateMismatchError("deferred checkpoint has not completed")
            results[component_id] = outcome
            status = "decided" if len(results) == len(row["components"]) else "queued"
            updated = conn.execute(
                "UPDATE reasoning_runtime_steering SET component_results=%s,status=%s "
                "WHERE workspace_id=%s AND run_id=%s AND id=%s RETURNING *",
                (Jsonb(results), status, context.workspace_id, _run_id(run_id), request["id"]),
            ).fetchone()
            return self._view(_record(updated))

    def resolve(
        self,
        context: WorkspaceContext,
        run_id: str,
        request: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(decision, dict) or len(json.dumps(decision)) > 8192:
            raise InvalidInputError("steering decision must be bounded")
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            row = _record(
                conn.execute(
                    "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                    "AND run_id=%s AND id=%s FOR UPDATE",
                    (context.workspace_id, _run_id(run_id), request["id"]),
                ).fetchone()
            )
            if row["components"]:
                raise InvalidInputError("temporal steering requires fenced component resolution")
            if row["status"] == "decided":
                return self._view(row)
            result = conn.execute(
                "UPDATE reasoning_runtime_steering SET status='decided',decision=%s "
                "WHERE workspace_id=%s AND run_id=%s AND id=%s RETURNING *",
                (Jsonb(decision), context.workspace_id, _run_id(run_id), request["id"]),
            ).fetchone()
            return self._view(_record(result))

    def seal(self, context: WorkspaceContext, run_id: str) -> None:
        with self.database.transaction(context) as conn:
            conn.execute(
                "UPDATE reasoning_runtime_runs SET accepting_steering=false "
                "WHERE workspace_id=%s AND id=%s AND state='running'",
                (context.workspace_id, _run_id(run_id)),
            )

    def get(self, context: WorkspaceContext, run_id: str, request_id: str) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                "AND run_id=%s AND id=%s",
                (context.workspace_id, _run_id(run_id), self._key(request_id)),
            ).fetchone()
            return self._view(_record(row))

    def history(self, context: WorkspaceContext, run_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(context) as conn:
            _record(
                conn.execute(
                    "SELECT id FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, _run_id(run_id)),
                ).fetchone()
            )
            rows = conn.execute(
                "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                "AND run_id=%s ORDER BY created_at,id LIMIT 257",
                (context.workspace_id, _run_id(run_id)),
            ).fetchall()
        if len(rows) > 256:
            raise InvalidInputError("steering history exceeds page limit")
        return [self._view(dict(row)) for row in rows]
