"""Fact-gated execution preserves the original hosted job and known usage."""

import json
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from potbot.controller.budgets import BudgetLedger
from potbot.controller.model_reasoning_results import ReasoningFactCheckError
from potbot.controller.routed_reasoning import RoutedReasoning
from potbot.domain.errors import RevisionConflictError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.models.reasoning_schema import ROLE_SCHEMA_VARIANT, ReasoningOutputSpec
from tests.integration import test_hosted_gateway as provider
from tests.integration.test_model_route_bindings import accept
from tests.integration.test_routed_reasoning import (
    PROPOSALS,
    ROLES,
    _advance,
    _critique,
    _fixture_packet,
    _queue_current_stage,
    _worker,
    routed,
)

__all__ = ["routed"]
pytestmark = pytest.mark.postgres

USB = {
    "kind": "minimum_cost",
    "minimum_capacity": 32,
    "records": [
        {"id": "blue", "capacity": 16, "cost": 5},
        {"id": "green", "capacity": 32, "cost": 7},
        {"id": "red", "capacity": 64, "cost": 9},
    ],
}
COUNTS = {
    "kind": "counts",
    "labels": ["ready", "held", "blocked"],
    "values": [
        "ready",
        "held",
        "ready",
        "blocked",
        "ready",
    ],
}


def _plan_for(r, answer_check):
    source = r.plan["stages"][-1]
    baseline = r.routing.inspect(r.actor, UUID(source["policy_id"]))["manifest"]["baseline"]
    r.policy_id = uuid4()
    recipe = {
        "schema": "potbot-route-prompt-recipe/v1",
        "model": source["request"]["selected_models"][0]["model"],
        "role": "synthesizer",
        "variant": ROLE_SCHEMA_VARIANT,
        "output_contract": "json_object_v1",
    }
    r.routing.create(
        r.actor,
        r.policy_id,
        reviewer_id=r.reviewer.principal_id,
        role="reasoning",
        atomic_kind="synthesizer",
        baseline=baseline,
        candidate=baseline | {"runtime_identity_sha256": "c" * 64},
        prompt_contract_sha256=content_hash(recipe),
        evaluation_plan_sha256="f" * 64,
    )
    r.routing.configure_prompt(r.actor, r.policy_id, recipe)
    accept(r)
    r.routing.transition(r.actor, r.policy_id, expected_epoch=1, stage="shadow")
    return r.plan | {
        "answer_check": answer_check,
        "stages": [*r.plan["stages"][:-1], source | {"policy_id": str(r.policy_id)}],
    }


@pytest.mark.parametrize(
    "answer_check, model_output, correct",
    [
        (USB, {"record_id": "blue", "capacity": 32, "cost": 5}, False),
        (USB, {"record_id": "green", "capacity": 32, "cost": 7}, True),
        (COUNTS, {"counts": {"ready": 3, "held": 1, "blocked": 1}}, True),
        (COUNTS, {"counts": {"ready": 2, "held": 2, "blocked": 1}}, False),
    ],
    ids=["invented-capacity", "eligible-cheapest", "exact-counts", "wrong-distribution"],
)
def test_verified_synthesis_uses_frozen_facts_and_preserves_settlement(
    routed, monkeypatch, answer_check, model_output, correct
):
    service = RoutedReasoning(routed.env.db)
    run_id = uuid4()
    plan = _plan_for(routed, answer_check)
    start = service.start(routed.actor, run_id, plan)
    assert start["state"] == "active"
    with pytest.raises(StateMismatchError):
        service.start(
            routed.actor, run_id, plan | {"answer_check": COUNTS if answer_check == USB else USB}
        )
    wire = provider.wire
    observed = []
    text = json.dumps(model_output, ensure_ascii=False)

    def response():
        packet = _fixture_packet(routed.requests[-1])
        if len(routed.requests) == 1:
            assert "answer_check" not in packet["context"]
            output = json.dumps(PROPOSALS)
        elif len(routed.requests) == 2:
            assert "answer_check" not in packet["context"]
            output = json.dumps(_critique(packet))
        else:
            assert packet["context"]["answer_check"] == answer_check
            sent = json.loads(routed.requests[-1].content)["response_format"]["json_schema"]
            fixed = ReasoningOutputSpec.from_context("synthesizer", packet["context"])
            assert sent == {"strict": True, "name": fixed.name, "schema": fixed.schema()}
            assert Draft202012Validator(sent["schema"]).is_valid(model_output)
            observed.append(fixed.fingerprint)
            output = text
        return wire((output,))

    monkeypatch.setattr(provider, "wire", response)
    for role in ROLES[:2]:
        _queue_current_stage(routed, run_id, role)
        _worker(routed, "succeeded")
        assert _advance(routed, run_id)["state"] == "active"

    final = _queue_current_stage(routed, run_id, ROLES[2])
    final_check = routed.jobs.inspect(routed.actor, UUID(final["check_job_id"]))
    assert _worker(routed, "succeeded")["id"] == final["job_id"]
    assert (
        final_check["result"]["operation_check"]["bindings"]["modelOutputSchemaSha256"]
        == observed[0]
    )
    receipt = {"decision_id": final["decision_id"], "output_sha256": content_hash(text)}
    if not correct:
        # A successful provider job and honest digest are insufficient to write
        # a completed, fact-checked run directly in PostgreSQL.
        with pytest.raises(RevisionConflictError):
            with routed.env.db.transaction(routed.actor) as conn:
                conn.execute(
                    "UPDATE routed_reasoning_runs SET results=results||%s,stage_index=3,state='completed' "
                    "WHERE workspace_id=%s AND id=%s",
                    (Jsonb([receipt]), routed.actor.workspace_id, run_id),
                )
        with pytest.raises(ReasoningFactCheckError):
            routed.routing.reasoning_result(routed.actor, UUID(final["decision_id"]))
    after = _advance(routed, run_id)
    if correct:
        assert after["state"] == "completed" and after["completed"][-1] == receipt
        result = RoutedReasoning(routed.env.db).result(routed.actor, run_id)
        assert result["parsed"] == model_output
        assert (
            result["job_id"] == final["job_id"]
            and result["output_sha256"] == receipt["output_sha256"]
        )
        assert result["semantic_correctness"] == "verified_against_frozen_records"
    else:
        assert after["state"] == "blocked" and after["error_code"] == "answer_check_failed", after
        assert after["stage_index"] == 2 and after["job_id"] == final["job_id"]
        with pytest.raises(StateMismatchError):
            service.result(routed.actor, run_id)

    assert _advance(routed, run_id) == after
    assert routed.runner.run_once(routed.actor, "no-factual-answer-replay") is None
    ledger = BudgetLedger(routed.env.db).inspect(routed.actor, routed.task_id)
    assert len(routed.requests) == ledger["used"]["calls"] == 3
    assert ledger["used"]["tokens"] == 45 and ledger["held"] == {}


def test_answer_check_and_structured_synthesis_must_be_selected_together(routed):
    service = RoutedReasoning(routed.env.db)
    with pytest.raises(StateMismatchError):
        service.start(routed.actor, uuid4(), routed.plan | {"answer_check": USB})
    plan = _plan_for(routed, USB)
    with pytest.raises(StateMismatchError):
        service.start(routed.actor, uuid4(), {k: v for k, v in plan.items() if k != "answer_check"})
