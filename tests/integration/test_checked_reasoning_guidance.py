"""Test the race between a stage check and a concurrent operator instruction."""

import json
from uuid import UUID, uuid4

import pytest

from potbot.controller.checked_reasoning import CheckedReasoning
from potbot.controller.routed_reasoning import RoutedReasoning
from potbot.domain.errors import RevisionConflictError
from tests.integration import test_hosted_gateway as mock
from tests.integration import test_routed_reasoning, test_ui_checked_reasoning
from tests.integration.test_operation_checks import TOKEN, effects
from tests.integration.test_routed_reasoning import (
    PROPOSALS,
    _critique,
    _fixture_packet,
    _worker,
)

pytestmark = pytest.mark.postgres
routed = test_routed_reasoning.routed


def test_guidance_racing_advance_is_checked_on_the_next_attempt(routed, monkeypatch):
    run_id = uuid4()
    path = f"/v1/ui/checked-reasoning/{run_id}"
    owner = test_ui_checked_reasoning._headers(routed)
    setup = {**owner, "X-Potbot-Setup-Token": TOKEN}
    guide = {
        "confirm": True,
        "expected_stage": 1,
        "constraints": ["Treat the estimated read latency as unverified."],
    }
    ok = test_ui_checked_reasoning._ok
    wire = mock.wire

    def reply():
        packet = _fixture_packet(routed.requests[-1])
        if len(routed.requests) == 1:
            assert "operator_constraints" not in packet["context"]
            return wire((json.dumps(PROPOSALS),))
        assert len(routed.requests) == 2
        assert packet["context"]["operator_constraints"] == guide["constraints"]
        messages = json.loads(routed.requests[-1].content)["messages"]
        assert "Consider context.operator_constraints" in messages[0]["content"]
        return wire((json.dumps(_critique(packet)),))

    monkeypatch.setattr(mock, "wire", reply)
    with test_ui_checked_reasoning._client(routed) as client:
        ok(client.post(path + "/start", headers=owner, json={"confirm": True, "plan": routed.plan}))
        assert client.post(path + "/guide", headers=owner, json=guide).status_code == 409

        def advance(stage):
            return client.post(
                path + "/advance", headers=setup, json={"confirm": True, "expected_stage": stage}
            )

        ok(advance(0))
        _worker(routed, "succeeded")
        ok(advance(0))
        _worker(routed, "succeeded")
        successor = ok(advance(0))
        assert successor["stage_index"] == 1 and successor["decision_id"] is None
        duplicate = {**guide, "constraints": guide["constraints"] * 2}
        assert client.post(path + "/guide", headers=owner, json=duplicate).status_code == 400
        foreign = {
            **owner,
            "Authorization": f"Bearer {test_ui_checked_reasoning._OTHER}",
            "X-Potbot-Expected-Principal": str(routed.reviewer.principal_id),
        }
        assert client.post(path + "/guide", headers=foreign, json=guide).status_code == 404

        start_check = CheckedReasoning.begin

        def guide_after_read(checked, *args, **kwargs):
            assert kwargs["role"] == "critic"
            assert "operator_constraints" not in kwargs["facts"]["context"]
            fresh = RoutedReasoning(routed.env.db)
            fresh.guide(routed.actor, run_id, 1, guide["constraints"])
            return start_check(checked, *args, **kwargs)

        with monkeypatch.context() as race:
            race.setattr(CheckedReasoning, "begin", guide_after_read)
            observed = ok(advance(1))
        assert observed["operator_constraints"] == guide["constraints"]
        assert observed["decision_id"] is None
        assert routed.runner.run_once(routed.actor, "no-unguided-check") is None

        assert ok(client.post(path + "/guide", headers=owner, json=guide)) == observed
        assert (
            client.post(
                path + "/guide",
                headers=owner,
                json={**guide, "constraints": ["Replace the guidance."]},
            ).status_code
            == 409
        )
        next_check = ok(advance(1))
        decision_id = UUID(next_check["decision_id"])
        with routed.env.db.transaction(routed.actor) as conn:
            row = conn.execute(
                "SELECT facts FROM model_route_compiled_prompts WHERE decision_id=%s",
                (decision_id,),
            ).fetchone()
        assert row["facts"]["context"]["operator_constraints"] == guide["constraints"]
        assert row["facts"]["context"]["stage_outputs"] == [
            successor["completed"][0]["output_sha256"]
        ]
        assert ok(client.post(path + "/guide", headers=owner, json=guide)) == next_check
        _worker(routed, "succeeded")
        ok(advance(1))
        _worker(routed, "succeeded")
        following = ok(advance(1))
        assert following["stage_index"] == 2 and "operator_constraints" not in following
        assert ok(client.post(path + "/guide", headers=owner, json=guide)) == following
        assert len(routed.requests) == effects(routed)["usage"] == 2


def test_postgres_refuses_late_guidance_even_for_the_owner(routed, monkeypatch):
    from psycopg.types.json import Jsonb

    wire = mock.wire
    monkeypatch.setattr(mock, "wire", lambda: wire((json.dumps(PROPOSALS),)))
    service = RoutedReasoning(routed.env.db)
    run_id = uuid4()
    service.start(routed.actor, run_id, routed.plan)
    test_routed_reasoning._queue_current_stage(routed, run_id, "hypothesis_generator")
    _worker(routed, "succeeded")
    assert service.advance(routed.actor, run_id, setup_token=TOKEN)["stage_index"] == 1
    service.advance(routed.actor, run_id, setup_token=TOKEN)
    with pytest.raises(RevisionConflictError), routed.env.db.transaction(routed.actor) as conn:
        conn.execute(
            "INSERT INTO routed_reasoning_guidance "
            "(workspace_id,principal_id,run_id,stage_index,constraints) VALUES (%s,%s,%s,1,%s)",
            (routed.actor.workspace_id, routed.actor.principal_id, run_id, Jsonb(["Too late."])),
        )
    assert "operator_constraints" not in service.inspect(routed.actor, run_id)
