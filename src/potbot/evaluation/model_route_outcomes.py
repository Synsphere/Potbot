"""Separate observed completion, literal format and independent correctness.

Pure retained-evidence summaries. No model invocation, output repair, automatic
qualification, fallback selection or reinterpretation of frozen benchmarks.
"""

from __future__ import annotations

from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.prompt_contracts import evaluate_json_object_v1

VERDICTS = frozenset({"acceptable", "regression", "inconclusive"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})


def assess_output(
    state: str,
    result: dict[str, Any] | None,
    text: str,
    output_contract: str | None,
    provider_receipts: list[dict[str, Any]],
    *,
    native_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A complete observed reply and literal JSON are separate from correctness."""
    if state not in TERMINAL_STATES or output_contract not in (None, "json_object_v1"):
        raise InvalidInputError("Unsupported route outcome state or contract")
    if not isinstance(text, str) or len(text.encode()) > 32768:
        raise InvalidInputError("Route outcome text exceeds the review bound")
    if native_evidence is None:
        complete_reply = (
            isinstance(result, dict)
            and result.get("completed_models") == 1
            and len(provider_receipts) == 1
            and provider_receipts[0].get("status") == "completed"
            and provider_receipts[0].get("finish_reason") == "stop"
        )
    else:
        complete_reply = native_evidence.get("finish_reason") == "stop"
    complete = state == "succeeded" and complete_reply
    literal = evaluate_json_object_v1(text) if output_contract else None
    return {
        "schema": "potbot-route-output-assessment/v1",
        "completion_confirmed": complete,
        "literal_contract": literal,
        "semantic_correctness": "not_evaluated",
        "runtime_qualified": False,
    }


def summarize_outcomes(rows: list[dict[str, Any]], *, allocations: int) -> dict[str, Any]:
    """Count distinct decisions, never repeats as independent holdout evidence."""
    if type(allocations) is not int or allocations < len(rows) or len(rows) > 1000:
        raise InvalidInputError("Outcome summary exceeds its bounded inventory")
    seen: set[str] = set()
    states = dict.fromkeys(sorted(TERMINAL_STATES), 0)
    reviews = dict.fromkeys(sorted(VERDICTS | {"unreviewed"}), 0)
    for row in rows:
        identity, state, verdict = row["decision_id"], row["state"], row.get("verdict")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in seen
            or state not in states
            or verdict not in VERDICTS | {None}
            or (verdict == "acceptable" and state != "succeeded")
        ):
            raise InvalidInputError("Outcome summary has duplicate or unsupported evidence")
        seen.add(identity)
        states[state] += 1
        reviews[verdict or "unreviewed"] += 1
    return {
        "schema": "potbot-route-outcome-summary/v1",
        "allocations": allocations,
        "observed_decisions": len(rows),
        "unobserved_decisions": allocations - len(rows),
        "terminal_states": states,
        "independent_reviews": reviews,
        "regression_fenced": reviews["regression"] > 0,
        "holdout_qualified": False,
        "runtime_qualified": False,
        "candidate_execution_enabled": False,
    }
