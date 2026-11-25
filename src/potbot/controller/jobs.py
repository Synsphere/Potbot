"""Scoped durable API jobs, fenced workers and commit-ordered progress replay.

Paid/external work is never replayed merely because a worker lease expires. A
lost worker is reported as indeterminate and must be reconciled explicitly.
This queue is separate from the existing at-least-once projection outbox.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.tasks import json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    ErrorCode,
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.jobs import (
    ACTIVE_JOB_STATES,
    CHECK_JOB,
    COG_JOB,
    JOB_TYPES,
    MAX_JOB_OUTPUT_BYTES,
    MAX_OUTPUT_DELTA_BYTES,
    TERMINAL_JOB_STATES,
    CogJobInput,
    JobLease,
    JobSpec,
    JobState,
    identifier,
    lease_duration,
    progress_data,
    result_metadata,
    safe_integer,
)
from potbot.domain.serialization import canonical_json
from potbot.models.registry import DeploymentSpec
from potbot.persistence.database import Database, idempotent_command

_PUBLIC_FIELDS = (
    "id",
    "episode_id",
    "contract_revision_id",
    "job_type",
    "state",
    "update_token",
    "selected_models",
    "progress",
    "cancellation_requested",
    "result",
    "error_code",
    "output_bytes",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "dispatched_at",
    "heartbeat_at",
    "lease_until",
)
_ERROR_CODES = frozenset(code.value for code in ErrorCode) | {
    "lease_expired",
    "unsupported_job",
    "provider_refused",
    "budget_exhausted",
}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = json_record({key: row[key] for key in _PUBLIC_FIELDS})
    result.update(
        json_record(
            {
                "owner_principal_id": row.get("owner_principal_id"),
                "operation_check_id": row.get("operation_check_id"),
            }
        )
    )
    result.update(schema_version=1, job_id=result["id"], task_id=result["episode_id"])
    return result


def _read(
    conn: Any, context: WorkspaceContext, job_id: UUID, *, lock: bool = False
) -> dict[str, Any]:
    if not isinstance(job_id, UUID):
        raise InvalidInputError("Job identity must be a UUID.")
    row = conn.execute(
        "SELECT *,lease_until>clock_timestamp() AS valid_lease FROM jobs "
        "WHERE workspace_id=%s AND id=%s AND owner_principal_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (context.workspace_id, job_id, context.principal_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("The job is unavailable in this workspace.")
    return dict(row)


def _event(
    conn: Any, context: WorkspaceContext, job_id: UUID, event_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Allocate the cursor under a transaction-held lock, preserving commit order.

    SQL sequences do not provide this property: a later sequence could commit
    first and cause a resumed stream to skip the earlier transaction forever.
    All callers lock a job before this workspace cursor, never the reverse.
    """
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO job_event_cursors(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (context.workspace_id,),
    )
    counter = conn.execute(
        "UPDATE job_event_cursors SET last_sequence=last_sequence+1 "
        "WHERE workspace_id=%s RETURNING last_sequence",
        (context.workspace_id,),
    ).fetchone()
    row = conn.execute(
        "INSERT INTO job_events(workspace_id,sequence,id,job_id,event_type,data) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
        (context.workspace_id, counter["last_sequence"], uuid4(), job_id, event_type, Jsonb(data)),
    ).fetchone()
    return _event_public(dict(row))


