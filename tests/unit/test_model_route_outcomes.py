"""Completion, format and independently reviewed correctness remain separate."""

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.model_route_outcomes import assess_output, summarize_outcomes


def assess(state="succeeded", text='{"answer":42}', **change):
    values = dict(
        state=state,
        result={"completed_models": 1},
        text=text,
        output_contract="json_object_v1",
        provider_receipts=[{"status": "completed", "finish_reason": "stop"}],
    )
    return assess_output(**(values | change))


def test_valid_json_is_not_semantic_or_runtime_qualification():
    report = assess()
    assert report["completion_confirmed"] and report["literal_contract"]["syntax_valid"]
    assert (
        report["semantic_correctness"] == "not_evaluated" and report["runtime_qualified"] is False
    )


@pytest.mark.parametrize("text", ["```json\n{}\n```", '{"a":1,"a":2}', '{"a":NaN}', "[42]"])
def test_format_is_evaluated_without_repair(text):
    report = assess(text=text)
    assert report["completion_confirmed"] and report["literal_contract"]["syntax_valid"] is False
    assert report["literal_contract"]["repaired"] is False


@pytest.mark.parametrize("state", ["failed", "cancelled", "indeterminate"])
def test_even_literal_json_does_not_confirm_an_uncertain_or_failed_job(state):
    assert assess(state=state)["completion_confirmed"] is False


@pytest.mark.parametrize(
    "receipts",
    [
        [],
        [{"status": "indeterminate", "finish_reason": "stop"}],
        [{"status": "completed", "finish_reason": "length"}],
    ],
)
def test_success_metadata_cannot_replace_the_original_provider_receipt(receipts):
    assert assess(provider_receipts=receipts)["completion_confirmed"] is False


def test_benchmark_summary_retains_unknowns_and_never_promotes():
    report = summarize_outcomes(
        [
            {"decision_id": "one", "state": "succeeded", "verdict": "acceptable"},
            {"decision_id": "two", "state": "indeterminate", "verdict": None},
            {"decision_id": "three", "state": "succeeded", "verdict": "regression"},
        ],
        allocations=4,
    )
    assert report["observed_decisions"] == 3 and report["unobserved_decisions"] == 1
    assert report["terminal_states"]["indeterminate"] == 1
    assert report["independent_reviews"]["unreviewed"] == 1 and report["regression_fenced"]
    assert not report["holdout_qualified"] and not report["candidate_execution_enabled"]
    with pytest.raises(InvalidInputError):
        summarize_outcomes([{"decision_id": "one", "state": "succeeded"}] * 2, allocations=2)


def test_evaluation_bounds_and_absent_contract_are_explicit():
    assert assess(output_contract=None)["literal_contract"] is None
    for change in [{"state": "running"}, {"output_contract": "unknown"}, {"text": "x" * 32769}]:
        with pytest.raises(InvalidInputError):
            assess(**change)


def test_impossible_acceptable_review_does_not_enter_an_offline_summary():
    with pytest.raises(InvalidInputError):
        summarize_outcomes(
            [{"decision_id": "lost", "state": "indeterminate", "verdict": "acceptable"}],
            allocations=1,
        )
