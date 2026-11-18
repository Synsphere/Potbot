"""Bounded declarative checks over an existing gateway's retained MCP output.

The caller must first verify the gateway completion, artifact and stopped
receipt through their existing owners. These pure checks neither authenticate a
native process nor grant authority, fetch resources or execute peer content.
"""

from __future__ import annotations

import re
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import type_identity
from potbot.tools.capability_contracts import ToolObservation
from potbot.tools.mcp_discovery import bounded_metadata
from potbot.tools.mcp_protocol import (
    MAX_WIRE_BYTES,
    McpCallResult,
    McpProtocolError,
    parse_json_document,
)

ORACLE_SCHEMA = "potbot-mcp-result-oracle/v1"
RESULT_SCHEMA = "potbot-mcp-result-check/v1"
MAX_ASSERTIONS = 16
MAX_ORACLE_BYTES = 65_536
MAX_EXPECTED_BYTES = 16_384
MAX_POINTER_BYTES = 1024
MAX_POINTER_DEPTH = 16
_OPERATIONS = frozenset({"equals", "contains", "length_equals"})
_MISSING = object()


def _pointer(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise InvalidInputError("MCP oracle pointer must be bounded RFC 6901 text")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeError:
        raise InvalidInputError("MCP oracle pointer must be valid UTF-8") from None
    if (
        size > MAX_POINTER_BYTES
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (value and not value.startswith("/"))
        or re.search(r"~(?![01])", value)
    ):
        raise InvalidInputError("MCP oracle pointer must be bounded RFC 6901 text")
    parts = value.split("/")[1:] if value else []
    if len(parts) > MAX_POINTER_DEPTH:
        raise InvalidInputError("MCP oracle pointer exceeds its depth allowance")
    # RFC 6901 decoding must preserve ~01 as the literal token ~1.
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)


def validate_oracle(value: object) -> dict[str, Any]:
    """Return a detached exact oracle; no output is read or trusted here."""
    try:
        oracle = bounded_metadata(value, maximum=MAX_ORACLE_BYTES)
    except (McpProtocolError, ValueError, TypeError, UnicodeError):
        raise InvalidInputError("MCP qualification oracle is not bounded finite JSON") from None
    if (
        not isinstance(oracle, dict)
        or set(oracle) != {"schema", "assertions"}
        or oracle["schema"] != ORACLE_SCHEMA
        or not isinstance(oracle["assertions"], list)
        or not 1 <= len(oracle["assertions"]) <= MAX_ASSERTIONS
    ):
        raise InvalidInputError("MCP qualification oracle has an invalid envelope")
    for assertion in oracle["assertions"]:
        if (
            not isinstance(assertion, dict)
            or set(assertion) != {"op", "pointer", "value"}
            or not isinstance(assertion["op"], str)
            or assertion["op"] not in _OPERATIONS
        ):
            raise InvalidInputError("MCP oracle assertion has an invalid contract")
        _pointer(assertion["pointer"])
        try:
            expected = bounded_metadata(assertion["value"], maximum=MAX_EXPECTED_BYTES)
        except (McpProtocolError, ValueError, TypeError, UnicodeError):
            raise InvalidInputError("MCP oracle expected value exceeds its allowance") from None
        operation = assertion["op"]
        if operation == "contains" and (not isinstance(expected, str) or not expected):
            raise InvalidInputError("MCP text containment requires a nonempty literal string")
        if operation == "length_equals" and (
            type(expected) is not int or not 0 <= expected < 2**53
        ):
            raise InvalidInputError("MCP length assertion requires a nonnegative safe integer")
    return dict(oracle)


def _select(value: Any, parts: tuple[str, ...]) -> Any:
    current = value
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list):
            # A literal object key "01" is legal; an array index "01" is not.
            if re.fullmatch(r"0|[1-9][0-9]*", part, flags=re.ASCII) is None:
                return _MISSING
            # Avoid converting arbitrarily long indices to Python integers.
            if len(part) > len(str(len(current))):
                return _MISSING
            index = int(part)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _check(assertion: dict[str, Any], view: dict[str, Any]) -> str | None:
    actual = _select(view, _pointer(assertion["pointer"]))
    if actual is _MISSING:
        return "pointer_missing"
    expected, operation = assertion["value"], assertion["op"]
    if operation == "equals":
        # Canonical JSON preserves booleans versus integers and 1 versus 1.0.
        return None if canonical_json(actual) == canonical_json(expected) else "value_mismatch"
    if operation == "contains":
        if not isinstance(actual, str):
            return "value_type_mismatch"
        return None if expected in actual else "text_not_found"
    if not isinstance(actual, (list, str)):
        return "value_type_mismatch"
    return None if len(actual) == expected else "length_mismatch"


def evaluate_oracle(
    oracle: object,
    stdout_bytes: bytes,
    *,
    invocation_sha256: str,
    output_type_ref: str,
) -> dict[str, Any]:
    """Check successful hash-linked MCP output without exposing selected values.

    A malformed, incomplete or mismatched observation raises a fixed domain
    error. A well-formed output which fails an explicit assertion produces a
    retained negative result. Neither result is a native qualification receipt.
    """
    checked = validate_oracle(oracle)
    if (
        not isinstance(invocation_sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", invocation_sha256, flags=re.ASCII) is None
    ):
        raise InvalidInputError("MCP oracle requires an exact invocation digest")
    expected_type = type_identity(output_type_ref)
    try:
        document = parse_json_document(stdout_bytes)
        document = bounded_metadata(document, maximum=MAX_WIRE_BYTES)
    except (McpProtocolError, ValueError, TypeError, UnicodeError):
        raise InvalidInputError("MCP gateway output is not bounded finite JSON") from None
    if not isinstance(document, dict) or set(document) != {
        "observation",
        "content",
        "structured_content",
    }:
        raise InvalidInputError("MCP gateway output has an invalid envelope")
    observation = ToolObservation.from_dict(document["observation"])
    if observation.invocation_hash != invocation_sha256 or (
        type_identity(observation.output_type_ref) != expected_type
    ):
        raise ScopeDeniedError("MCP output differs from the selected invocation or result Type")
    if observation.status != "succeeded" or observation.artifact_sha256 is not None:
        raise StateMismatchError("MCP qualification requires a successful retained tool result")
    view = {
        "content": document["content"],
        "structuredContent": document["structured_content"],
        "isError": False,
    }
    result = McpCallResult.from_wire("qualification-output", view)
    if result.result_hash != observation.output_hash:
        raise StateMismatchError("MCP observation hash differs from its retained output")
    checks = []
    for index, assertion in enumerate(checked["assertions"]):
        reason = _check(assertion, view)
        checks.append({"index": index, "passed": reason is None, "reasonCode": reason})
    return {
        "schema": RESULT_SCHEMA,
        "oracleSha256": content_hash(checked),
        "outputSha256": result.result_hash,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }
