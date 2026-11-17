"""A check's operator capability is verified before any worker dependency."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.controller.operation_checks import OperationChecks
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.jobs import CHECK_JOB, JobLease
from potbot.domain.operation_workbench import sign_authorization

TOKEN = "operation-check-test-setup-capability-123456789"


class Untouched:
    def __getattr__(self, name):
        raise AssertionError(f"Untrusted check touched worker dependency: {name}")


def request():
    actor = WorkspaceContext(uuid4(), uuid4())
    now = datetime.now(UTC)
    spec = {
        "job_type": "model.generate",
        "episode_id": str(uuid4()),
        "input": {"prompt": "private input", "max_output_tokens": 8},
        "selected_models": [{"provider": "openai", "model": "fixture", "role": "reasoning"}],
    }
    data = {
        "operation_id": "a" * 64,
        "request": spec,
        "authorization": sign_authorization(actor, "a" * 64, spec, TOKEN, purpose="check", now=now),
    }
    from uuid import UUID

    lease = JobLease(
        actor.workspace_id,
        uuid4(),
        1,
        "checks",
        now + timedelta(minutes=1),
        CHECK_JOB,
        UUID(spec["episode_id"]),
        data,
        [],
        uuid4(),
        owner_principal_id=actor.principal_id,
    )
    return actor, now, lease


@pytest.mark.parametrize("change", ["mac", "prompt", "scope", "expired", "rotated", "locked"])
def test_untrusted_check_never_reaches_database_credentials_runtime_or_provider(change):
    actor, now, lease = request()
    data = deepcopy(lease.input)
    token = TOKEN
    if change == "mac":
        data["authorization"]["mac"] = "0" * 64
    elif change == "prompt":
        data["request"]["input"]["prompt"] = "tampered"
    elif change == "scope":
        actor = WorkspaceContext(actor.workspace_id, uuid4())
    elif change == "expired":
        now += timedelta(minutes=11)
    elif change == "rotated":
        token += "rotated"
    else:
        token = None
    checks = OperationChecks(
        Untouched(), Untouched(), Untouched(), setup_token=lambda: token, clock=lambda: now
    )
    with pytest.raises(ScopeDeniedError):
        checks.evaluate(actor, replace(lease, input=data))


def test_locked_setup_store_never_falls_back_to_provider_resolver():
    actor, now, lease = request()

    def locked():
        raise RuntimeError("private store path and token must not escape")

    checks = OperationChecks(Untouched(), Untouched(), None, setup_token=locked, clock=lambda: now)
    with pytest.raises(ScopeDeniedError, match="unavailable or expired"):
        checks.evaluate(actor, lease)


def test_unlinked_legacy_job_does_not_require_new_setup_capability():
    actor, _now, lease = request()
    OperationChecks(Untouched(), Untouched(), None).revalidate(
        actor, replace(lease, job_type="model.generate")
    )


@pytest.mark.parametrize("change", ["mac", "request", "check", "rotated", "expired", "purpose"])
def test_launch_authorization_is_checked_before_runtime_and_provider_dependencies(change):
    actor, now, checked = request()
    data = deepcopy(checked.input)
    spec = data["request"]
    authorization = sign_authorization(
        actor,
        data["operation_id"],
        spec,
        TOKEN,
        purpose="launch",
        check_id=checked.job_id,
        expires_at=int(now.timestamp()) + 60,
        now=now,
    )
    lease = replace(
        checked,
        job_id=uuid4(),
        job_type=spec["job_type"],
        input=deepcopy(spec["input"]),
        selected_models=spec["selected_models"],
        operation_check_id=checked.job_id,
        operation_authorization=authorization,
    )
    token = TOKEN
    if change == "mac":
        authorization["mac"] = "0" * 64
    elif change == "request":
        lease.input["prompt"] = "changed after checking"
    elif change == "check":
        lease = replace(lease, operation_check_id=uuid4())
    elif change == "rotated":
        token += "rotated"
    elif change == "expired":
        now += timedelta(seconds=61)
    else:
        lease = replace(lease, operation_authorization=data["authorization"])
    jobs = SimpleNamespace(operation_check=lambda *_: {"input": data})
    checks = OperationChecks(
        jobs, Untouched(), Untouched(), setup_token=lambda: token, clock=lambda: now
    )
    with pytest.raises(ScopeDeniedError):
        checks.revalidate(actor, lease)
