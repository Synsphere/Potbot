"""Bounded literal assertions shared by retained-result consumers.

These functions evaluate data that an existing result owner has already
verified. They never fetch references, execute expressions, infer success from
the returned text, or confer authority on a task or installation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json

MAX_ASSERTIONS = 16
MAX_ORACLE_BYTES = 65_536
MAX_EXPECTED_BYTES = 16_384
MAX_POINTER_BYTES = 1024
MAX_POINTER_DEPTH = 16
OPERATIONS = frozenset({"equals", "contains", "length_equals"})
_MISSING = object()


def bounded_value(value: object, *, maximum: int) -> Any:
    """Detach finite JSON with a bounded serialized and structural footprint."""
    try:
        raw = canonical_json(value).encode("utf-8", "strict")
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise InvalidInputError("Result assertions require finite UTF-8 JSON") from None
    if len(raw) > maximum:
        raise InvalidInputError("Result assertion data exceeds its byte allowance")
    detached = json.loads(raw)
    stack, count = [(detached, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 32 or count > 8192:
            raise InvalidInputError("Result assertion data exceeds its structure allowance")
        if isinstance(node, dict):
            if any(len(key) > 512 for key in node):
                raise InvalidInputError("Result assertion data has an oversized object key")
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    return detached


def pointer_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise InvalidInputError("Result pointer must be bounded RFC 6901 text")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeError:
        raise InvalidInputError("Result pointer must be valid UTF-8") from None
    if (
        size > MAX_POINTER_BYTES
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (value and not value.startswith("/"))
        or re.search(r"~(?![01])", value)
    ):
        raise InvalidInputError("Result pointer must be bounded RFC 6901 text")
    parts = value.split("/")[1:] if value else []
    if len(parts) > MAX_POINTER_DEPTH:
        raise InvalidInputError("Result pointer exceeds its depth allowance")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)


def validate_result_oracle(value: object, *, schema: str) -> dict[str, Any]:
    """Validate an exact predeclared result specification, preserving JSON types."""
    oracle = bounded_value(value, maximum=MAX_ORACLE_BYTES)
    if (
        not isinstance(oracle, dict)
        or set(oracle) != {"schema", "assertions"}
        or oracle["schema"] != schema
        or not isinstance(oracle["assertions"], list)
        or not 1 <= len(oracle["assertions"]) <= MAX_ASSERTIONS
    ):
        raise InvalidInputError("Result oracle has an invalid envelope")
    for assertion in oracle["assertions"]:
        if (
            not isinstance(assertion, dict)
            or set(assertion) != {"op", "pointer", "value"}
            or not isinstance(assertion["op"], str)
            or assertion["op"] not in OPERATIONS
        ):
            raise InvalidInputError("Result assertion has an invalid contract")
        pointer_parts(assertion["pointer"])
        expected = bounded_value(assertion["value"], maximum=MAX_EXPECTED_BYTES)
        if assertion["op"] == "contains" and (not isinstance(expected, str) or not expected):
            raise InvalidInputError("Text containment requires a nonempty literal string")
        if assertion["op"] == "length_equals" and (
            type(expected) is not int or not 0 <= expected < 2**53
        ):
            raise InvalidInputError("Length assertions require a nonnegative safe integer")
    return dict(oracle)


def _select(value: Any, parts: tuple[str, ...]) -> Any:
    current = value
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list):
            if re.fullmatch(r"0|[1-9][0-9]*", part, flags=re.ASCII) is None:
                return _MISSING
            if len(part) > len(str(len(current))):
                return _MISSING
            index = int(part)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _check(assertion: dict[str, Any], view: Any) -> str | None:
    actual = _select(view, pointer_parts(assertion["pointer"]))
    if actual is _MISSING:
        return "pointer_missing"
    expected, operation = assertion["value"], assertion["op"]
    if operation == "equals":
        return None if canonical_json(actual) == canonical_json(expected) else "value_mismatch"
    if operation == "contains":
        if not isinstance(actual, str):
            return "value_type_mismatch"
        return None if expected in actual else "text_not_found"
    if not isinstance(actual, (list, str)):
        return "value_type_mismatch"
    return None if len(actual) == expected else "length_mismatch"


def evaluate_result_assertions(
    oracle: object, view: Any, *, schema: str
) -> list[dict[str, Any]]:
    """Return value-blind checks; callers retain the verified output identity."""
    checked = validate_result_oracle(oracle, schema=schema)
    checks = []
    for index, assertion in enumerate(checked["assertions"]):
        reason = _check(assertion, view)
        checks.append({"index": index, "passed": reason is None, "reasonCode": reason})
    return checks
