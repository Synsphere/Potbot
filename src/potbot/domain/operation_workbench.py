"""Strict, value-blind operation check reports and scoped operator authorizations."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash

if TYPE_CHECKING:
    from potbot.domain.jobs import JobLease, JobSpec

SCHEMA = "potbot-operation-workbench/v1"
CHECK_SCHEMA = "potbot-operation-check/v1"
MAX_INPUT_BYTES = 16_384
MAX_OUTPUT_BYTES = 262_144
MAX_AUTHORIZATION_SECONDS = 600
MAX_CHECK_SECONDS = 300
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BINDINGS = {
    "requestSha256",
    "contractRevisionId",
    "policySha256",
    "profileSha256",
    "installationSha256",
    "grantGeneration",
    "grantSha256",
    "repositorySha256",
    "nativeSha256",
    "runtimeSha256",
    "releaseSha256",
    "taskExecutionSha256",
    "modelRoutingSha256",
    "modelPromptSha256",
    "modelOutputSchemaSha256",
    "modelRuntimeSha256",
}


def _valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 4096
        and all(33 <= ord(c) <= 126 for c in value)
    )


def operation_id(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("Operation identity must be lowercase SHA-256")
    return value


def canonical_uuid(value: object) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("Operation identity requires a canonical UUID") from None


def parse_request(value: object) -> JobSpec:
    from potbot.domain.jobs import COG_JOB, JobSpec

    if (
        not isinstance(value, dict)
        or not isinstance(value.get("job_type"), str)
        or value["job_type"] not in {COG_JOB, "model.generate"}
    ):
        raise InvalidInputError("Operation requests require an existing execution handler")
    try:
        if len(canonical_json(value).encode()) > MAX_INPUT_BYTES:
            raise InvalidInputError("Operation request exceeds its byte allowance")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidInputError("Operation request must be bounded JSON") from None
    return JobSpec.from_dict(value)


def request_digest(value: object) -> str:
    return content_hash(parse_request(value).to_dict())


def _authorization(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"expires_at", "mac"}
        or type(value["expires_at"]) is not int
        or not 0 < value["expires_at"] < 2**53
    ):
        raise InvalidInputError("Operation authorization is malformed")
    operation_id(value["mac"])
    return value


def parse_check_input(value: object) -> tuple[str, JobSpec]:
    required = {"operation_id", "request", "authorization"}
    optional = {"task_execution_sha256", "routing_decision_id"}
    if not isinstance(value, dict) or not required <= set(value) <= required | optional:
        raise InvalidInputError("Operation check fields do not match the supported contract")
    _authorization(value["authorization"])
    if "task_execution_sha256" in value:
        operation_id(value["task_execution_sha256"])
    if "routing_decision_id" in value:
        canonical_uuid(value["routing_decision_id"])
    return operation_id(value["operation_id"]), parse_request(value["request"])


def _body(
    actor: WorkspaceContext,
    identity: str,
    request: object,
    expires: int,
    *,
    purpose: str,
    check_id: UUID | None,
    task_execution_sha256: str | None = None,
    routing_decision_id: str | None = None,
) -> bytes:
    spec = parse_request(request)
    value = {
        "schema": "potbot-operation-authorization/v1",
        "purpose": purpose,
        "workspace_id": str(actor.workspace_id),
        "principal_id": str(actor.principal_id),
        "operation_id": operation_id(identity),
        "job_type": spec.job_type,
        "request_sha256": content_hash(spec.to_dict()),
        "expires_at": expires,
        "check_job_id": str(check_id) if check_id else None,
    }
    if task_execution_sha256 is not None:
        value["task_execution_sha256"] = operation_id(task_execution_sha256)
    if routing_decision_id is not None:
        value["routing_decision_id"] = str(canonical_uuid(routing_decision_id))
    return canonical_json(value).encode()


def sign_authorization(
    actor: WorkspaceContext,
    identity: str,
    request: object,
    setup_token: str,
    *,
    purpose: str,
    check_id: UUID | None = None,
    expires_at: int | None = None,
    now: datetime | None = None,
    task_execution_sha256: str | None = None,
    routing_decision_id: str | None = None,
) -> dict[str, Any]:
    current = int((now or datetime.now(UTC)).timestamp())
    expires = expires_at if expires_at is not None else current + MAX_AUTHORIZATION_SECONDS
    if (
        purpose not in {"check", "launch"}
        or (purpose == "launch") != (check_id is not None)
        or not _valid_token(setup_token)
        or not current < expires <= current + MAX_AUTHORIZATION_SECONDS
    ):
        raise ScopeDeniedError("Current operator authorization is required")
    return {
        "expires_at": expires,
        "mac": hmac.new(
            setup_token.encode(),
            _body(
                actor,
                identity,
                request,
                expires,
                purpose=purpose,
                check_id=check_id,
                task_execution_sha256=task_execution_sha256,
                routing_decision_id=routing_decision_id,
            ),
            hashlib.sha256,
        ).hexdigest(),
    }


def _verify(
    actor: WorkspaceContext,
    identity: str,
    request: object,
    authorization: object,
    setup_token: str | None,
    *,
    purpose: str,
    check_id: UUID | None,
    now: datetime | None,
    task_execution_sha256: str | None = None,
    routing_decision_id: str | None = None,
    require_current: bool = True,
) -> None:
    authorization = _authorization(authorization)
    current = int((now or datetime.now(UTC)).timestamp())
    expires = authorization["expires_at"]
    if not _valid_token(setup_token) or (
        require_current and not current < expires <= current + MAX_AUTHORIZATION_SECONDS
    ):
        raise ScopeDeniedError("Operation authorization is unavailable or expired")
    assert isinstance(setup_token, str)
    expected = hmac.new(
        setup_token.encode(),
        _body(
            actor,
            identity,
            request,
            expires,
            purpose=purpose,
            check_id=check_id,
            task_execution_sha256=task_execution_sha256,
            routing_decision_id=routing_decision_id,
        ),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, authorization["mac"]):
        raise ScopeDeniedError("Operation authorization no longer matches the request")


def verify_check_authorization(
    actor: WorkspaceContext,
    value: object,
    setup_token: str | None,
    *,
    now: datetime | None = None,
    require_current: bool = True,
) -> tuple[str, JobSpec]:
    identity, spec = parse_check_input(value)
    assert isinstance(value, dict)
    _verify(
        actor,
        identity,
        spec.to_dict(),
        value["authorization"],
        setup_token,
        purpose="check",
        check_id=None,
        now=now,
        task_execution_sha256=value.get("task_execution_sha256"),
        routing_decision_id=value.get("routing_decision_id"),
        require_current=require_current,
    )
    return identity, spec


def verify_launch_authorization(
    actor: WorkspaceContext,
    lease: JobLease,
    original_input: object,
    setup_token: str | None,
    *,
    now: datetime | None = None,
    require_current: bool = True,
) -> None:
    identity, request = verify_check_authorization(
        actor, original_input, setup_token, now=now, require_current=require_current
    )
    if lease.operation_check_id is None:
        raise ScopeDeniedError("Operation launch requires its original check")
    actual = {
        "job_type": lease.job_type,
        "episode_id": str(lease.episode_id),
        "input": lease.input,
        "selected_models": lease.selected_models,
    }
    if request_digest(actual) != request_digest(request.to_dict()):
        raise ScopeDeniedError("Operation launch differs from the checked request")
    assert isinstance(original_input, dict)
    binding = lease.task_execution_binding
    if original_input.get("task_execution_sha256") != (
        binding.get("sha256") if binding is not None else None
    ):
        raise ScopeDeniedError("Operation launch differs from the selected task binding")
    _verify(
        actor,
        identity,
        actual,
        lease.operation_authorization,
        setup_token,
        purpose="launch",
        check_id=lease.operation_check_id,
        now=now,
        task_execution_sha256=original_input.get("task_execution_sha256"),
        routing_decision_id=original_input.get("routing_decision_id"),
        require_current=require_current,
    )


def report_time(value: object) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = result.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("Operation check time must be UTC") from None


def parse_check_report(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "operationId",
            "eligible",
            "checks",
            "bindings",
            "checkedAt",
            "expiresAt",
            "bindingSha256",
        }
        or value["schema"] != CHECK_SCHEMA
        or type(value["eligible"]) is not bool
    ):
        raise InvalidInputError("Operation check report fields do not match the public contract")
    operation_id(value["operationId"])
    operation_id(value["bindingSha256"])
    checks, bindings = value["checks"], value["bindings"]
    if (
        not isinstance(checks, list)
        or not 1 <= len(checks) <= 32
        or any(
            not isinstance(row, dict)
            or set(row) != {"code", "status"}
            or not isinstance(row["code"], str)
            or not _CODE.fullmatch(row["code"])
            or not isinstance(row["status"], str)
            or row["status"] not in {"passed", "blocked", "unsupported"}
            for row in checks
        )
        or len({row["code"] for row in checks}) != len(checks)
        or value["eligible"] != all(row["status"] == "passed" for row in checks)
    ):
        raise InvalidInputError("Operation check statuses are inconsistent")
    if (
        not isinstance(bindings, dict)
        or not {"requestSha256", "contractRevisionId"} <= set(bindings) <= _BINDINGS
    ):
        raise InvalidInputError("Operation check bindings are malformed")
    for key, binding in bindings.items():
        if key == "contractRevisionId":
            canonical_uuid(binding)
        elif key == "grantGeneration":
            if type(binding) is not int or not 1 <= binding <= 2**53 - 1:
                raise InvalidInputError("Operation grant generation is invalid")
        else:
            operation_id(binding)
    checked, expires = report_time(value["checkedAt"]), report_time(value["expiresAt"])
    if not 0 < (expires - checked).total_seconds() <= MAX_CHECK_SECONDS:
        raise InvalidInputError("Operation check validity exceeds its bound")
    if value["bindingSha256"] != content_hash(bindings):
        raise InvalidInputError("Operation check binding identity differs")
    return json_copy(value)


def json_copy(value: dict[str, Any]) -> dict[str, Any]:
    import json

    return dict(json.loads(canonical_json(value)))
