"""Synthetic typed native results and role contracts are not model qualification."""

from uuid import uuid4

import pytest

from potbot.controller.model_reasoning_results import deployment_from_content
from potbot.controller.reasoning_result_contracts import validate_reasoning_output
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.models.native_results import (
    RESULT,
    parse_native_result,
    strict_object,
    validate_native_request,
)
from potbot.models.registry import DeploymentSpec


def deployment():
    return DeploymentSpec(
        "fixture-model",
        "a" * 64,
        "explicit-native-fixture",
        "1",
        "f16",
        "b" * 40,
        "c" * 64,
        "potbot.native-model-result/1",
        "none",
        4096,
        128,
        (("investigator", "model-aware-reasoning-json-object-v1"),),
        ("reasoning",),
        (RESULT,),
        cancellation="process_termination",
    )


def request():
    return {
        "model": "fixture-model",
        "deployment_sha256": deployment().fingerprint,
        "system": "system",
        "prompt": "private task",
        "max_output_tokens": 16,
    }


def native_output(job_id, value=None):
    selected = value or request()
    spec = deployment()
    return {
        "type_ref": RESULT,
        "value": {
            "schema": "potbot.native-model-result/1",
            "invocation_id": str(job_id),
            "request_sha256": content_hash(selected),
            "deployment_sha256": spec.fingerprint,
            "model": spec.canonical_model,
            "provenance": {
                "weights_sha256": spec.weights_digest,
                "backend": spec.backend,
                "backend_version": spec.backend_version,
                "tokenizer_revision": spec.tokenizer_revision,
                "chat_template_sha256": spec.chat_template_digest,
            },
            "text": '{"queries":["inspect original receipt"]}',
            "finish_reason": "stop",
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        },
    }


def parse(payload, job):
    return parse_native_result(
        canonical_json(payload).encode(), request=request(), job_id=job, deployment=deployment()
    )


def test_native_result_binds_original_request_and_deployment_without_qualification():
    job = uuid4()
    value = parse(native_output(job), job)
    assert value["usage"]["total_tokens"] == 15
    assert validate_reasoning_output("investigator", value["text"], {}) == {
        "queries": ["inspect original receipt"]
    }
    assert deployment_from_content(deployment().to_dict()) == deployment()


@pytest.mark.parametrize(
    "field,value",
    [
        ("invocation_id", str(uuid4())),
        ("request_sha256", "f" * 64),
        ("deployment_sha256", "d" * 64),
        ("model", "another"),
        ("schema", "unknown"),
    ],
)
def test_native_replies_cannot_move_between_jobs_or_deployments(field, value):
    job = uuid4()
    payload = native_output(job)
    payload["value"][field] = value
    with pytest.raises(StateMismatchError):
        parse(payload, job)


@pytest.mark.parametrize(
    "field",
    ["weights_sha256", "backend", "backend_version", "tokenizer_revision", "chat_template_sha256"],
)
def test_each_native_provenance_field_is_bound(field):
    job = uuid4()
    payload = native_output(job)
    payload["value"]["provenance"][field] = "changed"
    with pytest.raises(StateMismatchError):
        parse(payload, job)


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 12, "output_tokens": True, "total_tokens": 13},
        {"input_tokens": 12, "output_tokens": 3, "total_tokens": 16},
        {"input_tokens": 12, "output_tokens": 17, "total_tokens": 29},
        {"input_tokens": 4095, "output_tokens": 3, "total_tokens": 4098},
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    ],
)
def test_native_usage_is_exact_and_within_request_and_context_bounds(usage):
    job = uuid4()
    payload = native_output(job)
    payload["value"]["usage"] = usage
    with pytest.raises(InvalidInputError):
        parse(payload, job)


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "http://untrusted"},
        {"max_output_tokens": True},
        {"deployment_sha256": "d" * 64},
        {"prompt": ""},
    ],
)
def test_request_does_not_admit_execution_options_or_another_deployment(change):
    with pytest.raises((InvalidInputError, StateMismatchError)):
        validate_native_request(request() | change, deployment())


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":1e9999}',
        "```json\n{}\n```",
        "[]",
        '{"a":' + "[" * 30 + "0" + "]" * 30 + "}",
    ],
)
def test_no_repair_duplicate_keys_nonfinite_or_deep_model_objects(raw):
    with pytest.raises(InvalidInputError):
        strict_object(raw)


def critique(node="one"):
    return {
        "evaluations": [
            {
                "node_id": node,
                "relevance": 1,
                "evidence_alignment": 1,
                "falsification_risk": 1,
                "uncertainty": 1,
                "recommendation": "keep",
                "rationale": "Synthetic advisory judgment",
                "issues": [],
                "tests": [],
            }
        ]
    }


def test_critique_is_bound_to_every_frozen_branch_and_remains_advisory():
    parsed = validate_reasoning_output(
        "critic", canonical_json(critique()), {"branches": [{"node_id": "one"}]}
    )
    assert parsed["one"]["recommendation"] == "keep"
    with pytest.raises(InvalidInputError):
        validate_reasoning_output(
            "critic", canonical_json(critique("invented")), {"branches": [{"node_id": "one"}]}
        )
    with pytest.raises(InvalidInputError):
        validate_reasoning_output(
            "critic", canonical_json(critique()), {"branches": [{"node_id": "one"}] * 2}
        )


def test_steering_cannot_invent_reopened_branches_or_hide_work_in_no_change():
    value = {
        "decision": "reopen_branch",
        "explanation": "Reconsider the supplied evidence",
        "branch_id": "one",
        "hypothesis": None,
    }
    context = {"current_hypotheses": [{"id": "one"}]}
    assert validate_reasoning_output("steering_evaluator", canonical_json(value), context) == value
    for changed in [
        value | {"branch_id": "invented"},
        value | {"decision": "no_change"},
        value | {"hypothesis": "extra"},
    ]:
        with pytest.raises((InvalidInputError, StateMismatchError)):
            validate_reasoning_output("steering_evaluator", canonical_json(changed), context)


def test_hypotheses_and_queries_remain_bounded_data_without_execution_authority():
    value = {
        "hypotheses": [
            {"hypothesis": "A synthetic possibility", "assumptions": [], "questions": []}
        ]
    }
    assert len(validate_reasoning_output("hypothesis_generator", canonical_json(value), {})) == 1
    with pytest.raises(InvalidInputError):
        validate_reasoning_output(
            "hypothesis_generator", canonical_json(value | {"execute": "shell"}), {}
        )
    for queries in [["duplicate"] * 2, ["a"] * 4, [{}], [" "]]:
        with pytest.raises(InvalidInputError):
            validate_reasoning_output("investigator", canonical_json({"queries": queries}), {})
    assert (
        validate_reasoning_output("synthesizer", "An explicitly uncertain conclusion", {}) is None
    )


@pytest.mark.parametrize("raw", [b"\xff", '{"a":1}'.encode("utf-16"), b'{"a":"\\ud800"}'])
def test_result_boundary_uses_existing_strict_utf8_json_parser(raw):
    with pytest.raises(InvalidInputError):
        strict_object(raw)
