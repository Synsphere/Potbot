"""A route cannot be removed or substituted after operator authorization."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.jobs import JobLease
from potbot.domain.operation_workbench import (
    sign_authorization,
    verify_check_authorization,
    verify_launch_authorization,
)
from tests.unit.test_operation_workbench import NOW, TOKEN, request


def test_routed_check_and_launch_bind_the_same_decision_and_legacy_omission():
    actor, spec, decision, check_id = (
        WorkspaceContext(uuid4(), uuid4()),
        request(),
        str(uuid4()),
        uuid4(),
    )
    signed = sign_authorization(
        actor, "a" * 64, spec, TOKEN, purpose="check", now=NOW, routing_decision_id=decision
    )
    value = {
        "operation_id": "a" * 64,
        "request": spec,
        "authorization": signed,
        "routing_decision_id": decision,
    }
    assert verify_check_authorization(actor, value, TOKEN, now=NOW)[1].to_dict() == spec
    for mutation in (None, str(uuid4())):
        changed = deepcopy(value)
        if mutation is None:
            del changed["routing_decision_id"]
        else:
            changed["routing_decision_id"] = mutation
        with pytest.raises(ScopeDeniedError):
            verify_check_authorization(actor, changed, TOKEN, now=NOW)
    launch = sign_authorization(
        actor,
        "a" * 64,
        spec,
        TOKEN,
        purpose="launch",
        check_id=check_id,
        now=NOW,
        routing_decision_id=decision,
    )
    lease = JobLease(
        actor.workspace_id,
        uuid4(),
        1,
        "worker",
        NOW + timedelta(seconds=30),
        spec["job_type"],
        UUID(spec["episode_id"]),
        spec["input"],
        spec["selected_models"],
        uuid4(),
        actor.principal_id,
        check_id,
        launch,
    )
    verify_launch_authorization(actor, lease, value, TOKEN, now=NOW)
    legacy = sign_authorization(
        actor, "a" * 64, spec, TOKEN, purpose="launch", check_id=check_id, now=NOW
    )
    with pytest.raises(ScopeDeniedError):
        verify_launch_authorization(
            actor, replace(lease, operation_authorization=legacy), value, TOKEN, now=NOW
        )
    assert sign_authorization(
        actor, "a" * 64, spec, TOKEN, purpose="check", now=NOW
    ) == sign_authorization(
        actor, "a" * 64, spec, TOKEN, purpose="check", now=NOW, routing_decision_id=None
    )


@pytest.mark.parametrize("value", ["not-a-uuid", str(uuid4()).upper(), 17, ""])
def test_invalid_decision_identity_cannot_be_signed(value):
    with pytest.raises(InvalidInputError):
        sign_authorization(
            WorkspaceContext(uuid4(), uuid4()),
            "a" * 64,
            request(),
            TOKEN,
            purpose="check",
            now=NOW,
            routing_decision_id=value,
        )
