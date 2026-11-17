"""Operation authorization is exact, bounded and separate from execution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.jobs import CHECK_JOB, JobLease, JobSpec, result_metadata
from potbot.domain.operation_workbench import (
    CHECK_SCHEMA,
    parse_check_input,
    parse_check_report,
    parse_request,
    request_digest,
    sign_authorization,
    verify_check_authorization,
    verify_launch_authorization,
)
from potbot.domain.serialization import content_hash

TOKEN = "operation-setup-capability-1234567890-abcdefgh"
NOW = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)


def request():
    return {
        "job_type": "model.generate",
        "episode_id": str(uuid4()),
        "input": {"prompt": "private-prompt-canary", "max_output_tokens": 32},
        "selected_models": [{"provider": "openai", "model": "fixture-model", "role": "reasoning"}],
    }


def signed(actor, spec):
    return {
        "operation_id": "a" * 64,
        "request": spec,
        "authorization": sign_authorization(actor, "a" * 64, spec, TOKEN, purpose="check", now=NOW),
    }


def report():
    bindings = {"requestSha256": "b" * 64, "contractRevisionId": str(uuid4())}
    return {
        "schema": CHECK_SCHEMA,
        "operationId": "a" * 64,
        "eligible": True,
        "checks": [{"code": "task_current", "status": "passed"}],
        "bindings": bindings,
        "checkedAt": NOW.isoformat(),
        "expiresAt": (NOW + timedelta(seconds=120)).isoformat(),
        "bindingSha256": content_hash(bindings),
    }


def test_check_signature_binds_scope_operation_kind_and_complete_request():
    actor = WorkspaceContext(uuid4(), uuid4())
    spec = request()
    value = signed(actor, spec)
    identity, parsed = verify_check_authorization(actor, value, TOKEN, now=NOW)
    assert identity == "a" * 64 and parsed.to_dict() == spec
    for selected in (
        WorkspaceContext(uuid4(), actor.principal_id),
        WorkspaceContext(actor.workspace_id, uuid4()),
    ):
        with pytest.raises(ScopeDeniedError):
            verify_check_authorization(selected, value, TOKEN, now=NOW)
    for field in ("prompt", "max_output_tokens"):
        changed = deepcopy(value)
        changed["request"]["input"][field] = "changed" if field == "prompt" else 64
        with pytest.raises(ScopeDeniedError):
            verify_check_authorization(actor, changed, TOKEN, now=NOW)
    changed = deepcopy(value)
    changed["operation_id"] = "c" * 64
    with pytest.raises(ScopeDeniedError):
        verify_check_authorization(actor, changed, TOKEN, now=NOW)


@pytest.mark.parametrize(
    "token",
    (None, "", "x" * 31, "x" * 4097, TOKEN + "\n", "é" * 40, "changed-token-12345678901234567890"),
)
def test_missing_rotated_or_invalid_setup_capability_cannot_validate(token):
    actor = WorkspaceContext(uuid4(), uuid4())
    with pytest.raises(ScopeDeniedError):
        verify_check_authorization(actor, signed(actor, request()), token, now=NOW)


def test_check_and_launch_authorizations_are_distinct_and_expire():
    actor, spec, check_id = WorkspaceContext(uuid4(), uuid4()), request(), uuid4()
    value = signed(actor, spec)
    with pytest.raises(ScopeDeniedError):
        verify_check_authorization(actor, value, TOKEN, now=NOW + timedelta(seconds=600))
    authorization = sign_authorization(
        actor, "a" * 64, spec, TOKEN, purpose="launch", check_id=check_id, now=NOW
    )
    lease = JobLease(
        actor.workspace_id,
        uuid4(),
        1,
        "worker",
        NOW + timedelta(seconds=30),
        "model.generate",
        uuid4(),
        spec["input"],
        spec["selected_models"],
        uuid4(),
        actor.principal_id,
        check_id,
        authorization,
    )
    from uuid import UUID

    lease = replace(lease, episode_id=UUID(spec["episode_id"]))
    verify_launch_authorization(actor, lease, value, TOKEN, now=NOW)
    for changed in (
        replace(lease, operation_check_id=uuid4()),
        replace(lease, operation_authorization=value["authorization"]),
        replace(lease, input={"prompt": "different"}),
    ):
        with pytest.raises(ScopeDeniedError):
            verify_launch_authorization(actor, changed, value, TOKEN, now=NOW)


def test_check_job_accepts_only_signed_nonrecursive_execution_request():
    actor, spec = WorkspaceContext(uuid4(), uuid4()), request()
    value = signed(actor, spec)
    checked = JobSpec.from_dict(
        {
            "job_type": CHECK_JOB,
            "episode_id": spec["episode_id"],
            "input": value,
            "selected_models": [],
        }
    )
    assert parse_check_input(checked.input)[1].to_dict() == spec
    with pytest.raises(InvalidInputError):
        parse_request(checked.to_dict())
    with pytest.raises(InvalidInputError):
        JobSpec.from_dict({**checked.to_dict(), "episode_id": str(uuid4())})
    with pytest.raises(InvalidInputError):
        parse_check_input({"operation_id": "a" * 64, "request": spec})
    with pytest.raises(InvalidInputError):
        parse_request({**spec, "input": {"prompt": "x" * 16_384}})
    with pytest.raises(InvalidInputError):
        parse_request({**spec, "job_type": []})
    assert request_digest(spec) == request_digest(deepcopy(spec))


@pytest.mark.parametrize(
    "change",
    (
        {"eligible": False},
        {"secret": "private"},
        {"bindingSha256": "f" * 64},
        {"checks": []},
        {"checks": [{"code": "raw private output", "status": "passed"}]},
        {"checks": [{"code": "denied", "status": "blocked"}]},
        {"checks": [{"code": "malformed", "status": []}]},
        {"expiresAt": (NOW + timedelta(seconds=301)).isoformat()},
        {"checkedAt": "2026-11-17T20:00:00"},
        {
            "bindings": {
                "requestSha256": "b" * 64,
                "contractRevisionId": str(uuid4()),
                "secret": "private",
            }
        },
    ),
)
def test_public_check_report_rejects_unsafe_or_inconsistent_fields(change):
    with pytest.raises(InvalidInputError):
        parse_check_report({**report(), **change})


def test_report_results_have_only_safe_bounded_metadata():
    expected = report()
    assert result_metadata({"operation_check": expected}) == {"operation_check": expected}
    assert TOKEN not in str(expected) and "private-prompt-canary" not in str(expected)
    with pytest.raises(InvalidInputError):
        result_metadata({"operation_check": expected, "model": "fixture-model"})
    expected["checks"] = [{"code": "credential_unavailable", "status": "blocked"}]
    expected["eligible"] = False
    assert parse_check_report(expected)["eligible"] is False
