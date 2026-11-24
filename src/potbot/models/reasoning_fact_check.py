"""Deterministic checks over the frozen typed facts in a reasoning plan."""

from __future__ import annotations

from collections import Counter
from typing import Any

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.jobs import MAX_SAFE_INTEGER, safe_integer
from potbot.models.native_results import strict_object

_MAX_RECORDS = 32


def _identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not value.isprintable()
        or value != value.strip()
    ):
        raise InvalidInputError("Facts require bounded nonempty identifiers")
    return value


def _minimum_cost(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"kind", "minimum_capacity", "records"}:
        raise InvalidInputError("A minimum cost check requires its threshold and records")
    threshold = safe_integer(value["minimum_capacity"], "Minimum capacity")
    records = value["records"]
    if not isinstance(records, list) or not 1 <= len(records) <= _MAX_RECORDS:
        raise InvalidInputError("A minimum cost check requires a bounded list of records")
    normalized: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict) or set(row) != {"id", "capacity", "cost"}:
            raise InvalidInputError("Each choice requires an ID, capacity and cost")
        normalized.append(
            {
                "id": _identifier(row["id"]),
                "capacity": safe_integer(row["capacity"], "Capacity"),
                "cost": safe_integer(row["cost"], "Cost"),
            }
        )
    if len({row["id"] for row in normalized}) != len(normalized):
        raise InvalidInputError("Choice IDs must be distinct")
    if not any(row["capacity"] >= threshold for row in normalized):
        raise InvalidInputError("At least one record must meet the minimum capacity")
    return {"kind": "minimum_cost", "minimum_capacity": threshold, "records": normalized}


def _counts(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"kind", "labels", "values"}:
        raise InvalidInputError("A counts check requires labels and observed values")
    labels, values = value["labels"], value["values"]
    if (
        not isinstance(labels, list)
        or not 1 <= len(labels) <= _MAX_RECORDS
        or not isinstance(values, list)
        or len(values) > _MAX_RECORDS
    ):
        raise InvalidInputError("Counts require bounded lists of labels and observations")
    allowed = [_identifier(label) for label in labels]
    if len(set(allowed)) != len(allowed) or any(
        not isinstance(item, str) or item not in allowed for item in values
    ):
        raise InvalidInputError("Labels must be distinct and every observation must use a label")
    return {"kind": "counts", "labels": allowed, "values": list(values)}


def fact_check(value: object) -> dict[str, Any]:
    """Validate and copy the explicit facts supplied in the reasoning plan."""
    if isinstance(value, dict):
        if value.get("kind") == "minimum_cost":
            return _minimum_cost(value)
        if value.get("kind") == "counts":
            return _counts(value)
    raise InvalidInputError("Unsupported reasoning fact check")


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def fact_answer_schema(check: dict[str, Any]) -> dict[str, Any]:
    """Build an application-owned shape without providing the factual answer."""
    frozen = fact_check(check)
    if frozen["kind"] == "minimum_cost":
        amount = {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER}
        return _object(
            {
                "record_id": {
                    "type": "string",
                    "enum": sorted(row["id"] for row in frozen["records"]),
                },
                "capacity": amount.copy(),
                "cost": amount,
            }
        )
    amount = {"type": "integer", "minimum": 0, "maximum": len(frozen["values"])}
    return _object(
        {"counts": _object({label: amount.copy() for label in sorted(frozen["labels"])})}
    )


def verify_fact_answer(check: dict[str, Any], text: str) -> dict[str, Any]:
    """Recompute the answer from the frozen records and validate the literal reply."""
    frozen = fact_check(check)
    answer = strict_object(text)
    if frozen["kind"] == "minimum_cost":
        if set(answer) != {"record_id", "capacity", "cost"}:
            raise InvalidInputError("The answer requires the record ID, capacity and cost")
        selected_id = _identifier(answer["record_id"])
        capacity = safe_integer(answer["capacity"], "Answer capacity")
        cost = safe_integer(answer["cost"], "Answer cost")
        eligible = [
            row for row in frozen["records"] if row["capacity"] >= frozen["minimum_capacity"]
        ]
        cheapest = min(row["cost"] for row in eligible)
        if cost != cheapest or not any(
            row["id"] == selected_id and row["capacity"] == capacity and row["cost"] == cost
            for row in eligible
        ):
            raise StateMismatchError("The answer differs from an eligible minimum-cost record")
        return answer

    counts = answer.get("counts")
    if (
        set(answer) != {"counts"}
        or not isinstance(counts, dict)
        or set(counts) != set(frozen["labels"])
    ):
        raise InvalidInputError("The answer requires a count for each declared label")
    observed = Counter(frozen["values"])
    actual = {label: safe_integer(counts[label], "Answer count") for label in frozen["labels"]}
    if actual != {label: observed[label] for label in frozen["labels"]}:
        raise StateMismatchError("The answer counts differ from the observed values")
    return answer
