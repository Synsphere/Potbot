"""Constrain decoding without weakening validation or rewriting historical prompts."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.models.prompt_compiler import compile_reasoning_prompt
from potbot.models.reasoning_schema import (
    ROLE_SCHEMA_PROMPT_VERSION,
    ROLE_SCHEMA_VARIANT,
    ReasoningOutputSpec,
)
from tests.unit.test_model_route_bindings import base, facts, recipe


@pytest.mark.parametrize(
    "variant, contract, frozen_sha256",
    [
        ("generic", None, "6abba3666072cd97bf0cbc7ef0c17e73ae3975d83f55f838c26d92949023c507"),
        (
            "generic",
            "json_object_v1",
            "0e7302d9a32f079ffb3af4190e4207c3bc39ed96700c5e931908a15ba9e7f1f3",
        ),
        ("family", None, "bc3433b10a15c035566d75d13ef11933590c14f8c5ca03ecbe4e26c64256ed3a"),
        (
            "family",
            "json_object_v1",
            "fb5f25ef67cd990fbd77225e890d60215eb225cc7dbefc2991c71f38e371d33a",
        ),
        ("model_role", None, "7aa53b1de45f1478a1722dfa8f485a6898c33cc45cf32597e4cd47e476829b2b"),
        (
            "model_role",
            "json_object_v1",
            "ab7ce224ca7da8404c2b89a1e165aa33152ed1a964bed2fff313e8aca816253c",
        ),
    ],
)
def test_older_prompt_identities_remain_byte_compatible(variant, contract, frozen_sha256):
    prompted = compile_reasoning_prompt(
        model="qwen3.5:2b",
        role="critic",
        task="Review two branches",
        context={"branches": [{"node_id": "a"}, {"node_id": "b"}]},
        variant=variant,
        output_contract=contract,
    )
    assert prompted.identity == frozen_sha256


def test_hypothesis_schema_bounds_arrays_and_question_type():
    spec = ReasoningOutputSpec("hypothesis_generator")
    schema = spec.schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    answer = {
        "hypotheses": [
            {
                "hypothesis": "Use the tested route",
                "assumptions": ["The times are current"],
                "questions": [{"channel": "external_libraries", "query": "Confirm the timetable"}],
            }
        ]
    }
    assert validator.is_valid(answer)
    for key, invalid in (
        ("assumptions", ["Too many"] * 4),
        ("questions", [{"channel": "shell", "query": "Impossible channel"}]),
        ("action", "Do work"),
    ):
        changed = deepcopy(answer)
        changed["hypotheses"][0][key] = invalid
        assert not validator.is_valid(changed)


def test_critique_schema_binds_ids_count_and_score_enum():
    context = {"branches": [{"node_id": "a"}, {"node_id": "b"}]}
    spec = ReasoningOutputSpec.from_context("critic", context)
    schema = spec.schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    def evaluation(branch):
        return {
            "node_id": branch,
            "relevance": 4,
            "evidence_alignment": 1,
            "falsification_risk": 0,
            "uncertainty": 2,
            "recommendation": "keep",
            "rationale": "Evidence supports the branch.",
            "issues": [],
            "tests": [],
        }

    assert validator.is_valid({"evaluations": [evaluation("a"), evaluation("b")]})
    assert not validator.is_valid({"evaluations": [evaluation("a")]})
    assert not validator.is_valid({"evaluations": [evaluation("a"), evaluation("different")]})
    assert not validator.is_valid(
        {"evaluations": [evaluation("a"), evaluation("b") | {"relevance": 5}]}
    )
    assert spec.fingerprint != ReasoningOutputSpec("critic", ("a", "c")).fingerprint
    assert spec.fingerprint == ReasoningOutputSpec("critic", ("b", "a")).fingerprint
    with pytest.raises(InvalidInputError):
        ReasoningOutputSpec("critic", ("a", "a"))


def test_structured_variant_requires_json_role_and_supported_checked_executor():
    from potbot.controller.model_route_bindings import compile_route_request

    selected = recipe() | {"role": "hypothesis_generator", "variant": ROLE_SCHEMA_VARIANT}
    source = base()
    compiled, _ = compile_route_request(selected, facts(), source, fill=True)
    assert "bounded role schema" in compiled["input"]["system"]
    prompt = compile_reasoning_prompt(
        model=selected["model"],
        role=selected["role"],
        task="A task",
        variant=ROLE_SCHEMA_VARIANT,
        output_contract="json_object_v1",
    )
    assert prompt.prompt_version == ROLE_SCHEMA_PROMPT_VERSION
    source["selected_models"][0]["provider"] = "gemini"
    with pytest.raises(ScopeDeniedError):
        compile_route_request(selected, facts(), source, fill=True)
    with pytest.raises(InvalidInputError):
        compile_route_request(selected | {"role": "investigator"}, facts(), base(), fill=True)
    with pytest.raises(InvalidInputError):
        compile_route_request(selected | {"output_contract": None}, facts(), base(), fill=True)
