"""Routing contracts cannot skip rollout stages or change a selected executor."""

from __future__ import annotations

import copy
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.model_routing import (
    RoutingStage,
    bind_route_request,
    route_target,
    routing_choice,
    routing_transition,
)


@pytest.mark.parametrize(
    "before,after",
    [
        ("draft", "shadow"),
        ("shadow", "canary"),
        ("canary", "promoted"),
        ("promoted", "rolled_back"),
        ("canary", "blocked"),
        ("rolled_back", "shadow"),
        ("blocked", "shadow"),
        ("draft", "disabled"),
    ],
)
def test_declared_stage_order_is_validated_without_qualification(before, after):
    assert routing_transition(before, after) == RoutingStage(after)


@pytest.mark.parametrize(
    "before,after",
    [
        ("draft", "promoted"),
        ("shadow", "promoted"),
        ("disabled", "canary"),
        ("rolled_back", "promoted"),
        ("canary", "canary"),
        ("blocked", "promoted"),
    ],
)
def test_promotion_cannot_skip_shadow_or_canary(before, after):
    with pytest.raises(StateMismatchError):
        routing_transition(before, after)


@pytest.mark.parametrize("stage", ["draft", "blocked", "disabled"])
def test_inactive_routes_cannot_select_a_target(stage):
    with pytest.raises(StateMismatchError):
        routing_choice(stage, uuid4(), 1, "request-key", 100)


def test_shadow_and_rollback_use_comparator_and_promotion_selects_candidate():
    identity = uuid4()
    assert routing_choice("shadow", identity, 1, "key", 100) == "baseline"
    assert routing_choice("rolled_back", identity, 2, "key", 100) == "baseline"
    assert routing_choice("promoted", identity, 3, "key", 100) == "candidate"


def test_canary_allocation_is_stable_and_has_both_outcomes():
    identity = UUID("738adf1e-fdae-471a-99de-bd672c72acc4")
    first = [routing_choice("canary", identity, 2, str(i), 1000) for i in range(300)]
    assert first == [routing_choice("canary", identity, 2, str(i), 1000) for i in range(300)]
    assert {"baseline", "candidate"} == set(first)
    assert first != [routing_choice("canary", identity, 3, str(i), 1000) for i in range(300)]


@pytest.mark.parametrize("epoch,bps", [(True, 100), (0, 100), (1, True), (1, 0), (1, 1001)])
def test_bools_and_unbounded_allocation_are_refused(epoch, bps):
    with pytest.raises(InvalidInputError):
        routing_choice("canary", uuid4(), epoch, "key", bps)


def hosted():
    projection = {
        "job_type": "model.generate",
        "selected_models": [{"provider": "openai", "model": "fixture-model", "role": "coding"}],
        "input": {},
    }
    target = {"operation_id": "a" * 64, "runtime_identity_sha256": "b" * 64, "executor": projection}
    request = {
        **copy.deepcopy(projection),
        "episode_id": str(uuid4()),
        "input": {"prompt": "Synthetic fixture only", "max_output_tokens": 64},
    }
    return target, request


def test_hosted_projection_pins_the_model_without_rewriting_ordinary_inputs():
    target, request = hosted()
    bound = bind_route_request(target, request)
    assert bound.to_dict() == request
    target["executor"]["selected_models"][0]["model"] = "different"
    with pytest.raises(StateMismatchError):
        bind_route_request(target, request)


def test_target_is_detached_and_additional_authority_is_not_a_target_field():
    target, _ = hosted()
    detached = route_target(target)
    target["executor"]["selected_models"][0]["model"] = "changed"
    assert detached["executor"]["selected_models"][0]["model"] == "fixture-model"
    detached["qualified"] = True
    with pytest.raises(InvalidInputError):
        route_target(detached)


def native():
    target = {
        "operation_id": "a" * 64,
        "runtime_identity_sha256": "b" * 64,
        "executor": {
            "job_type": "cog.execute",
            "selected_models": [],
            "input": {
                "operation_ref": "syn://thing/local-model",
                "profile_sha256": "c" * 64,
                "type_ref": "syn://type/model-request",
                "value": {"deployment_id": str(uuid4()), "model_revision": "d" * 40},
            },
        },
    }
    request = {**copy.deepcopy(target["executor"]), "episode_id": str(uuid4())}
    request["input"].update(agent_id=str(uuid4()), repository_state_id=str(uuid4()))
    request["input"]["value"].update(prompt="Synthetic private input", max_output_tokens=32)
    return target, request


def test_native_projection_pins_profile_and_deployment_without_losing_live_input():
    target, request = native()
    assert bind_route_request(target, request).to_dict() == request
    request["input"]["value"]["deployment_id"] = str(uuid4())
    with pytest.raises(StateMismatchError):
        bind_route_request(target, request)


@pytest.mark.parametrize("field", ["profile_sha256", "type_ref", "operation_ref"])
def test_native_executor_changes_are_refused(field):
    target, request = native()
    request["input"][field] = "f" * 64 if field == "profile_sha256" else "syn://type/different"
    with pytest.raises(StateMismatchError):
        bind_route_request(target, request)
