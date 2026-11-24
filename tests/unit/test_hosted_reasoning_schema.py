"""Transport and accounting regressions for trusted hosted reasoning schemas."""

from dataclasses import replace

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.models.providers.contracts import ProviderRequest, ProviderSelection
from potbot.models.providers.protocols import request_body
from potbot.models.reasoning_schema import ReasoningOutputSpec


@pytest.mark.parametrize(
    ("protocol", "path"),
    [("responses", "/v1/responses"), ("chat_completions", "/v1/chat/completions")],
)
def test_strict_reasoning_schema_uses_native_openai_wire_fields(protocol, path):
    spec = ReasoningOutputSpec("critic", ("branch-beta", "branch-alpha"))
    selection = ProviderSelection("openai", "test-model", protocol)
    legacy = ProviderRequest(selection, "payload")
    request = ProviderRequest(selection, "payload", response_format="json_schema", output_spec=spec)
    route, body = request_body(request)
    contract = {"name": spec.name, "schema": spec.schema(), "strict": True}

    assert route == path
    if protocol == "responses":
        assert body["text"]["format"] == {"type": "json_schema", **contract}
        assert "response_format" not in body
    else:
        assert body["response_format"] == {"type": "json_schema", "json_schema": contract}
        assert "text" not in body
    assert request.to_dict() == {
        **legacy.to_dict(),
        "response_format": "json_schema",
        "response_schema": contract,
    }
    assert request.fingerprint != legacy.fingerprint
    different = replace(request, output_spec=ReasoningOutputSpec("critic", ("other-branch",)))
    assert request.fingerprint != different.fingerprint
    assert request_body(legacy)[1].keys() == body.keys() - {"text", "response_format"}
    assert "response_schema" not in legacy.to_dict()


def test_arbitrary_or_unsupported_hosted_schemas_are_refused():
    openai = ProviderSelection("openai", "test-model")
    with pytest.raises(InvalidInputError):
        ProviderRequest(openai, "payload", response_format="json_schema")
    spec = ReasoningOutputSpec("hypothesis_generator")
    with pytest.raises(InvalidInputError):
        ProviderRequest(openai, "payload", output_spec=spec)
    for provider in ("anthropic", "gemini", "xai", "evolink"):
        with pytest.raises(InvalidInputError):
            ProviderRequest(
                ProviderSelection(provider, "test-model"),
                "payload",
                response_format="json_schema",
                output_spec=spec,
            )
    policy = HostedPolicy(True, (RateRule("openai", "test-model", 0, 0),))
    selected = {"provider": "openai", "model": "test-model", "role": "reasoning"}
    with pytest.raises(InvalidInputError):
        policy.request(selected, {"prompt": "payload", "response_schema": spec.schema()})


def test_schema_bytes_increase_prompt_and_cost_admission_bounds():
    policy = HostedPolicy(True, (RateRule("openai", "test-model", 1_000_000, 0),))
    selection = {"provider": "openai", "model": "test-model", "role": "reasoning"}
    spec = ReasoningOutputSpec("critic", ("schema-only-identifier",))
    value = {"prompt": "task", "system": "rules"}
    legacy, original_allowance, _ = policy.request(selection, value)
    request, allowance, _ = policy.request(selection, value, output_spec=spec)

    assert legacy.disclosure_bytes == b"taskrules"
    assert (
        request.disclosure_bytes
        == legacy.disclosure_bytes + canonical_json(request.response_schema).encode()
    )
    assert b"schema-only-identifier" in request.disclosure_bytes
    assert allowance == original_allowance + len(canonical_json(request.response_schema).encode())
    assert allowance > original_allowance

    for strict in (
        replace(policy, max_prompt_bytes=len(legacy.disclosure_bytes)),
        replace(policy, max_cost_microunits=original_allowance),
    ):
        strict.request(selection, value)
        with pytest.raises(ScopeDeniedError):
            strict.request(selection, value, output_spec=spec)
    with pytest.raises(InvalidInputError):
        policy.request(
            selection, {**value, "input_token_allowance": original_allowance}, output_spec=spec
        )
