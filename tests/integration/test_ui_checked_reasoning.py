"""The operator API progresses owned runs through the existing checked worker."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from potbot.api.server import ServerSettings, create_app
from potbot.api.tasks import DevelopmentBearerAuth
from potbot.api.ui_checked_reasoning import register_checked_reasoning_routes
from potbot.controller.tasks import TaskService
from potbot.domain.errors import ScopeDeniedError
from tests.integration import test_hosted_gateway as mock
from tests.integration import test_routed_reasoning
from tests.integration.test_operation_checks import TOKEN, effects
from tests.integration.test_routed_reasoning import (
    PROPOSALS,
    SYNTHESIS,
    _critique,
    _fixture_packet,
    _worker,
)

pytestmark = pytest.mark.postgres
routed = test_routed_reasoning.routed
_ORIGIN = "http://operator.test"
_OWNER = "fixture-operator-access-token-0001"
_OTHER = "fixture-reviewer-access-token-0002"


def _client(routed):
    authenticate = DevelopmentBearerAuth({_OWNER: routed.actor, _OTHER: routed.reviewer})

    def authorize(token):
        if token != TOKEN:
            raise ScopeDeniedError("Invalid fixture setup token")

    app = create_app(
        task_service=TaskService(routed.env.db),
        job_service=routed.jobs,
        authenticate=authenticate,
        settings=ServerSettings(allowed_origins=(_ORIGIN,)),
    )
    register_checked_reasoning_routes(
        app, routed.env.db, authenticate, (_ORIGIN,), routed.actor.workspace_id, authorize
    )
    return TestClient(app)


def _headers(routed):
    return {
        "Authorization": f"Bearer {_OWNER}",
        "Origin": _ORIGIN,
        "X-Potbot-Expected-Principal": str(routed.actor.principal_id),
    }


def _ok(response):
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    return response.json()


def test_operator_finishes_and_reopens_three_checked_stages(routed, monkeypatch):
    path = f"/v1/ui/checked-reasoning/{uuid4()}"
    headers = _headers(routed)
    wire = mock.wire

    def reply():
        if len(routed.requests) == 1:
            answer = json.dumps(PROPOSALS)
        elif len(routed.requests) == 2:
            answer = json.dumps(_critique(_fixture_packet(routed.requests[-1])))
        else:
            assert len(routed.requests) == 3
            answer = SYNTHESIS
        return wire((answer,))

    monkeypatch.setattr(mock, "wire", reply)
    with _client(routed) as client:
        body = {"confirm": True, "plan": routed.plan}
        started = _ok(client.post(path + "/start", headers=headers, json=body))
        assert _ok(client.post(path + "/start", headers=headers, json=body)) == started
        assert started["stage_index"] == 0 and started["check_job_id"] is None
        assert routed.runner.run_once(routed.actor, "http-start-is-inert") is None

    # Recreate the app and controller before continuing with the same run.
    with _client(routed) as client:
        assert _ok(client.get(path, headers=headers)) == started
        assert client.get(path + "/result", headers=headers).status_code == 409

        def advance(index):
            return client.post(
                path + "/advance",
                headers={**headers, "X-Potbot-Setup-Token": TOKEN},
                json={"confirm": True, "expected_stage": index},
            )

        for index in range(3):
            checking = _ok(advance(index))
            assert _worker(routed, "succeeded")["id"] == checking["check_job_id"]
            queued = _ok(advance(index))
            assert checking["decision_id"] == queued["decision_id"]
            assert _ok(advance(index)) == queued
            assert _worker(routed, "succeeded")["id"] == queued["job_id"]
            adopted = _ok(advance(index))
            assert adopted["stage_index"] == index + 1
            if index < 2:
                assert advance(index).status_code == 409
            else:
                assert _ok(advance(index)) == adopted
            assert routed.runner.run_once(routed.actor, "no-replayed-successor") is None

        assert adopted["state"] == "completed" and len(adopted["completed"]) == 3
        result = _ok(client.get(path + "/result", headers=headers))
        assert result["text"] == SYNTHESIS
        assert _ok(client.get(path + "/result", headers=headers)) == result
    assert effects(routed)["usage"] == len(routed.requests) == 3


def test_operator_interrupt_fences_through_same_owned_scope(routed):
    path = f"/v1/ui/checked-reasoning/{uuid4()}"
    headers = _headers(routed)
    with _client(routed) as client:
        body = {"confirm": True, "plan": routed.plan}
        assert client.post(path + "/start", json=body).status_code == 404
        assert (
            client.post(
                path + "/start", headers={**headers, "Origin": "http://wrong.test"}, json=body
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/start",
                headers={**headers, "X-Potbot-Expected-Principal": str(uuid4())},
                json=body,
            ).status_code
            == 404
        )
        started = _ok(client.post(path + "/start", headers=headers, json=body))
        assert (
            client.post(
                path + "/start",
                headers=headers,
                json={**body, "plan": {**routed.plan, "task": "Other"}},
            ).status_code
            == 409
        )
        foreign = {
            **headers,
            "Authorization": f"Bearer {_OTHER}",
            "X-Potbot-Expected-Principal": str(routed.reviewer.principal_id),
        }
        assert client.get(path, headers=foreign).status_code == 404
        assert (
            client.post(path + "/interrupt", headers=foreign, json={"confirm": True}).status_code
            == 404
        )
        tick = {"confirm": True, "expected_stage": 0}
        assert client.post(path + "/advance", headers=headers, json=tick).status_code == 404
        advance_headers = {**headers, "X-Potbot-Setup-Token": TOKEN}
        assert (
            client.post(
                path + "/advance", headers=advance_headers, json={**tick, "expected_stage": True}
            ).status_code
            == 400
        )
        assert _ok(client.get(path, headers=headers)) == started
        checking = _ok(client.post(path + "/advance", headers=advance_headers, json=tick))
        assert _worker(routed, "succeeded")["id"] == checking["check_job_id"]
        queued = _ok(client.post(path + "/advance", headers=advance_headers, json=tick))
        stopped = _ok(client.post(path + "/interrupt", headers=headers, json={"confirm": True}))
        assert stopped["state"] == "interrupted" and stopped["job_id"] == queued["job_id"]
        assert _ok(client.post(path + "/advance", headers=advance_headers, json=tick)) == stopped
        assert (
            _ok(client.post(path + "/interrupt", headers=headers, json={"confirm": True}))
            == stopped
        )
    assert routed.runner.run_once(routed.actor, "operator-stop-fence") is None
    job = routed.jobs.inspect(routed.actor, UUID(queued["job_id"]))
    assert job["dispatched_at"] is None and job["state"] == "cancelled"
    assert routed.requests == [] and effects(routed)["reservations"] == 0


def test_operator_lost_provider_reply_preserves_original_job_and_hold(routed):
    path = f"/v1/ui/checked-reasoning/{uuid4()}"
    headers = {**_headers(routed), "X-Potbot-Setup-Token": TOKEN}

    def lose_reply(request):
        raise httpx.ReadError("Synthetic provider reply lost", request=request)

    routed.callbacks.before_response = lose_reply
    with _client(routed) as client:
        _ok(
            client.post(
                path + "/start", headers=headers, json={"confirm": True, "plan": routed.plan}
            )
        )
        tick = {"confirm": True, "expected_stage": 0}
        _ok(client.post(path + "/advance", headers=headers, json=tick))
        _worker(routed, "succeeded")
        queued = _ok(client.post(path + "/advance", headers=headers, json=tick))
        assert _worker(routed, "indeterminate")["id"] == queued["job_id"]
    with _client(routed) as client:
        stopped = _ok(client.post(path + "/advance", headers=headers, json=tick))
        assert stopped["state"] == "blocked" and stopped["error_code"] == "job_indeterminate"
        assert stopped["job_id"] == queued["job_id"]
        assert _ok(client.post(path + "/advance", headers=headers, json=tick)) == stopped
    assert routed.runner.run_once(routed.actor, "operator-no-replay") is None
    assert len(routed.requests) == 1 and effects(routed)["usage"] == 0
    with routed.env.db.transaction(routed.actor) as connection:
        [hold] = connection.execute("SELECT status FROM budget_reservations").fetchall()
    assert hold["status"] == "indeterminate"
