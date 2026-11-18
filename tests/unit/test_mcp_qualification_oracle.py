"""Independent declarative result checks never confer native authority."""

from __future__ import annotations

import copy
import json

import pytest

from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.capability_contracts import ToolObservation
from potbot.tools.mcp_protocol import MAX_WIRE_BYTES, McpCallResult
from potbot.tools.mcp_qualification_oracle import (
    MAX_ASSERTIONS,
    MAX_EXPECTED_BYTES,
    ORACLE_SCHEMA,
    RESULT_SCHEMA,
    evaluate_oracle,
    validate_oracle,
)

INVOCATION = "a" * 64
OUTPUT_TYPE = "synny://type/qualification-result"


def oracle(*assertions):
    return {"schema": ORACLE_SCHEMA, "assertions": list(assertions)}


def assertion(pointer="/structuredContent/answer", value=42, op="equals"):
    return {"op": op, "pointer": pointer, "value": value}


def output(structured=None, content=None, *, failed=False):
    result = McpCallResult.from_wire(
        "fixture",
        {
            "structuredContent": {"answer": 42} if structured is None else structured,
            "content": [] if content is None else content,
            "isError": failed,
        },
    )
    observation = ToolObservation(
        invocation_hash=INVOCATION,
        output_type_ref=OUTPUT_TYPE,
        status="failed" if failed else "succeeded",
        output_hash=None if failed else result.result_hash,
        artifact_sha256=None,
        error_code="mcp_tool_reported_error" if failed else None,
    )
    return {
        "observation": observation.to_dict(),
        "content": list(result.content),
        "structured_content": result.structured_content,
    }


def evaluate(spec=None, document=None, **kwargs):
    return evaluate_oracle(
        oracle(assertion()) if spec is None else spec,
        canonical_json(output() if document is None else document).encode(),
        invocation_sha256=kwargs.get("invocation_sha256", INVOCATION),
        output_type_ref=kwargs.get("output_type_ref", OUTPUT_TYPE),
    )


def test_exact_structured_and_text_case_has_safe_bound_digest_report():
    spec = oracle(
        assertion(),
        assertion("/content/0/text", "verified literal", "contains"),
        assertion("/structuredContent/items", 2, "length_equals"),
    )
    document = output(
        {"answer": 42, "items": ["first", "second"]},
        [{"type": "text", "text": "A verified literal from fixture data."}],
    )
    result = evaluate(spec, document)
    assert result == {
        "schema": RESULT_SCHEMA,
        "oracleSha256": content_hash(spec),
        "outputSha256": document["observation"]["output_hash"],
        "passed": True,
        "checks": [{"index": index, "passed": True, "reasonCode": None} for index in range(3)],
    }
    assert "verified literal" not in canonical_json(result)
    assert "native" not in result and "qualified" not in result


@pytest.mark.parametrize(
    "selected,op,value,reason",
    [
        ("/structuredContent/absent", "equals", None, "pointer_missing"),
        ("/structuredContent/answer", "equals", 41, "value_mismatch"),
        ("/structuredContent/answer", "contains", "42", "value_type_mismatch"),
        ("/structuredContent/answer", "length_equals", 2, "value_type_mismatch"),
        ("/content/0/text", "contains", "missing", "text_not_found"),
        ("/content", "length_equals", 2, "length_mismatch"),
    ],
)
def test_declared_mismatch_is_negative_evidence_without_value_disclosure(
    selected, op, value, reason
):
    result = evaluate(
        oracle(assertion(selected, value, op)),
        output(content=[{"type": "text", "text": "private returned value"}]),
    )
    assert result["passed"] is False
    assert result["checks"] == [{"index": 0, "passed": False, "reasonCode": reason}]
    assert "private returned value" not in canonical_json(result)


@pytest.mark.parametrize("actual,expected", [(True, 1), (False, 0), (1, 1.0), ([True], [1])])
def test_equality_preserves_exact_json_types(actual, expected):
    result = evaluate(oracle(assertion(value=expected)), output({"answer": actual}))
    assert result["passed"] is False


