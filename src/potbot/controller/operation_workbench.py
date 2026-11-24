"""Operator-controlled operation lifecycle over the existing durable queue.

The API process never initializes a native worker, probes an installation or
contacts a model. Signed checks execute in the configured queue worker; launch
is a separate signed decision and repeats admission on that worker.
"""

from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from potbot.controller.jobs import JobService, _public, _read
from potbot.controller.operation_catalogue import OperationCatalogue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.jobs import CHECK_JOB, COG_JOB, JobSpec, JobState
from potbot.domain.operation_workbench import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    SCHEMA,
    operation_id,
    parse_check_input,
    parse_check_report,
    parse_request,
    report_time,
    request_digest,
    sign_authorization,
    verify_check_authorization,
)
from potbot.domain.serialization import canonical_json
from potbot.persistence.database import Database

MAX_PREVIEW_BYTES = 65_536
MAX_EVENT_PAGE_BYTES = 524_288


class OperationWorkbench:
    """One scoped lifecycle, retaining catalogue, job and ledger authorities."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.jobs = JobService(database)
        self.catalogue = OperationCatalogue(database)

    @staticmethod
    def _scope(context: WorkspaceContext, identity: str) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "operationId": operation_id(identity),
        }

    @staticmethod
    def _route(item: dict[str, Any]) -> str | None:
        if item["kind"] == "model" and item["reference"].startswith("model:"):
            return "model.generate"
        if item["kind"] == "cog" and item["reference"].startswith("syn://cog/"):
            return COG_JOB
        if item["reference"].startswith("syn://thing/"):
            return COG_JOB
        return None

    @staticmethod
    def _execution_reference(item: dict[str, Any]) -> str:
        # Native Cog adapters already bind signature.cog_ref.slug to a native
        # thing with the same slug. The worker must still prove installation.
        reference: str = item["reference"]
        return reference.replace("syn://cog/", "syn://thing/", 1)

    def describe(self, context: WorkspaceContext, identity: str) -> dict[str, Any]:
        item = self.catalogue.inspect(context, operation_id(identity))["item"]
        route = self._route(item)
        return {
            **self._scope(context, identity),
            "kind": item["kind"],
            "reference": item["reference"],
            "name": item["name"],
            "supported": route is not None,
            "jobType": route,
            "executionReference": self._execution_reference(item) if route == COG_JOB else None,
            "inputFields": (
                [
                    "episode_id",
                    "agent_id",
                    "repository_state_id",
                    "profile_sha256",
                    "type_ref",
                    "value",
                ]
                if route == COG_JOB
                else [
                    "episode_id",
                    "provider",
                    "role",
                    "prompt",
                    "max_output_tokens",
                    "timeout_seconds",
                ]
                if route == "model.generate"
                else []
            ),
            "reasonCodes": ["worker_check_required", "operator_authorization_required"]
            if route
            else ["operation_has_no_installed_job_route"],
            "maxInputBytes": MAX_INPUT_BYTES,
        }

    def _request(self, context: WorkspaceContext, identity: str, spec: JobSpec) -> None:
        item = self.catalogue.inspect(context, identity)["item"]
        route = self._route(item)
        if route is None or route != spec.job_type:
            raise ScopeDeniedError("This catalogue entry has no matching execution route")
        if route == COG_JOB:
            if spec.input["operation_ref"].replace(
                "synny://", "syn://"
            ) != self._execution_reference(item):
                raise ScopeDeniedError("The request selects another operation")
        elif (
            len(spec.selected_models) != 1
            or "model:" + spec.selected_models[0]["model"] != item["reference"]
        ):
            raise ScopeDeniedError("Select exactly the catalogue model for this operation")

    def check(
        self,
        context: WorkspaceContext,
        identity: str,
        request: object,
        key: str,
        *,
        setup_token: str,
        task_execution_binding: dict[str, Any] | None = None,
        task_execution_artifacts: Any = None,
        routing_decision_id: UUID | None = None,
    ) -> dict[str, Any]:
        if routing_decision_id is not None and not isinstance(routing_decision_id, UUID):
            raise InvalidInputError("Model routing requires a decision UUID")
        identity, spec = operation_id(identity), parse_request(request)
        authorization = sign_authorization(
            context,
            identity,
            spec.to_dict(),
            setup_token,
            purpose="check",
            routing_decision_id=str(routing_decision_id) if routing_decision_id else None,
            task_execution_sha256=task_execution_binding["sha256"]
            if task_execution_binding
            else None,
        )

        def validate(_connection: Any, episode: dict[str, Any]) -> None:
            self._request(context, identity, spec)
            if episode["status"] != "running":
                raise ScopeDeniedError("Operation checks require a running task")
            if task_execution_binding is not None:
                from potbot.controller.task_execution import validate_current_binding

                validate_current_binding(
                    self.database,
                    context,
                    _connection,
                    task_execution_binding,
                    setup_token=setup_token,
                    artifacts=task_execution_artifacts,
                )

        job = self.jobs.submit(
            context,
            {
                "job_type": CHECK_JOB,
                "episode_id": str(spec.episode_id),
                "input": {
                    "operation_id": identity,
                    "request": spec.to_dict(),
                    "authorization": authorization,
                    **(
                        {"routing_decision_id": str(routing_decision_id)}
                        if routing_decision_id
                        else {}
                    ),
                    **(
                        {"task_execution_sha256": task_execution_binding["sha256"]}
                        if task_execution_binding
                        else {}
                    ),
                },
                "selected_models": [],
            },
            key,
            trusted_operation=True,
            create_guard=validate,
            task_execution_binding=task_execution_binding,
        )
        return {**self._scope(context, identity), "job": job}

    def launch(
        self,
        context: WorkspaceContext,
        identity: str,
        check_job_id: UUID,
        request: object,
        key: str,
        *,
        setup_token: str,
        task_execution_binding: dict[str, Any] | None = None,
        task_execution_artifacts: Any = None,
    ) -> dict[str, Any]:
        identity, spec = operation_id(identity), parse_request(request)
        if not isinstance(check_job_id, UUID):
            raise InvalidInputError("Operation launch requires a check UUID")
        authorization: dict[str, Any] = {}

        def validate(connection: Any, episode: dict[str, Any]) -> None:
            self._request(context, identity, spec)
            check = _read(connection, context, check_job_id, lock=True)
            if check.get("task_execution_binding") != task_execution_binding:
                raise ScopeDeniedError("Launch must retain the original selected task binding")
            if task_execution_binding is not None:
                from potbot.controller.task_execution import validate_current_binding

                validate_current_binding(
                    self.database,
                    context,
                    connection,
                    task_execution_binding,
                    setup_token=setup_token,
                    artifacts=task_execution_artifacts,
                )
            if check["job_type"] != CHECK_JOB or check["state"] != JobState.SUCCEEDED:
                raise ScopeDeniedError("A completed eligible check is required")
            checked_identity, checked = verify_check_authorization(
                context, check["input"], setup_token
            )
            report = parse_check_report((check["result"] or {}).get("operation_check"))
            current = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if (
                checked_identity != identity
                or report["operationId"] != identity
                or request_digest(checked.to_dict()) != request_digest(spec.to_dict())
                or report["bindings"]["requestSha256"] != request_digest(spec.to_dict())
                or report["bindings"]["contractRevisionId"] != str(check["contract_revision_id"])
                or not report["eligible"]
                or report_time(report["expiresAt"]) <= current
                or report_time(report["checkedAt"]) > current
                or check["episode_id"] != spec.episode_id
                or check["contract_revision_id"] != episode["active_contract_id"]
                or episode["status"] != "running"
            ):
                raise RevisionConflictError("Operation check changed or expired; check again")
            if (
                connection.execute(
                    "SELECT 1 FROM jobs WHERE workspace_id=%s AND operation_check_id=%s",
                    (context.workspace_id, check_job_id),
                ).fetchone()
                is not None
            ):
                raise RevisionConflictError("This operation check already has a run")
            authorization.update(
                sign_authorization(
                    context,
                    identity,
                    spec.to_dict(),
                    setup_token,
                    purpose="launch",
                    check_id=check_job_id,
                    routing_decision_id=check["input"].get("routing_decision_id"),
                    expires_at=int(report_time(report["expiresAt"]).timestamp()),
                    now=current,
                    task_execution_sha256=task_execution_binding["sha256"]
                    if task_execution_binding
                    else None,
                )
            )

        job = self.jobs.submit(
            context,
            spec.to_dict(),
            key,
            trusted_operation=True,
            operation_check_id=check_job_id,
            operation_authorization=authorization,
            create_guard=validate,
            task_execution_binding=task_execution_binding,
        )
        # Idempotent retries skip fresh authorization/expiry validation. They
        # must still belong to the operation named in this route.
        self._linked(context, identity, UUID(job["id"]))
        return {**self._scope(context, identity), "job": job}

    def _linked(self, context: WorkspaceContext, identity: str, job_id: UUID) -> dict[str, Any]:
        operation_id(identity)
        with self.database.transaction(context) as connection:
            row = _read(connection, context, job_id)
            if row["job_type"] == CHECK_JOB:
                check = row
            elif row["operation_check_id"] is not None:
                check = _read(connection, context, row["operation_check_id"])
            else:
                raise ScopeDeniedError("The job does not belong to this operation workbench")
            checked_identity, _ = parse_check_input(check["input"])
            if checked_identity != identity:
                raise ScopeDeniedError("The job belongs to another operation")
            return row

    def inspect(self, context: WorkspaceContext, identity: str, job_id: UUID) -> dict[str, Any]:
        self._linked(context, identity, job_id)
        return {**self._scope(context, identity), "job": self.jobs.inspect(context, job_id)}

    def events(
        self,
        context: WorkspaceContext,
        identity: str,
        job_id: UUID,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._linked(context, identity, job_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("Operation events require a bounded page")
        page = self.jobs.events(context, job_id, after=after, limit=limit)
        retained, size = [], 0
        for event in page["items"]:
            amount = len(canonical_json(event).encode())
            if size + amount > MAX_EVENT_PAGE_BYTES:
                break
            retained.append(event)
            size += amount
        if page["items"] and not retained:
            raise InvalidInputError("Operation event exceeds the public page bound")
        return {
            **self._scope(context, identity),
            "items": retained,
            "next_cursor": retained[-1]["sequence"] if retained else after,
            "has_more": page["has_more"] or len(retained) < len(page["items"]),
        }

    def cancel(
        self,
        context: WorkspaceContext,
        identity: str,
        job_id: UUID,
        key: str,
    ) -> dict[str, Any]:
        self._linked(context, identity, job_id)
        return {**self._scope(context, identity), "job": self.jobs.cancel(context, job_id, key)}

    def result(self, context: WorkspaceContext, identity: str, job_id: UUID) -> dict[str, Any]:
        row = self._linked(context, identity, job_id)
        with self.database.transaction(context) as connection:
            # Re-read under current membership/owner policy before the result.
            row = _read(connection, context, job_id)
            chunks = connection.execute(
                "SELECT data->>'text' AS text FROM job_events WHERE workspace_id=%s "
                "AND job_id=%s AND event_type='job.output' ORDER BY sequence LIMIT 512",
                (context.workspace_id, job_id),
            ).fetchall()
            output = b""
            for chunk in chunks:
                if isinstance(chunk["text"], str):
                    output += chunk["text"].encode()[: MAX_PREVIEW_BYTES - len(output)]
                if len(output) >= MAX_PREVIEW_BYTES:
                    break
            artifact = None
            reference = (row["result"] or {}).get("artifact_id")
            if reference is not None:
                record = connection.execute(
                    "SELECT id,digest,byte_length,media_type FROM artifacts "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, UUID(reference)),
                ).fetchone()
                if record is not None:
                    artifact = {
                        "id": str(record["id"]),
                        "sha256": record["digest"],
                        "bytes": record["byte_length"],
                        "mediaType": record["media_type"],
                    }
            result = {
                **self._scope(context, identity),
                "job": _public(row),
                "output": output.decode("utf-8", errors="ignore"),
                "truncated": row["output_bytes"] > len(output),
                "artifact": artifact,
                "artifactText": None,
                "artifactTruncated": False,
            }
        if artifact is not None:
            page = self.artifact(context, identity, job_id, length=MAX_PREVIEW_BYTES)
            if artifact["mediaType"] in {"application/json", "text/plain"}:
                result["artifactText"] = base64.b64decode(page["dataBase64"]).decode(
                    "utf-8", errors="replace"
                )
                result["artifactTruncated"] = page["nextOffset"] is not None
        return result

    def artifact(
        self,
        context: WorkspaceContext,
        identity: str,
        job_id: UUID,
        *,
        offset: int = 0,
        length: int = MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        """Read only a verified bounded range of this owned job's result artifact."""
        from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
        from potbot.workspace_sandbox import load_workspace_sandbox

        if (
            type(offset) is not int
            or offset < 0
            or type(length) is not int
            or not 1 <= length <= MAX_OUTPUT_BYTES
        ):
            raise InvalidInputError("Operation artifact range is invalid")
        row = self._linked(context, identity, job_id)
        reference = (row["result"] or {}).get("artifact_id")
        if reference is None:
            raise ScopeDeniedError("No result artifact is available for this operation")
        sandbox = load_workspace_sandbox(self.database, context)
        store = ArtifactStore(
            self.database,
            LocalArtifactBackend(sandbox.path("artifacts"), max_range_bytes=MAX_OUTPUT_BYTES),
        )
        metadata = store.metadata(context, UUID(reference))
        if offset > metadata["byte_length"]:
            raise InvalidInputError("Operation artifact offset exceeds its length")
        verified = store.read_range(
            context,
            UUID(reference),
            expected_sha256=metadata["digest"],
            expected_byte_length=metadata["byte_length"],
            offset=offset,
            length=length,
        )
        next_offset = offset + len(verified.data)
        return {
            **self._scope(context, identity),
            "artifact": {
                "id": str(metadata["id"]),
                "sha256": metadata["digest"],
                "bytes": metadata["byte_length"],
                "mediaType": metadata["media_type"],
            },
            "offset": offset,
            "dataBase64": base64.b64encode(verified.data).decode("ascii"),
            "nextOffset": next_offset if next_offset < metadata["byte_length"] else None,
        }
