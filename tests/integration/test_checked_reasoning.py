"""Real checked queue and accounting, with a controlled provider fixture."""

from uuid import UUID

import httpx
import pytest

from potbot.controller.checked_reasoning import CheckedReasoning
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from tests.integration import test_hosted_gateway as mock
from tests.integration.test_model_route_bindings import rig
from tests.integration.test_operation_checks import TOKEN, effects
from tests.unit.test_model_route_bindings import facts

pytestmark = pytest.mark.postgres


def begin(service, fixture, *, role="investigator", packet=None):
    return service.begin(
        fixture.actor,
        fixture.policy_id,
        "run-node",
        role=role,
        request=fixture.base,
        facts=packet or facts(),
        setup_token=TOKEN,
    )


def test_reasoning_check_launch_and_recovery_share_one_queue_and_charge(app_env, monkeypatch):
    original = mock.wire
    monkeypatch.setattr(mock, "wire", lambda: original(('{"queries":["read retained evidence"]}',)))
    r = rig(app_env)
    try:
        controller = CheckedReasoning(app_env.db)
        pending = begin(controller, r)
        decision_id = UUID(pending["decision_id"])
        assert pending["state"] == "checking" and pending["job_id"] is None
        recovered = CheckedReasoning(app_env.db)
        assert recovered.status(r.actor, decision_id) == pending == begin(recovered, r)
        with pytest.raises(StateMismatchError):
            recovered.launch(r.actor, decision_id, setup_token=TOKEN)
        with pytest.raises(ScopeDeniedError):
            CheckedReasoning(app_env.other_db).status(r.reviewer, decision_id)
        assert effects(r)["reservations"] == 0 and r.requests == []
        r.runner.run_once(r.actor, "reasoning-check")
        assert controller.status(r.actor, decision_id)["state"] == "checked"
        queued = controller.launch(r.actor, decision_id, setup_token=TOKEN)
        assert queued["state"] == "queued"
        assert recovered.launch(r.actor, decision_id, setup_token=TOKEN) == queued
        r.runner.run_once(r.actor, "reasoning-job")
        assert recovered.status(r.actor, decision_id)["state"] == "succeeded"
        answer = recovered.result(r.actor, decision_id)
        assert answer["job_id"] == queued["job_id"]
        assert answer["parsed"] == {"queries": ["read retained evidence"]}
        assert len(r.requests) == effects(r)["usage"] == 1
        assert r.runner.run_once(r.actor, "recovery-worker") is None
    finally:
        r.http.close()


def test_reasoning_role_and_frozen_facts_cannot_be_rebound(app_env):
    r = rig(app_env)
    try:
        controller = CheckedReasoning(app_env.db)
        with pytest.raises(StateMismatchError):
            begin(controller, r, role="critic")
        assert r.runner.run_once(r.actor, "no-check") is None
        pending = begin(controller, r)
        with pytest.raises(StateMismatchError):
            begin(controller, r, packet=facts() | {"task": "Changed intent"})
        assert controller.status(r.actor, UUID(pending["decision_id"])) == pending
    finally:
        r.http.close()


def test_reasoning_lost_reply_preserves_original_indeterminate_job(app_env):
    r = rig(app_env)
    try:

        def lost(request):
            raise httpx.ReadError("Synthetic response lost", request=request)

        r.callbacks.before_response = lost
        controller = CheckedReasoning(app_env.db)
        decision_id = UUID(begin(controller, r)["decision_id"])
        r.runner.run_once(r.actor, "reasoning-check")
        queued = controller.launch(r.actor, decision_id, setup_token=TOKEN)
        r.runner.run_once(r.actor, "reasoning-job")
        recovered = CheckedReasoning(app_env.db)
        original = recovered.status(r.actor, decision_id)
        assert original["job_id"] == queued["job_id"] and original["state"] == "indeterminate"
        assert recovered.launch(r.actor, decision_id, setup_token=TOKEN) == original
        with pytest.raises(StateMismatchError):
            recovered.result(r.actor, decision_id)
        assert r.runner.run_once(r.actor, "no-replay") is None and len(r.requests) == 1
        with app_env.db.transaction(r.actor) as conn:
            rows = conn.execute("SELECT status FROM budget_reservations").fetchall()
        assert {row["status"] for row in rows} == {"indeterminate"}
    finally:
        r.http.close()