def _event_public(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = json_record(
        {key: row[key] for key in ("sequence", "id", "job_id", "event_type", "data", "recorded_at")}
    )
    result.update(schema_version=1, event_id=result["id"])
    return result


def _snapshot_cursor(conn: Any, context: WorkspaceContext) -> int:
    # Share-lock the stream before reading a snapshot. Writers already modifying
    # a job remain uncommitted while waiting for this cursor; reads see its old
    # value. No job locks are taken here, preserving the writer lock order.
    row = conn.execute(
        "SELECT last_sequence FROM job_event_cursors WHERE workspace_id=%s FOR SHARE",
        (context.workspace_id,),
    ).fetchone()
    if row is None:
        return 0
    return int(
        conn.execute(
            "SELECT COALESCE(max(e.sequence),0) AS sequence FROM job_events e JOIN jobs j "
            "ON j.workspace_id=e.workspace_id AND j.id=e.job_id "
            "WHERE j.workspace_id=%s AND j.owner_principal_id=%s",
            (context.workspace_id, context.principal_id),
        ).fetchone()["sequence"]
    )


def _update_leased(
    conn: Any,
    context: WorkspaceContext,
    lease: JobLease,
    assignments: str,
    parameters: tuple[Any, ...] = (),
) -> dict[str, Any]:
    # The assignment text is an installation-owned constant from methods below.
    # Recheck the deadline in the write itself, including time spent waiting on
    # locks or validating metadata after the initial lease read.
    row = conn.execute(
        "UPDATE jobs SET " + assignments + " WHERE workspace_id=%s AND id=%s "
        "AND owner_principal_id=%s "
        "AND generation=%s AND lease_owner=%s AND state IN ('running','cancelling') "
        "AND lease_until>clock_timestamp() RETURNING *",
        (
            *parameters,
            context.workspace_id,
            lease.job_id,
            context.principal_id,
            lease.generation,
            lease.worker_id,
        ),
    ).fetchone()
    if row is None:
        raise RevisionConflictError("The worker lease is no longer current.")
    return dict(row)


def _page_cursor(row: dict[str, Any]) -> str:
    value = [row["created_at"].isoformat(), str(row["id"])]
    return base64.urlsafe_b64encode(canonical_json(value).encode()).decode().rstrip("=")


def _parse_cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 200:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        parts = json.loads(decoded)
        if not isinstance(parts, list) or len(parts) != 2:
            raise ValueError
        timestamp = datetime.fromisoformat(parts[0])
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, UUID(parts[1])
    except (ValueError, TypeError, AttributeError, UnicodeError, binascii.Error):
        raise InvalidInputError("The job page cursor is invalid.") from None


class JobService:
    def __init__(self, database: Database, *, task_execution_artifacts: Any = None) -> None:
        self.database = database
        self.task_execution_artifacts = task_execution_artifacts

    def submit(
        self,
        context: WorkspaceContext,
        payload: object,
        key: str,
        *,
        trusted_operation: bool = False,
        operation_check_id: UUID | None = None,
        operation_authorization: dict[str, Any] | None = None,
        create_guard: Callable[[Any, dict[str, Any]], None] | None = None,
        task_execution_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        spec = JobSpec.from_dict(payload)
        if (
            spec.job_type == CHECK_JOB
            or operation_check_id is not None
            or operation_authorization is not None
            or create_guard is not None
            or task_execution_binding is not None
        ) and not trusted_operation:
            raise ScopeDeniedError("Operation jobs require the operator workbench")
        if (operation_check_id is None) != (operation_authorization is None):
            raise InvalidInputError("Operation launches require their check and authorization")
        if spec.job_type == CHECK_JOB and operation_check_id is not None:
            raise InvalidInputError("A check cannot itself be a checked execution")
        selection_id = None
        if task_execution_binding is not None:
            from potbot.domain.task_execution import parse_binding

            task_execution_binding = parse_binding(task_execution_binding)
            selection_id = UUID(task_execution_binding["selectionId"])
            if (
                task_execution_binding["workspaceId"] != str(context.workspace_id)
                or task_execution_binding["principalId"] != str(context.principal_id)
                or task_execution_binding["taskBinding"]["episodeId"] != str(spec.episode_id)
                or (spec.job_type != CHECK_JOB and operation_check_id is None)
                or (
                    spec.job_type == CHECK_JOB
                    and spec.input.get("task_execution_sha256") != task_execution_binding["sha256"]
                )
            ):
                raise ScopeDeniedError("Selected task work must retain its exact owned binding")
        elif spec.job_type == CHECK_JOB and "task_execution_sha256" in spec.input:
            raise ScopeDeniedError("Selected task checks require their immutable binding")
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                episode = conn.execute(
                    "SELECT status,active_contract_id FROM episodes "
                    "WHERE workspace_id=%s AND id=%s FOR SHARE",
                    (context.workspace_id, spec.episode_id),
                ).fetchone()
                if episode is None:
                    raise ScopeDeniedError("The task is unavailable in this workspace.")
                if episode["active_contract_id"] is None:
                    raise StateMismatchError("Jobs require an existing task contract.")
                if episode["status"] in {
                    "verified",
                    "partial",
                    "budget_exhausted",
                    "cancelled",
                    "failed",
                }:
                    raise StateMismatchError("Terminal tasks cannot accept new jobs.")
                if create_guard is not None:
                    create_guard(conn, dict(episode))
                job_id = uuid4()
                row = conn.execute(
                    "INSERT INTO jobs(workspace_id,id,episode_id,contract_revision_id,"
                    "job_type,input,selected_models,owner_principal_id,operation_check_id,"
                    "operation_authorization,task_selection_id,task_execution_binding) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        context.workspace_id,
                        job_id,
                        spec.episode_id,
                        episode["active_contract_id"],
                        spec.job_type,
                        Jsonb(spec.input),
                        Jsonb(spec.selected_models),
                        context.principal_id,
                        operation_check_id,
                        Jsonb(operation_authorization) if operation_authorization else None,
                        selection_id,
                        Jsonb(task_execution_binding) if task_execution_binding else None,
                    ),
                ).fetchone()
                public = _public(dict(row))
                _event(conn, context, job_id, "job.queued", {"job": public})
                return public

            # Authorization timestamps/MACs are private capabilities, not retry
            # identity. A committed same-key submission remains replayable after
            # expiry, while changing the checked request or check conflicts.
            request = spec.to_dict()
            if spec.job_type == CHECK_JOB:
                request = {
                    **request,
                    "input": {
                        key: value for key, value in spec.input.items() if key != "authorization"
                    },
                }
            if operation_check_id is not None:
                request = {"request": request, "operation_check_id": str(operation_check_id)}
            if task_execution_binding is not None:
                request = {
                    "request": request,
                    "task_execution_sha256": task_execution_binding["sha256"],
                }
            result = idempotent_command(conn, context, "submit_api_job", key, request, create)
            # Existing command keys are workspace-scoped. Never let a matching
            # key return another principal's committed private result.
            _read(conn, context, UUID(result["id"]))
            return result

    def inspect(self, context: WorkspaceContext, job_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            cursor = _snapshot_cursor(conn, context)
            result = _public(_read(conn, context, job_id))
            result["event_cursor"] = cursor
            return result

    def list(
        self,
        context: WorkspaceContext,
        *,
        state: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInputError("Page size must be between one and 200.")
        if state is not None and (
            not isinstance(state, str) or state not in {s.value for s in JobState}
        ):
            raise InvalidInputError("The job state filter is invalid.")
        cursor = _parse_cursor(after)
        predicates = ["workspace_id=%s", "owner_principal_id=%s"]
        parameters: list[Any] = [context.workspace_id, context.principal_id]
        if state is not None:
            predicates.append("state=%s")
            parameters.append(state)
        if cursor is not None:
            predicates.append("(created_at,id)<(%s,%s)")
            parameters.extend(cursor)
        parameters.append(limit + 1)
        with self.database.transaction(context) as conn:
            event_cursor = _snapshot_cursor(conn, context)
            rows = conn.execute(
                "SELECT * FROM jobs WHERE "
                + " AND ".join(predicates)
                + " ORDER BY created_at DESC,id DESC LIMIT %s",
                parameters,
            ).fetchall()
            more = len(rows) > limit
            page = rows[:limit]
            return {
                "items": [_public(dict(row)) for row in page],
                "next_cursor": _page_cursor(dict(page[-1])) if more else None,
                "has_more": more,
                "event_cursor": event_cursor,
            }

    def queue(self, context: WorkspaceContext) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            event_cursor = _snapshot_cursor(conn, context)
            rows = conn.execute(
                "SELECT state,count(*) AS count,min(created_at) AS oldest_created_at "
                "FROM jobs WHERE workspace_id=%s AND owner_principal_id=%s GROUP BY state",
                (context.workspace_id, context.principal_id),
            ).fetchall()
            counts = {state.value: 0 for state in JobState}
            oldest = None
            for row in rows:
                counts[row["state"]] = row["count"]
                if row["state"] == "queued":
                    oldest = json_record(row["oldest_created_at"])
            stale = conn.execute(
                "SELECT count(*) AS count FROM jobs WHERE workspace_id=%s AND owner_principal_id=%s "
                "AND state IN ('running','cancelling') AND lease_until<=clock_timestamp()",
                (context.workspace_id, context.principal_id),
            ).fetchone()["count"]
            return {
                "schema_version": 1,
                "counts": counts,
                "total": sum(counts.values()),
                "queued": counts["queued"],
                "running": counts["running"],
                "cancelling": counts["cancelling"],
                "expired_leases": stale,
                "oldest_queued_at": oldest,
                "event_cursor": event_cursor,
                "automatic_retry": False,
            }

    def events(
        self,
        context: WorkspaceContext,
        job_id: UUID | None = None,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        safe_integer(after, "Event cursor")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InvalidInputError("Event page size must be between one and 500.")
        with self.database.transaction(context) as conn:
            if job_id is not None:
                _read(conn, context, job_id)
            where = "workspace_id=%s AND sequence>%s AND EXISTS (SELECT 1 FROM jobs j "
            where += "WHERE j.workspace_id=job_events.workspace_id AND j.id=job_events.job_id "
            where += "AND j.owner_principal_id=%s)"
            parameters: list[Any] = [context.workspace_id, after, context.principal_id]
            if job_id is not None:
                where += " AND job_id=%s"
                parameters.append(job_id)
            parameters.append(limit + 1)
            rows = conn.execute(
                "SELECT * FROM job_events WHERE " + where + " ORDER BY sequence LIMIT %s",
                parameters,
            ).fetchall()
            page = rows[:limit]
            return {
                "items": [_event_public(dict(row)) for row in page],
                "next_cursor": int(page[-1]["sequence"]) if page else after,
                "has_more": len(rows) > limit,
            }

    def cancel(self, context: WorkspaceContext, job_id: UUID, key: str) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            _read(conn, context, job_id)

            def change() -> dict[str, Any]:
                old = _read(conn, context, job_id, lock=True)
                if old["state"] in TERMINAL_JOB_STATES or old["cancellation_requested"]:
                    return _public(old)
                queued = old["state"] == JobState.QUEUED
                state = JobState.CANCELLED if queued else JobState.CANCELLING
                row = conn.execute(
                    "UPDATE jobs SET state=%s,cancellation_requested=true,"
                    "update_token=update_token+1,updated_at=clock_timestamp(),"
                    "finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,"
                    "progress=jsonb_build_object('stage',%s::text) "
                    "WHERE workspace_id=%s AND id=%s RETURNING *",
                    (state.value, queued, state.value, context.workspace_id, job_id),
                ).fetchone()
                public = _public(dict(row))
                _event(conn, context, job_id, "job." + state.value, {"job": public})
                return public

            result = idempotent_command(
                conn, context, "cancel_api_job", key, {"job_id": str(job_id)}, change
            )
            if result.get("id") != str(job_id):
                raise ScopeDeniedError("The cancellation result belongs to another job")
            _read(conn, context, job_id)
            return result

    @staticmethod
    def _lease_row(conn: Any, context: WorkspaceContext, lease: JobLease) -> dict[str, Any]:
        if not isinstance(lease, JobLease) or lease.workspace_id != context.workspace_id:
            raise ScopeDeniedError("The worker lease is unavailable in this workspace.")
        row = _read(conn, context, lease.job_id, lock=True)
        if (
            row["state"] not in ACTIVE_JOB_STATES
            or row["generation"] != lease.generation
            or row["lease_owner"] != lease.worker_id
            or not row["valid_lease"]
            or row["operation_check_id"] != lease.operation_check_id
            or row["operation_authorization"] != lease.operation_authorization
            or row.get("task_selection_id") != lease.task_selection_id
            or row.get("task_execution_binding") != lease.task_execution_binding
            or (
                lease.owner_principal_id is not None
                and lease.owner_principal_id != context.principal_id
            )
        ):
            raise RevisionConflictError("The worker lease is no longer current.")
        return row

    def operation_check(self, context: WorkspaceContext, lease: JobLease) -> dict[str, Any] | None:
        """Return the owned private check only to a current linked worker lease.

        This internal record includes the signed input and must never be returned
        by an HTTP/CLI adapter. Public views use :meth:`inspect` instead.
        """
        if lease.operation_check_id is None:
            return None
        with self.database.transaction(context) as conn:
            self._lease_row(conn, context, lease)
            check = _read(conn, context, lease.operation_check_id)
            if (
                check["job_type"] != CHECK_JOB
                or check["state"] != JobState.SUCCEEDED
                or check["episode_id"] != lease.episode_id
                or check["contract_revision_id"] != lease.contract_revision_id
            ):
                raise ScopeDeniedError("The operation check is no longer applicable")
            return check

    def expire(self, context: WorkspaceContext, *, limit: int = 200) -> int:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidInputError("Expiry batch size must be between one and 1000.")
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT id,job_type,episode_id,dispatched_at FROM jobs WHERE workspace_id=%s "
                "AND owner_principal_id=%s "
                "AND state IN ('running','cancelling') AND lease_until<=clock_timestamp() "
                "ORDER BY lease_until,id FOR UPDATE SKIP LOCKED LIMIT %s",
                (context.workspace_id, context.principal_id, limit),
            ).fetchall()
            for old in rows:
                self._settle_cog(conn, context, old, JobState.INDETERMINATE)
                row = conn.execute(
                    "UPDATE jobs SET state='indeterminate',error_code='lease_expired',"
                    "lease_until=NULL,lease_owner=NULL,finished_at=clock_timestamp(),"
                    "updated_at=clock_timestamp(),update_token=update_token+1,"
                    "progress=jsonb_build_object('stage','indeterminate') "
                    "WHERE workspace_id=%s AND id=%s RETURNING *",
                    (context.workspace_id, old["id"]),
                ).fetchone()
                _event(conn, context, old["id"], "job.indeterminate", {"job": _public(dict(row))})
            return len(rows)

    def claim(
        self,
        context: WorkspaceContext,
        worker_id: str,
        *,
        kinds: tuple[str, ...] = ("model.generate",),
        lease_seconds: float = 30,
    ) -> JobLease | None:
        seconds = lease_duration(lease_seconds)
        identifier(worker_id, "Worker identity")
        if (
            not isinstance(kinds, tuple)
            or not kinds
            or any(not isinstance(kind, str) or kind not in JOB_TYPES for kind in kinds)
        ):
            raise InvalidInputError("Worker kinds must identify installed job handlers.")
        self.expire(context)
        with self.database.transaction(context) as conn:
            from potbot.controller.node_controls import admission_allowed
            from potbot.domain.live_controls import WORKER_PREFIX

            if worker_id.startswith(WORKER_PREFIX) and not admission_allowed(
                conn, context, worker_id
            ):
                return None
            old = conn.execute(
                "SELECT id FROM jobs WHERE workspace_id=%s AND owner_principal_id=%s AND state='queued' "
                "AND job_type=ANY(%s) ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1",
                (context.workspace_id, context.principal_id, list(kinds)),
            ).fetchone()
            if old is None:
                return None
            row = conn.execute(
                "UPDATE jobs SET state='running',generation=generation+1,lease_owner=%s,"
                "lease_until=clock_timestamp()+(%s * interval '1 second'),"
                "heartbeat_at=clock_timestamp(),started_at=clock_timestamp(),"
                "updated_at=clock_timestamp(),update_token=update_token+1,"
                "progress=jsonb_build_object('stage','claimed') "
                "WHERE workspace_id=%s AND id=%s RETURNING *",
                (worker_id, seconds, context.workspace_id, old["id"]),
            ).fetchone()
            _event(conn, context, row["id"], "job.running", {"job": _public(dict(row))})
            return JobLease(
                context.workspace_id,
                row["id"],
                row["generation"],
                worker_id,
                row["lease_until"],
                row["job_type"],
                row["episode_id"],
                dict(row["input"]),
                list(row["selected_models"]),
                row["contract_revision_id"],
                row["owner_principal_id"],
                row["operation_check_id"],
                dict(row["operation_authorization"]) if row["operation_authorization"] else None,
                row["task_selection_id"],
                dict(row["task_execution_binding"]) if row["task_execution_binding"] else None,
            )

    def renew(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        *,
        lease_seconds: float = 30,
    ) -> datetime:
        seconds = lease_duration(lease_seconds)
        with self.database.transaction(context) as conn:
            self._lease_row(conn, context, lease)
            row = _update_leased(
                conn,
                context,
                lease,
                "lease_until=clock_timestamp()+(%s * interval '1 second'),"
                "heartbeat_at=clock_timestamp(),updated_at=clock_timestamp(),"
                "update_token=update_token+1",
                (seconds,),
            )
            _event(conn, context, lease.job_id, "job.heartbeat", {"job": _public(dict(row))})
            deadline: datetime = row["lease_until"]
            return deadline

    def mark_dispatched(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        *,
        cog_max_wall_ms: int | None = None,
        cog_model_deployment: DeploymentSpec | None = None,
        check_cog_grant: Callable[[Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from psycopg.errors import ObjectNotInPrerequisiteState

        with self.database.transaction(context) as conn:
            old = self._lease_row(conn, context, lease)
            if old["cancellation_requested"]:
                raise OperationCancelledError("Job cancellation preceded external dispatch.")
            if old["job_type"] == CHECK_JOB:
                raise ScopeDeniedError("Operation checks cannot dispatch effects")
            if old.get("task_selection_id") is not None:
                from potbot.controller.task_execution import validate_dispatch_binding

                validate_dispatch_binding(
                    self.database, context, conn, old, artifacts=self.task_execution_artifacts
                )
            if old["dispatched_at"] is not None:
                if old["job_type"] == COG_JOB:
                    raise StateMismatchError("A native Cog dispatch cannot be repeated.")
                return _public(old)
            if cog_model_deployment is not None and old["job_type"] != COG_JOB:
                raise ScopeDeniedError("Only a native Cog can dispatch an installed model.")
            decision = None
            if old["job_type"] == COG_JOB:
                from potbot.controller.budgets import BudgetLedger

                if type(cog_max_wall_ms) is not int or not 1 <= cog_max_wall_ms <= 300_000:
                    raise ScopeDeniedError(
                        "Native Cog dispatch requires an installed wall-time bound."
                    )
                spec = CogJobInput.parse(old["input"])
                amounts = {"concurrency": 1, "calls": 1, "tool_ms": cog_max_wall_ms}
                if cog_model_deployment is not None:
                    from potbot.models.native_results import REQUEST, validate_native_request

                    if (
                        not isinstance(cog_model_deployment, DeploymentSpec)
                        or spec.type_ref != REQUEST
                    ):
                        raise ScopeDeniedError("Native inference needs an exact installed model.")
                    validate_native_request(spec.value, cog_model_deployment)
                    amounts["tokens"] = cog_model_deployment.usable_context_tokens
                episode = lock_episode(conn, context, old["episode_id"])
                contract = conn.execute(
                    "SELECT content FROM contract_revisions "
                    "WHERE workspace_id=%s AND episode_id=%s AND id=%s",
                    (context.workspace_id, lease.episode_id, lease.contract_revision_id),
                ).fetchone()
                repository = conn.execute(
                    "SELECT 1 FROM repository_states WHERE workspace_id=%s AND id=%s "
                    "AND repository_id=%s",
                    (context.workspace_id, spec.repository_state_id, episode["repository_id"]),
                ).fetchone()
                if (
                    episode["status"] != "running"
                    or episode["active_contract_id"] != lease.contract_revision_id
                    or contract is None
                    or "cog.execute:" + spec.operation_ref
                    not in contract["content"]["capabilities"]
                    or repository is None
                ):
                    raise ScopeDeniedError("The active task does not authorize this native Cog.")
                if check_cog_grant is not None:
                    decision = check_cog_grant(conn)
                BudgetLedger(self.database).reserve_in_transaction(
                    conn,
                    context,
                    lease.episode_id,
                    amounts,
                    f"native-cog-{lease.job_id}",
                )
            try:
                row = _update_leased(
                    conn,
                    context,
                    lease,
                    "dispatched_at=clock_timestamp(),updated_at=clock_timestamp(),"
                    "update_token=update_token+1,progress=jsonb_build_object('stage','dispatching')",
                )
            except ObjectNotInPrerequisiteState:
                if cog_model_deployment is None or lease.operation_check_id is None:
                    raise
                # A database dispatch fence can veto a still-owned lease.
                # Failing the unsent job also rolls back its reservation above.
                raise StateMismatchError(
                    "The checked native model no longer has dispatch authority."
                ) from None
            public = _public(dict(row))
            evidence = {"job": public}
            if decision is not None:
                evidence["capability_policy"] = decision
            _event(conn, context, lease.job_id, "job.dispatched", evidence)
            return public

    def _settle_cog(
        self,
        conn: Any,
        context: WorkspaceContext,
        job: dict[str, Any],
        state: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        if job["job_type"] != COG_JOB or job["dispatched_at"] is None:
            return
        from potbot.controller.budgets import BudgetLedger

        row = conn.execute(
            "SELECT id,amounts FROM budget_reservations "
            "WHERE workspace_id=%s AND episode_id=%s AND reservation_key=%s",
            (context.workspace_id, job["episode_id"], f"native-cog-{job['id']}"),
        ).fetchone()
        if row is None:
            raise StateMismatchError("Native Cog dispatch lost its task reservation.")
        ledger = BudgetLedger(self.database)
        if state == JobState.SUCCEEDED:
            episode = lock_episode(conn, context, job["episode_id"])
            if (
                episode["status"] != "running"
                or episode["active_contract_id"] != job["contract_revision_id"]
                or job["cancellation_requested"]
            ):
                raise StateMismatchError("The native Cog task changed before completion.")
            actual = {
                name: amount for name, amount in row["amounts"].items() if name != "concurrency"
            }
            if "tokens" in actual:
                if (
                    result is None
                    or result.get("operation_id") != str(job["id"])
                    or "artifact_id" not in result
                    or "usage" not in result
                ):
                    raise StateMismatchError("Native model completion requires its metered result.")
                usage = result["usage"]
                request = CogJobInput.parse(job["input"]).value
                if (
                    set(usage) != {"input_tokens", "output_tokens", "tokens"}
                    or usage["input_tokens"] < 1
                    or not 1 <= usage["output_tokens"] <= request["max_output_tokens"]
                    or usage["tokens"] != usage["input_tokens"] + usage["output_tokens"]
                    or usage["tokens"] > actual["tokens"]
                ):
                    raise StateMismatchError(
                        "Native model usage exceeds or differs from admission."
                    )
                actual["tokens"] = usage["tokens"]
            ledger.settle(context, row["id"], actual, "conservative_estimate", connection=conn)
        else:
            ledger.mark_indeterminate(
                context, row["id"], "native_cog_outcome_unconfirmed", connection=conn
            )

    def progress(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        stage: str,
        completed: int | None = None,
        total: int | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        progress = progress_data(stage, completed, total)
        with self.database.transaction(context) as conn:
            old = self._lease_row(conn, context, lease)
            if old["progress"] == progress:
                return _public(old)
            row = _update_leased(
                conn,
                context,
                lease,
                "progress=%s,updated_at=clock_timestamp(),update_token=update_token+1",
                (Jsonb(progress),),
            )
            public = _public(dict(row))
            _event(conn, context, lease.job_id, "job.progress", {"job": public})
            return public

    def output(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        text: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """Persist authorized output after the calling worker has redacted secrets."""
        try:
            if not isinstance(text, str) or not 1 <= len(text.encode()) <= MAX_OUTPUT_DELTA_BYTES:
                raise InvalidInputError("Output delta must be nonempty and at most 64 KiB.")
        except UnicodeError:
            raise InvalidInputError("Output requires valid Unicode.") from None
        if (provider is None) != (model is None):
            raise InvalidInputError("Output tagging requires both provider and model.")
        if role is not None and (
            provider is None or not isinstance(role, str) or role not in {"reasoning", "coding"}
        ):
            raise InvalidInputError("An output role requires a selected provider and model.")
        with self.database.transaction(context) as conn:
            row = self._lease_row(conn, context, lease)
            if row["dispatched_at"] is None:
                raise StateMismatchError("Output requires a recorded dispatch.")
            data: dict[str, Any] = {"text": text}
            if provider is not None:
                if not any(
                    m["provider"] == provider
                    and m["model"] == model
                    and (role is None or m["role"] == role)
                    for m in row["selected_models"]
                ):
                    raise InvalidInputError("Output source is not selected for this job.")
                data.update(provider=provider, model=model)
                if role is not None:
                    data["role"] = role
            count = len(text.encode())
            if row["output_bytes"] + count > MAX_JOB_OUTPUT_BYTES:
                raise InvalidInputError("The job exceeded its retained output allowance.")
            _update_leased(
                conn,
                context,
                lease,
                "output_bytes=output_bytes+%s,updated_at=clock_timestamp(),"
                "update_token=update_token+1",
                (count,),
            )
            return _event(conn, context, lease.job_id, "job.output", data)

    def cancelled(self, context: WorkspaceContext, lease: JobLease) -> bool:
        """Tell a worker to stop after lease, job, or task authority changes."""
        if lease.workspace_id != context.workspace_id:
            raise ScopeDeniedError("The worker lease is unavailable in this workspace.")
        with self.database.transaction(context) as conn:
            row = _read(conn, context, lease.job_id)
            if (
                row["cancellation_requested"]
                or row["state"] != JobState.RUNNING
                or row["generation"] != lease.generation
                or row["lease_owner"] != lease.worker_id
                or not row["valid_lease"]
            ):
                return True
            task = conn.execute(
                "SELECT status,active_contract_id FROM episodes WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, row["episode_id"]),
            ).fetchone()
            return bool(
                task is None
                or task["status"] != "running"
                or task["active_contract_id"] != lease.contract_revision_id
                or row["contract_revision_id"] != lease.contract_revision_id
                or row["episode_id"] != lease.episode_id
            )

    def finish(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        state: str,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Publish a worker-observed terminal outcome; uncertain work uses indeterminate.

        Cancellation means the worker confirmed stopping. A cancellation request
        on its own can never call this method or manufacture that confirmation.
        """
        from psycopg.types.json import Jsonb

        if not isinstance(state, str) or state not in TERMINAL_JOB_STATES:
            raise InvalidInputError("Worker completion requires a terminal state.")
        if error_code is not None and (
            not isinstance(error_code, str) or error_code not in _ERROR_CODES
        ):
            raise InvalidInputError("Job failure requires a recognized error code.")
        if state == JobState.SUCCEEDED and error_code is not None:
            raise InvalidInputError("A successful job cannot report an error.")
        metadata = None if result is None else result_metadata(result)
        with self.database.transaction(context) as conn:
            old = self._lease_row(conn, context, lease)
            if (
                state == JobState.SUCCEEDED
                and old["dispatched_at"] is None
                and old["job_type"] != CHECK_JOB
            ):
                raise StateMismatchError("Successful work requires a recorded dispatch.")
            if old["job_type"] == CHECK_JOB and state == JobState.SUCCEEDED:
                if metadata is None or set(metadata) != {"operation_check"}:
                    raise StateMismatchError("Successful checks require their bounded report")
                from potbot.domain.operation_workbench import parse_check_input, request_digest

                identity, checked = parse_check_input(old["input"])
                report = metadata["operation_check"]
                if (
                    report["operationId"] != identity
                    or report["bindings"]["requestSha256"] != request_digest(checked.to_dict())
                    or report["bindings"]["contractRevisionId"] != str(old["contract_revision_id"])
                ):
                    raise StateMismatchError("Operation check report does not match its request")
            elif metadata is not None and "operation_check" in metadata:
                raise StateMismatchError("Only operation checks may publish a check report")
            if state == JobState.CANCELLED and not old["cancellation_requested"]:
                raise StateMismatchError("Cancellation completion needs a cancellation request.")
            self._settle_cog(conn, context, old, state, metadata)
            row = _update_leased(
                conn,
                context,
                lease,
                "state=%s,result=%s,error_code=%s,lease_owner=NULL,lease_until=NULL,"
                "finished_at=clock_timestamp(),updated_at=clock_timestamp(),"
                "update_token=update_token+1,progress=jsonb_build_object('stage',%s::text)",
                (
                    state,
                    Jsonb(metadata) if metadata is not None else None,
                    error_code,
                    state,
                ),
            )
            public = _public(dict(row))
            _event(conn, context, lease.job_id, "job." + state, {"job": public})
            return public
