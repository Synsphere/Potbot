"""Nonexecuting, versioned output-shape evaluation for NEW prompt trials.

Never repairs output or regrades an existing experiment's primary metric.
Passing this contract proves syntax only, not semantics, safety or qualification.
"""

from __future__ import annotations

import json
from typing import Any


def evaluate_json_object_v1(text: object) -> dict[str, Any]:
    """Bounded literal JSON: no fences, duplicate keys or nonfinite values."""
    result: dict[str, Any] = {
        "schema": "potbot.prompt-json-object-evaluation/1",
        "syntax_valid": False,
        "semantic_correctness": "not_evaluated",
        "repaired": False,
    }
    if not isinstance(text, str):
        return {**result, "reason": "not_text"}
    try:
        if len(text.encode("utf-8")) > 131072:
            return {**result, "reason": "byte_limit"}

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            record: dict[str, Any] = {}
            for key, value in items:
                if key in record:
                    raise ValueError("duplicate_key")
                record[key] = value
            return record

        def constant(_value: str) -> None:
            raise ValueError("nonfinite")

        parsed = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(parsed, dict):
            return {**result, "reason": "not_object"}
        stack = [(parsed, 1)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if depth > 16 or nodes > 4096:
                return {**result, "reason": "structure_limit"}
            if isinstance(item, dict):
                stack.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                stack.extend((v, depth + 1) for v in item)
        # Also refuse exponent overflow, which JSON's constant hook does not catch.
        json.dumps(parsed, allow_nan=False)
    except (ValueError, UnicodeError, RecursionError):
        return {**result, "reason": "invalid_literal_json"}
    return {**result, "syntax_valid": True, "reason": "literal_object"}
