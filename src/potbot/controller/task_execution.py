"""Selected-task provenance and admission on the existing operation lifecycle.

There is no separate execution state machine here. Existing jobs own checks,
dispatch, accounting, cancellation, worker fencing and uncertain outcomes.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from potbot.context.service import native_skill_setup_token
from potbot.controller.jobs import _page_cursor, _parse_cursor, _public, _read
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.controller.task_discovery import TaskDiscoveryService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.jobs import CHECK_JOB, COG_JOB, CogJobInput, JobLease
from potbot.domain.operation_workbench import (
    operation_id,
    parse_request,
    verify_check_authorization,
    verify_launch_authorization,
)
from potbot.domain.result_assertions import validate_result_oracle
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.task_execution import (
    BINDING_SCHEMA,
    ORACLE_SCHEMA,
    SCHEMA,
    execution_consumer_sha256,
    parse_binding,
)
from potbot.persistence.database import Database
from potbot.semantics.catalog import canonical_type_ref


def _uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("Task execution identity must be a UUID")
    return value


def _scope(context: WorkspaceContext, binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "workspaceId": str(context.workspace_id),
        "principalId": str(context.principal_id),
        "selectionId": binding["selectionId"],
        "selectionSha256": binding["selectionSha256"],
        "operationId": binding["operationId"],
        "taskBinding": binding["taskBinding"],
    }


def _row_binding(context: WorkspaceContext, row: dict[str, Any]) -> dict[str, Any]:
    binding = parse_binding(row.get("task_execution_binding"))
    if (
        row.get("task_selection_id") != UUID(binding["selectionId"])
        or row.get("owner_principal_id") != context.principal_id
        or binding["workspaceId"] != str(context.workspace_id)
        or binding["principalId"] != str(context.principal_id)
        or binding["taskBinding"]["episodeId"] != str(row["episode_id"])
        or binding["taskBinding"]["contractRevisionId"] != str(row["contract_revision_id"])
    ):
        raise ScopeDeniedError("The job has no matching owned selected-task binding")
    return binding


def _canonical_request(binding: dict[str, Any]) -> None:
    spec = parse_request(binding["request"])
    if spec.job_type == COG_JOB:
        request = CogJobInput.parse(spec.input)
        ports = binding["capabilities"]
        if (
            str(request.repository_state_id) != binding["taskBinding"]["repositoryStateId"]
            or ports["inputTypes"] != [str(canonical_type_ref(request.type_ref))]
            or len(ports["outputTypes"]) != 1
        ):
            raise ScopeDeniedError(
                "Selected native execution requires exact canonical input and output ports"
            )


def validate_current_binding(
    database: Database,
    context: WorkspaceContext,
    connection: Any,
    value: object,
    *,
    setup_token: str | None,
    artifacts: Any = None,
) -> dict[str, Any]:
    """Revalidate selected authority inside an existing admission transaction."""
    binding = parse_binding(value)
    if binding["consumerSha256"] != execution_consumer_sha256():
        raise RevisionConflictError("The selected execution consumer changed")
    if binding["workspaceId"] != str(context.workspace_id) or binding["principalId"] != str(
        context.principal_id
    ):
        raise ScopeDeniedError("Selected execution belongs to another owner")
    # Keep task contract and the selected discovery head stable until the queue
    # owner commits its admission. The collector reads the existing source
    # authorities; it never installs or probes a runtime.
    task = connection.execute(
        "SELECT status,active_contract_id FROM episodes WHERE workspace_id=%s AND id=%s FOR SHARE",
        (context.workspace_id, UUID(binding["taskBinding"]["episodeId"])),
    ).fetchone()
    if (
        task is None
        or task["status"] != "running"
        or str(task["active_contract_id"]) != binding["taskBinding"]["contractRevisionId"]
    ):
        raise RevisionConflictError("The selected task contract is no longer current")
    selected = connection.execute(
        "SELECT * FROM task_discovery_selections WHERE workspace_id=%s AND principal_id=%s AND id=%s",
        (context.workspace_id, context.principal_id, UUID(binding["selectionId"])),
    ).fetchone()
    if selected is None:
        raise ScopeDeniedError("The selected capability is unavailable")
    connection.execute(
        "SELECT id FROM task_discovery_searches WHERE workspace_id=%s AND principal_id=%s AND id=%s FOR SHARE",
        (context.workspace_id, context.principal_id, selected["search_id"]),
    ).fetchone()
    discovery = TaskDiscoveryService(database, setup_token=lambda: setup_token, artifacts=artifacts)
    selection = discovery._selection_view(connection, context, dict(selected), setup_token)
    if (
        not selection["approvalValid"]
        or not selection["authorityCurrent"]
        or selection["sha256"] != binding["selectionSha256"]
        or selection["taskBinding"] != binding["taskBinding"]
        or selection["candidateId"] != binding["candidateId"]
        or selection["searchId"] != binding["searchId"]
        or selection["resultId"] != binding["resultId"]
        or selection["owner"]
        != {
            "kind": "operation",
            "ownerId": binding["operationId"],
            "bindingId": None,
            "ownerSha256": binding["ownerSha256"],
        }
    ):
        raise RevisionConflictError("The exact selected capability is no longer current")
    result = discovery._row(connection, context, "result", selected["result_id"])
    candidate = next(
        (item for item in result["payload"]["items"] if item["id"] == binding["candidateId"]), None
    )
    if (
        candidate is None
        or candidate["capabilities"] != binding["capabilities"]
        or not candidate["authorityCurrent"]
    ):
        raise RevisionConflictError("The selected canonical operation contract changed")
    _canonical_request(binding)
    return binding


def validate_dispatch_binding(
    database: Database,
    context: WorkspaceContext,
    connection: Any,
    row: dict[str, Any],
    *,
    artifacts: Any = None,
) -> None:
    """Final selected-task fence at the existing durable dispatch marker."""
    service = TaskExecutionService(database, artifacts=artifacts)
    binding = service._verified_binding(connection, context, row, require_current=True)
    try:
        validate_current_binding(
            database,
            context,
            connection,
            binding,
            setup_token=service._token(),
            artifacts=artifacts,
        )
    except RevisionConflictError:
        # Queue workers reserve RevisionConflictError for a lost lease. A
        # current lease whose selected authority changed must finish denied.
        raise StateMismatchError("Selected authority changed before dispatch") from None


class TaskExecutionService:
    def __init__(
        self, database: Database, *, setup_token: Any = None, artifacts: Any = None
    ) -> None:
        self.database = database
        self.setup_token = setup_token if setup_token is not None else native_skill_setup_token
        self.artifacts = artifacts
        self.workbench = OperationWorkbench(database)
        self.discovery = TaskDiscoveryService(
            database, setup_token=self._token, artifacts=artifacts
        )

    def _token(self) -> str | None:
        try:
            value = self.setup_token()
            return value if isinstance(value, str) and 32 <= len(value) <= 4096 else None
        except Exception:
            return None

    def _authorized(self, presented: str) -> str:
        token = self._token()
        if (
            token is None
            or not isinstance(presented, str)
            or not hmac.compare_digest(token, presented)
        ):
            raise ScopeDeniedError("Selected execution requires current operator setup authority")
        return token

    def _selection(
        self, connection: Any, context: WorkspaceContext, identity: UUID
    ) -> dict[str, Any]:
        row = self.discovery._row(connection, context, "selection", _uuid(identity))
        if content_hash(row["payload"]) != row["sha256"] or set(row["payload"]) != {
            "taskBinding",
            "owner",
            "handoff",
        }:
            raise StateMismatchError("Task selection integrity failed")
        return row

    @staticmethod
    def _selection_scope(context: WorkspaceContext, row: dict[str, Any]) -> dict[str, Any]:
        return _scope(
            context,
            {
                "selectionId": str(row["id"]),
                "selectionSha256": row["sha256"],
                "operationId": row["payload"]["owner"]["ownerId"]
                if row["payload"]["owner"]["kind"] == "operation"
                else None,
                "taskBinding": row["payload"]["taskBinding"],
            },
        )

    def context(self, context: WorkspaceContext, selection_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            row = self._selection(connection, context, selection_id)
            selection = self.discovery._selection_view(connection, context, row, self._token())
        identity = self._selection_scope(context, row)["operationId"]
        operation = None
        reasons = list(selection["reasonCodes"])
        if identity is not None:
            try:
                operation = self.workbench.describe(context, identity)
            except (ScopeDeniedError, StateMismatchError):
                reasons.append("operation_unavailable")
        else:
            reasons.append("selection_requires_its_destination_owner")
        supported = operation is not None and operation["supported"]
        if operation is not None and not supported:
            reasons.extend(operation["reasonCodes"])
        return {
            **self._selection_scope(context, row),
            "selection": selection,
            "operation": operation,
            "supported": supported,
            "current": bool(selection["approvalValid"] and selection["authorityCurrent"]),
            "reasonCodes": sorted(set(reasons)),
        }

    def _make_binding(
        self,
        context: WorkspaceContext,
        selection_id: UUID,
        selection_sha256: str,
        request: object,
        acceptance: object,
    ) -> dict[str, Any]:
        spec = parse_request(request)
        oracle = validate_result_oracle(acceptance, schema=ORACLE_SCHEMA)
        operation_id(selection_sha256)
        with self.database.transaction(context) as connection:
            selected = self._selection(connection, context, selection_id)
            owner = selected["payload"]["owner"]
            if selected["sha256"] != selection_sha256:
                raise RevisionConflictError("The reviewed selection digest differs")
            if owner["kind"] != "operation" or owner["bindingId"] is not None:
                raise ScopeDeniedError("This selection must use its existing destination owner")
            result = self.discovery._row(connection, context, "result", selected["result_id"])
            candidate = next(
                (
                    item
                    for item in result["payload"]["items"]
                    if item["id"] == selected["candidate_id"]
                ),
                None,
            )
            if candidate is None or candidate["ownerSha256"] != owner["ownerSha256"]:
                raise StateMismatchError("The selected operation lost its exact candidate")
        binding = {
            "schema": BINDING_SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "selectionId": str(selection_id),
            "selectionSha256": selection_sha256,
            "searchId": str(selected["search_id"]),
            "resultId": str(selected["result_id"]),
            "candidateId": selected["candidate_id"],
            "operationId": owner["ownerId"],
            "ownerSha256": owner["ownerSha256"],
            "consumerSha256": execution_consumer_sha256(),
            "taskBinding": selected["payload"]["taskBinding"],
            "capabilities": candidate["capabilities"],
            "request": spec.to_dict(),
            "requestSha256": content_hash(spec.to_dict()),
            "acceptance": oracle,
            "acceptanceSha256": content_hash(oracle),
        }
        binding["sha256"] = content_hash(binding)
        return parse_binding(binding)

    def check(
        self,
        context: WorkspaceContext,
        selection_id: UUID,
        selection_sha256: str,
        request: object,
        acceptance: object,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        spec = parse_request(request).to_dict()
        oracle = validate_result_oracle(acceptance, schema=ORACLE_SCHEMA)
        binding = None
        with self.database.transaction(context) as connection:
            prior = connection.execute(
                "SELECT result FROM command_results WHERE workspace_id=%s AND operation='submit_api_job' AND idempotency_key=%s",
                (context.workspace_id, key),
            ).fetchone()
            if prior is not None:
                row = _read(connection, context, UUID(prior["result"]["id"]))
                retained = _row_binding(context, row)
                if (
                    row["job_type"] != CHECK_JOB
                    or retained["selectionId"] != str(_uuid(selection_id))
                    or retained["selectionSha256"] != selection_sha256
                    or retained["request"] != spec
                    or retained["acceptance"] != oracle
                ):
                    raise RevisionConflictError(
                        "This key already identifies a different selected check"
                    )
                binding = retained
        if binding is None:
            binding = self._make_binding(context, selection_id, selection_sha256, spec, oracle)
        queued = self.workbench.check(
            context,
            binding["operationId"],
            binding["request"],
            key,
            setup_token=token,
            task_execution_binding=binding,
            task_execution_artifacts=self.artifacts,
        )
        return {**_scope(context, binding), "binding": binding, "job": queued["job"]}

    def _linked(
        self, connection: Any, context: WorkspaceContext, selection_id: UUID, job_id: UUID
    ) -> dict[str, Any]:
        row = _read(connection, context, _uuid(job_id))
        if row.get("task_selection_id") != _uuid(selection_id):
            raise ScopeDeniedError("The job belongs to another task selection")
        return row

    def _verified_binding(
        self,
        connection: Any,
        context: WorkspaceContext,
        row: dict[str, Any],
        *,
        require_current: bool,
    ) -> dict[str, Any]:
        binding = _row_binding(context, row)
        if binding["consumerSha256"] != execution_consumer_sha256():
            raise StateMismatchError(
                "The retained execution consumer differs from this installation"
            )
        check = (
            row
            if row["job_type"] == CHECK_JOB
            else _read(connection, context, row["operation_check_id"])
        )
        if (
            _row_binding(context, check) != binding
            or check["job_type"] != CHECK_JOB
            or check["input"].get("task_execution_sha256") != binding["sha256"]
        ):
            raise StateMismatchError("The selected execution differs from its original check")
        identity, request = verify_check_authorization(
            context, check["input"], self._token(), require_current=require_current
        )
        if identity != binding["operationId"] or request.to_dict() != binding["request"]:
            raise StateMismatchError("The selected execution differs from its signed request")
        if (
            row["job_type"] != CHECK_JOB
            and {
                "job_type": row["job_type"],
                "episode_id": str(row["episode_id"]),
                "input": row["input"],
                "selected_models": row["selected_models"],
            }
            != binding["request"]
        ):
            raise StateMismatchError("The selected run differs from its bound request")
        if row["job_type"] != CHECK_JOB:
            if check["state"] != "succeeded":
                raise StateMismatchError("The selected run has no completed check")
            lease = JobLease(
                context.workspace_id,
                row["id"],
                row["generation"],
                row["lease_owner"] or "retained-binding",
                row["lease_until"] or datetime.now(UTC),
                row["job_type"],
                row["episode_id"],
                row["input"],
                row["selected_models"],
                row["contract_revision_id"],
                row["owner_principal_id"],
                row["operation_check_id"],
                row["operation_authorization"],
                row["task_selection_id"],
                row["task_execution_binding"],
            )
            verify_launch_authorization(
                context, lease, check["input"], self._token(), require_current=require_current
            )
        return binding

    def execution_binding(self, context: WorkspaceContext, job_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            return self._verified_binding(
                connection,
                context,
                _read(connection, context, _uuid(job_id)),
                require_current=False,
            )

    def launch(
        self,
        context: WorkspaceContext,
        selection_id: UUID,
        check_job_id: UUID,
        binding_sha256: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        operation_id(binding_sha256)
        with self.database.transaction(context) as connection:
            check = self._linked(connection, context, selection_id, check_job_id)
            binding = _row_binding(context, check)
            if check["job_type"] != CHECK_JOB or binding["sha256"] != binding_sha256:
                raise RevisionConflictError("Launch requires the exact reviewed check binding")
        queued = self.workbench.launch(
            context,
            binding["operationId"],
            check_job_id,
            binding["request"],
            key,
            setup_token=token,
            task_execution_binding=binding,
            task_execution_artifacts=self.artifacts,
        )
        return {**_scope(context, binding), "binding": binding, "job": queued["job"]}

    def inspect(
        self, context: WorkspaceContext, selection_id: UUID, job_id: UUID
    ) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            binding = _row_binding(context, self._linked(connection, context, selection_id, job_id))
        return {
            **_scope(context, binding),
            "binding": binding,
            "job": self.workbench.jobs.inspect(context, job_id),
        }

    def jobs(
        self,
        context: WorkspaceContext,
        selection_id: UUID,
        *,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 25
            or (after is not None and (not isinstance(after, str) or len(after) > 512))
        ):
            raise InvalidInputError("Selected jobs require a bounded page")
        cursor = _parse_cursor(after)
        with self.database.transaction(context) as connection:
            selected = self._selection(connection, context, selection_id)
            params: list[Any] = [context.workspace_id, context.principal_id, selection_id]
            predicate = ""
            if cursor is not None:
                found = self._linked(connection, context, selection_id, cursor[1])
                if found["created_at"] != cursor[0]:
                    raise InvalidInputError("The selected jobs cursor is inconsistent")
                predicate = " AND (created_at,id)<(%s,%s)"
                params.extend(cursor)
            params.append(limit + 1)
            rows = connection.execute(
                "SELECT * FROM jobs WHERE workspace_id=%s AND owner_principal_id=%s AND task_selection_id=%s"
                + predicate
                + " ORDER BY created_at DESC,id DESC LIMIT %s",
                params,
            ).fetchall()
            page, size = [], 0
            for row in rows[:limit]:
                item = {"binding": _row_binding(context, dict(row)), "job": _public(dict(row))}
                amount = len(canonical_json(item).encode("utf-8"))
                if size + amount > 1_000_000:
                    break
                page.append((row, item))
                size += amount
            more = len(rows) > len(page)
            return {
                **self._selection_scope(context, selected),
                "items": [item for _, item in page],
                "nextCursor": _page_cursor(dict(page[-1][0])) if more and page else None,
                "hasMore": more,
            }

    def events(
        self,
        context: WorkspaceContext,
        selection_id: UUID,
        job_id: UUID,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        view = self.inspect(context, selection_id, job_id)
        page = self.workbench.events(context, view["operationId"], job_id, after=after, limit=limit)
        return {**view, **{key: page[key] for key in ("items", "next_cursor", "has_more")}}

    def cancel(
        self, context: WorkspaceContext, selection_id: UUID, job_id: UUID, key: str
    ) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            row = self._linked(connection, context, selection_id, job_id)
            try:
                binding = _row_binding(context, row)
                scope = _scope(context, binding)
            except (InvalidInputError, StateMismatchError, ScopeDeniedError):
                binding = None
                selected = self._selection(connection, context, selection_id)
                scope = self._selection_scope(context, selected)
        # Ownership and the immutable relational link are enough to stop work;
        # a corrupt/lost positive binding must not stop a negative cancellation.
        job = self.workbench.jobs.cancel(context, job_id, key)
        return {**scope, "binding": binding, "job": job}

    def result(self, context: WorkspaceContext, selection_id: UUID, job_id: UUID) -> dict[str, Any]:
        from potbot.controller.task_execution_outcomes import result

        return result(self, context, selection_id, job_id)

    def artifact(
        self,
        context: WorkspaceContext,
        selection_id: UUID,
        job_id: UUID,
        *,
        offset: int = 0,
        length: int = 262_144,
    ) -> dict[str, Any]:
        view = self.inspect(context, selection_id, job_id)
        page = self.workbench.artifact(
            context, view["operationId"], job_id, offset=offset, length=length
        )
        return {
            **view,
            **{key: page[key] for key in ("artifact", "offset", "dataBase64", "nextOffset")},
        }
