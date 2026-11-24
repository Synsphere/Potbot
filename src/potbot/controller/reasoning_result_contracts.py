"""Validate role output against frozen context; never promote assertions to facts.

Reuses the existing hypothesis, critique and steering parsers. Proposals remain
data, even when their representation is accepted. No repair, inference or tools.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from potbot.controller.reasoning_evaluation import parse_critiques
from potbot.controller.reasoning_investigation import parse_hypotheses, parse_steering
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.models.native_results import strict_object


def _branches(context: dict[str, Any], key: str) -> set[str]:
    rows = context.get(key)
    if not isinstance(rows, list) or len(rows) > 12:
        raise InvalidInputError("Reasoning requires bounded frozen branch identities")
    values: set[str] = set()
    for row in rows:
        identity = row.get("node_id", row.get("id")) if isinstance(row, dict) else None
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or identity in values:
            raise InvalidInputError("Frozen reasoning branch identities must be distinct text")
        values.add(identity)
    return values


def validate_reasoning_output(
    role: str, text: str, context: dict[str, Any], *, strict_steering: bool = True
) -> Any:
    try:
        valid = isinstance(text, str) and bool(text.strip()) and len(text.encode()) <= 32768
    except UnicodeError:
        valid = False
    if not valid:
        raise InvalidInputError("Reasoning output must be bounded nonempty text")
    if not isinstance(context, dict):
        raise InvalidInputError("Reasoning requires frozen context")
    if role == "synthesizer":
        return None  # Prose representation; truth still requires independent review.
    literal = strict_object(text, maximum=32768)
    try:
        if role == "hypothesis_generator":
            return [asdict(value) for value in parse_hypotheses(text)]
        if role == "critic":
            expected = _branches(context, "branches")
            if not expected:
                raise InvalidInputError("Critique requires at least one frozen branch")
            return {key: asdict(value) for key, value in parse_critiques(text, expected).items()}
        if role == "investigator":
            queries = literal.get("queries")
            if (
                set(literal) != {"queries"}
                or not isinstance(queries, list)
                or len(queries) > 3
                or any(not isinstance(q, str) or not q.strip() or len(q) > 512 for q in queries)
                or len(set(queries)) != len(queries)
            ):
                raise InvalidInputError("Investigation must contain distinct bounded queries only")
            return literal
        if role == "steering_evaluator":
            value = parse_steering(text)
            if not strict_steering:
                return value  # Existing runner owns clarification of invalid branch proposals.
            action, branch, hypothesis = value["decision"], value["branch_id"], value["hypothesis"]
            if action == "reopen_branch":
                if branch not in _branches(context, "current_hypotheses") or hypothesis is not None:
                    raise StateMismatchError("Steering must reopen one known frozen branch")
            elif action == "spawn_hypothesis":
                if branch is not None or not isinstance(hypothesis, str) or not hypothesis.strip():
                    raise InvalidInputError("Steering spawn requires a new hypothesis only")
            elif branch is not None or hypothesis is not None:
                raise InvalidInputError(
                    "Non-executing steering cannot select a branch or hypothesis"
                )
            return value
    except (TypeError, KeyError, ValueError):
        raise InvalidInputError("Reasoning output violates its declared role") from None
    raise InvalidInputError("Unsupported reasoning role")