def test_null_is_distinct_from_missing_and_root_pointer_is_supported():
    assert evaluate(oracle(assertion(value=None)), output({"answer": None}))["passed"]
    view = {"content": [], "structuredContent": {"answer": 42}, "isError": False}
    assert evaluate(oracle(assertion("", view)))["passed"]


def test_pointer_escaping_empty_keys_and_unicode_follow_rfc6901():
    document = output({"a/b": {"~key": {"": "café"}}, "~1": "literal"})
    spec = oracle(
        assertion("/structuredContent/a~1b/~0key/", "café"),
        assertion("/structuredContent/~01", "literal"),
    )
    assert evaluate(spec, document)["passed"]
    assert evaluate(oracle(assertion("/structuredContent/01", 7)), output({"01": 7}))["passed"]


@pytest.mark.parametrize(
    "index",
    ["-", "-1", "01", "+1", "1.0", "١", "9" * 900, "2"],
    ids=[
        "append",
        "negative",
        "leading-zero",
        "plus",
        "decimal",
        "non-ascii",
        "oversized",
        "absent",
    ],
)
def test_array_selection_requires_existing_canonical_decimal_index(index):
    result = evaluate(
        oracle(assertion(f"/structuredContent/items/{index}", None)),
        output({"items": [None, "last"]}),
    )
    assert result["checks"][0]["reasonCode"] == "pointer_missing"


def test_array_selection_and_unicode_string_length():
    assert evaluate(
        oracle(assertion("/structuredContent/items/1", "last")), output({"items": [0, "last"]})
    )["passed"]
    assert evaluate(
        oracle(assertion("/structuredContent/answer", 4, "length_equals")),
        output({"answer": "café"}),
    )["passed"]


@pytest.mark.parametrize(
    "pointer",
    [
        "relative",
        "#fragment",
        "/bad~",
        "/bad~2",
        "/bad\nkey",
        "/bad\x7f",
        "/" + "a" * 1024,
        "/" + "é" * 512,
        "/" + "/".join(["a"] * 17),
        "/\ud800",
        None,
    ],
    ids=[
        "relative",
        "fragment",
        "trailing-escape",
        "invalid-escape",
        "newline",
        "control-character",
        "ascii-byte-limit",
        "unicode-byte-limit",
        "depth-limit",
        "lone-surrogate",
        "null",
    ],
)
def test_invalid_or_unbounded_pointers_cannot_become_an_oracle(pointer):
    with pytest.raises(InvalidInputError):
        validate_oracle(oracle(assertion(pointer)))


@pytest.mark.parametrize(
    "spec",
    [
        None,
        {},
        {"schema": "other", "assertions": []},
        oracle(),
        oracle(*[assertion()] * (MAX_ASSERTIONS + 1)),
        {**oracle(assertion()), "execute": "anything"},
        oracle({**assertion(), "regex": ".*"}),
        oracle({"op": "eval", "pointer": "", "value": "1+1"}),
        oracle({"op": [], "pointer": "", "value": None}),
        oracle(assertion(value="", op="contains")),
        oracle(assertion(value=42, op="contains")),
        oracle(assertion(value=True, op="length_equals")),
        oracle(assertion(value=-1, op="length_equals")),
        oracle(assertion(value=2**53, op="length_equals")),
        oracle(assertion(value=float("inf"))),
        oracle(assertion(value="x" * MAX_EXPECTED_BYTES)),
        oracle(*[assertion(value="x" * 10_000)] * 7),
    ],
    ids=[
        "null",
        "empty-object",
        "wrong-schema",
        "empty-assertions",
        "assertion-count-limit",
        "extra-oracle-field",
        "extra-assertion-field",
        "executable-operation",
        "invalid-operation-type",
        "empty-substring",
        "non-text-substring",
        "boolean-length",
        "negative-length",
        "unsafe-integer-length",
        "nonfinite-value",
        "expected-byte-limit",
        "oracle-byte-limit",
    ],
)
def test_invalid_assertions_bounds_and_nonfinite_expected_data_are_rejected(spec):
    with pytest.raises(InvalidInputError):
        validate_oracle(spec)


def test_validated_oracle_is_detached_from_operator_input():
    source = oracle(assertion(value={"nested": [1, 2]}))
    selected = validate_oracle(source)
    source["assertions"][0]["value"]["nested"].clear()
    assert selected["assertions"][0]["value"] == {"nested": [1, 2]}


