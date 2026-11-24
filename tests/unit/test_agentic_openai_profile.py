"""Explicit hosted benchmark controls, compatible identities and uncertainty fencing."""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_model_bench import ModelTarget, RoleCase, RoleSuite, prompt_for_case
from potbot.evaluation.agentic_model_transport import BenchmarkTransport
from potbot.evaluation.agentic_openai_profile import PREFIX, profile_for_target
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.models.providers import (
    ProviderCompleted,
    ProviderRequest,
    ProviderSelection,
    ProviderTextDelta,
    ProviderUsage,
)
from potbot.models.providers.contracts import ProviderAPIError
from potbot.models.providers.protocols import request_body


def target():
    model = "gpt-6-luna"
    return ModelTarget("cheap", model, "hosted", ("triage",), PREFIX + model, provider="openai")


@pytest.mark.parametrize(
    "model,effort",
    [
        ("gpt-6-luna", "none"),
        ("gpt-5.6-luna", "none"),
        ("gpt-5-nano-2025-08-07", "minimal"),
        ("gpt-4o-mini-2024-07-18", None),
    ],
)
def test_only_explicit_revision_selects_the_versioned_native_controls(model, effort):
    value = replace(target(), model=model, revision=PREFIX + model)
    assert profile_for_target(value)["reasoning_effort"] == effort
    assert profile_for_target(replace(value, revision="legacy")) is None
    with pytest.raises(InvalidInputError):
        profile_for_target(replace(value, revision=PREFIX + "wrong"))


def test_legacy_provider_request_identity_omits_new_optional_control():
    request = ProviderRequest(ProviderSelection("openai", "gpt-6-luna"), "synthetic")
    assert "reasoning_effort" not in request.to_dict()
    assert "reasoning" not in request_body(request)[1]
    selected = replace(request, reasoning_effort="none")
    assert selected.fingerprint != request.fingerprint
    assert request_body(selected)[1]["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize(
    "provider,protocol", [("anthropic", "messages"), ("gemini", "gemini"), ("evolink", "responses")]
)
def test_reasoning_control_does_not_broaden_other_provider_protocols(provider, protocol):
    with pytest.raises(InvalidInputError):
        ProviderRequest(
            ProviderSelection(provider, "test-model", protocol),
            "synthetic",
            reasoning_effort="none",
        )


def test_chat_control_uses_exact_native_field_and_invalid_effort_is_refused():
    request = ProviderRequest(
        ProviderSelection("openai", "gpt-6-luna", "chat_completions"),
        "synthetic",
        reasoning_effort="none",
    )
    body = request_body(request)[1]
    assert body["reasoning_effort"] == "none" and "reasoning" not in body
    with pytest.raises(InvalidInputError):
        replace(request, reasoning_effort="unbounded")


def test_known_token_limit_preserves_actual_usage_without_replaying():
    calls = []

    class Credentials:
        def resolve(self, _ref):
            return "unused-synthetic-key"

    class Client:
        def __init__(self, _resolver, *, authorize):
            self.authorize = authorize

        def stream(self, request, _id):
            self.authorize(request)
            calls.append(request)
            yield ProviderTextDelta("partial known answer")
            yield ProviderCompleted(
                "length", ProviderUsage(100, 384, 484), model=request.selection.model
            )

        def close(self):
            pass

    model = target()
    case = RoleCase("case", "triage", "Return JSON", "json_subset", {"x": 1})
    policy = HostedPolicy(
        allow_prompt_disclosure=True, rules=(RateRule("openai", model.model, 100000, 500000),)
    )
    transport = BenchmarkTransport(policy=policy, credentials=Credentials(), client_factory=Client)
    result = transport.request(model, prompt_for_case(model.model, case, "generic"))
    assert len(calls) == 1 and calls[0].reasoning_effort == "none"
    assert result.output_tokens == 384 and result.billed_microunits == 202
    assert result.origin == "openai-cheap-v1-length"


def test_unresolved_native_api_failure_stops_further_paid_cells(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "openai_bench_cli", root / "scripts/benchmark_agentic_models.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    calls = []

    class Credentials:
        def __init__(self):
            pass

        def resolve(self, _ref):
            return "unused-synthetic-key"

    class Client:
        def __init__(self, _resolver, *, authorize):
            pass

        def stream(self, request, _id):
            calls.append(request)
            raise ProviderAPIError("synthetic-lost-reply", dispatched=True)
            yield  # ensure a streaming generator at the boundary

        def close(self):
            pass

    def factory(**kwargs):
        return BenchmarkTransport(**kwargs, client_factory=Client)

    monkeypatch.setattr(cli, "ServiceCredentials", Credentials)
    monkeypatch.setattr(cli, "BenchmarkTransport", factory)
    model = target()
    cases = RoleSuite(
        "small",
        tuple(
            RoleCase(str(i), "triage", "Synthetic " + str(i), "json_subset", {"x": 1})
            for i in range(3)
        ),
    )
    args = Namespace(
        repeat=1,
        shuffle_seed=42,
        execution_order="model_blocks_v1",
        confirm_prompt_disclosure=True,
        judge_derived=False,
        min_samples=12,
        store_text=True,
        allow_paid=True,
        max_paid_calls=3,
        max_cost_microunits=100000,
    )
    policy = HostedPolicy(
        allow_prompt_disclosure=True, rules=(RateRule("openai", model.model, 100000, 500000),)
    )
    report = cli._execute(args, cases, [model], ["generic"], policy)
    assert len(calls) == 1 and report["hosted_attempted_calls"] == 1
    assert report["execution"]["status"] == "stopped_transport_failure"
    assert report["execution"]["unattempted_primary_attempts"] == 2
    assert report["reserved_ceiling_microunits"] > 0
    assert report["results"][0]["billed_microunits"] is None
    assert report["transport_profiles"]["openai_cheap_v1"][model.id]["reasoning_effort"] == "none"
