"""Bounded declarative checks over an existing gateway's retained MCP output.

The caller must first verify the gateway completion, artifact and stopped
receipt through their existing owners. These pure checks neither authenticate a
native process nor grant authority, fetch resources or execute peer content.
"""

from __future__ import annotations

import re
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.result_assertions import (
    MAX_ASSERTIONS as MAX_ASSERTIONS,
)
from potbot.domain.result_assertions import (
    MAX_EXPECTED_BYTES as MAX_EXPECTED_BYTES,
)
from potbot.domain.result_assertions import (
    MAX_ORACLE_BYTES as MAX_ORACLE_BYTES,
)
from potbot.domain.result_assertions import (
    MAX_POINTER_BYTES as MAX_POINTER_BYTES,
)
from potbot.domain.result_assertions import (
    MAX_POINTER_DEPTH as MAX_POINTER_DEPTH,
)
from potbot.domain.result_assertions import (
    evaluate_result_assertions,
    validate_result_oracle,
)
from potbot.domain.serialization import content_hash
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


def validate_oracle(value: object) -> dict[str, Any]:
    """Return a detached exact oracle; no output is read or trusted here."""
    return validate_result_oracle(value, schema=ORACLE_SCHEMA)


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
    checks = evaluate_result_assertions(checked, view, schema=ORACLE_SCHEMA)
    return {
        "schema": RESULT_SCHEMA,
        "oracleSha256": content_hash(checked),
        "outputSha256": result.result_hash,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }
