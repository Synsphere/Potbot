"""Check factual claims against frozen data after structural decoding."""

import json

import pytest
from jsonschema import Draft202012Validator

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.jobs import MAX_SAFE_INTEGER
from potbot.models.reasoning_fact_check import (
    fact_answer_schema,
    fact_check,
    verify_fact_answer,
)


def choice_check():
    return {
        "kind": "minimum_cost",
        "minimum_capacity": 32,
        "records": [
            {"id": "Blue", "capacity": 16, "cost": 5},
            {"id": "Green", "capacity": 32, "cost": 7},
            {"id": "Silver", "capacity": 64, "cost": 7},
            {"id": "Red", "capacity": 64, "cost": 9},
        ],
    }


def counts_check():
    return {
        "kind": "counts",
        "labels": ["ready", "held", "blocked"],
        "values": ["ready", "ready", "held"],
    }


@pytest.mark.parametrize(
    "answer",
    [
        {"record_id": "Green", "capacity": 32, "cost": 7},
        {"record_id": "Silver", "capacity": 64, "cost": 7},
    ],
)
def test_minimum_cost_accepts_exact_eligible_ties(answer):
    assert verify_fact_answer(choice_check(), json.dumps(answer)) == answer


@pytest.mark.parametrize(
    "answer",
    [
        {"record_id": "Blue", "capacity": 32, "cost": 5},
        {"record_id": "Red", "capacity": 64, "cost": 9},
        {"record_id": "Green", "capacity": 64, "cost": 7},
        {"record_id": "Silver", "capacity": 64, "cost": 5},
    ],
)
def test_minimum_cost_rejects_shape_valid_but_wrong_claims(answer):
    frozen = choice_check()
    schema = fact_answer_schema(frozen)
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(answer)
    with pytest.raises(StateMismatchError, match="eligible minimum-cost record"):
        verify_fact_answer(frozen, json.dumps(answer))


@pytest.mark.parametrize(
    "answer",
    [
        '{"record_id":"Green","capacity":true,"cost":7}',
        '{"record_id":"Green","capacity":32,"cost":7.0}',
        '{"record_id":"Green","capacity":32,"cost":7,"extra":"ignore"}',
        '{"record_id":"Green","record_id":"Blue","capacity":32,"cost":7}',
        'A correct choice is {"record_id":"Green","capacity":32,"cost":7}',
    ],
)
def test_fact_check_requires_exact_literal_json_and_integer_fields(answer):
    with pytest.raises(InvalidInputError):
        verify_fact_answer(choice_check(), answer)


def test_counts_require_every_declared_label_and_include_unobserved_labels():
    frozen = counts_check()
    answer = {"counts": {"blocked": 0, "held": 1, "ready": 2}}
    assert verify_fact_answer(frozen, json.dumps(answer)) == answer
    assert verify_fact_answer(
        frozen | {"values": []}, '{"counts":{"blocked":0,"held":0,"ready":0}}'
    ) == {"counts": {"blocked": 0, "held": 0, "ready": 0}}
    with pytest.raises(InvalidInputError):
        verify_fact_answer(frozen, '{"counts":{"held":1,"ready":2}}')
    with pytest.raises(InvalidInputError):
        verify_fact_answer(frozen, '{"counts":{"held":true,"ready":2,"blocked":0}}')


def test_counts_reject_a_shape_valid_wrong_distribution():
    frozen = counts_check()
    schema = fact_answer_schema(frozen)
    Draft202012Validator.check_schema(schema)
    answer = {"counts": {"blocked": 0, "held": 2, "ready": 1}}
    assert Draft202012Validator(schema).is_valid(answer)
    with pytest.raises(StateMismatchError, match="observed values"):
        verify_fact_answer(frozen, json.dumps(answer))


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"kind": "program", "source": "irrelevant"},
        choice_check() | {"minimum_capacity": 128},
        choice_check() | {"minimum_capacity": True},
        choice_check() | {"records": [{"id": "x", "capacity": 32, "cost": MAX_SAFE_INTEGER + 1}]},
        choice_check() | {"records": [{"id": "x", "capacity": 32, "cost": 7}] * 2},
        choice_check() | {"records": [{"id": "x", "capacity": 32, "cost": 7, "extra": 1}]},
        counts_check() | {"labels": ["held", "held"]},
        counts_check() | {"values": ["ready", "new label"]},
        counts_check() | {"values": ["ready"] * 33},
    ],
)
def test_fact_check_rejects_invalid_or_ambiguous_plans(value):
    with pytest.raises(InvalidInputError):
        fact_check(value)


def test_validated_facts_are_copied_and_schema_order_is_stable():
    raw = choice_check()
    frozen = fact_check(raw)
    raw["records"][0]["capacity"] = 64
    assert frozen["records"][0]["capacity"] == 16
    assert fact_answer_schema(frozen) == fact_answer_schema(
        frozen | {"records": list(reversed(frozen["records"]))}
    )