@pytest.mark.parametrize(
    "mutation",
    [
        "invocation",
        "type",
        "hash",
        "body",
        "artifact",
        "failed",
        "indeterminate",
        "claimedVerification",
        "extraEnvelope",
        "missingContent",
        "badContent",
        "badStructured",
    ],
)
def test_mismatched_or_incomplete_gateway_evidence_never_reaches_a_pass(mutation):
    document = output()
    if mutation == "invocation":
        document["observation"]["invocation_hash"] = "b" * 64
    elif mutation == "type":
        document["observation"]["output_type_ref"] = "synny://type/other"
    elif mutation == "hash":
        document["observation"]["output_hash"] = "b" * 64
    elif mutation == "body":
        document["structured_content"]["answer"] = 41
    elif mutation == "artifact":
        document["observation"]["artifact_sha256"] = "b" * 64
    elif mutation == "failed":
        document = output(failed=True)
    elif mutation == "indeterminate":
        document["observation"].update(
            status="indeterminate", output_hash=None, error_code="unknown"
        )
    elif mutation == "claimedVerification":
        document["observation"]["verified"] = True
    elif mutation == "extraEnvelope":
        document["success"] = True
    elif mutation == "missingContent":
        document.pop("content")
    elif mutation == "badContent":
        document["content"] = "unsafe"
    else:
        document["structured_content"] = []
    with pytest.raises(DomainError):
        evaluate(document=document)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"observation":{},"observation":{}}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b'"\xff"',
        b" " * (MAX_WIRE_BYTES + 1),
    ],
    ids=[
        "empty",
        "array",
        "duplicate-keys",
        "nan",
        "infinite-number",
        "invalid-utf8",
        "byte-limit",
    ],
)
def test_malformed_duplicate_or_oversized_retained_json_is_rejected(raw):
    with pytest.raises(InvalidInputError):
        evaluate_oracle(
            oracle(assertion()), raw, invocation_sha256=INVOCATION, output_type_ref=OUTPUT_TYPE
        )


@pytest.mark.parametrize(
    "digest",
    [None, "a" * 63, "A" * 64, "a" * 64 + "\n"],
    ids=["null", "too-short", "uppercase", "trailing-newline"],
)
def test_expected_invocation_must_be_an_exact_digest(digest):
    with pytest.raises(InvalidInputError):
        evaluate(invocation_sha256=digest)


def test_existing_syn_and_synny_type_aliases_remain_equivalent():
    assert evaluate(output_type_ref="syn://type/qualification-result")["passed"]
    with pytest.raises(InvalidInputError):
        evaluate(output_type_ref="synny://thing/not-a-type")
    with pytest.raises(ScopeDeniedError):
        evaluate(output_type_ref="synny://type/another")


def test_success_report_requires_independent_expected_data_not_an_upstream_success_flag():
    document = output({"success": True, "answer": 41})
    assert evaluate(document=document)["passed"] is False
    changed = copy.deepcopy(document)
    changed["structured_content"]["answer"] = 42
    with pytest.raises(StateMismatchError):
        evaluate(document=changed)


def test_uri_like_output_and_literal_code_remain_data_without_fetch_or_execution(monkeypatch):
    import socket
    import subprocess

    def denied(*args, **kwargs):
        raise AssertionError("Oracle attempted execution or remote I/O")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    text = "__import__('os').system('false') https://private.invalid/result"
    assert evaluate(
        oracle(assertion("/content/0/text", text)),
        output(content=[{"type": "text", "text": text}]),
    )["passed"]


def test_lone_surrogate_and_excessive_structure_are_rejected_without_disclosure():
    data = output()
    data["structured_content"]["answer"] = "\ud800"
    raw = json.dumps(data).encode()
    with pytest.raises(InvalidInputError):
        evaluate_oracle(
            oracle(assertion()), raw, invocation_sha256=INVOCATION, output_type_ref=OUTPUT_TYPE
        )
    value = 0
    for _ in range(33):
        value = {"next": value}
    with pytest.raises(InvalidInputError):
        validate_oracle(oracle(assertion(value=value)))
