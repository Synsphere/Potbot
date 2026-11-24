"""Strict native inference data contracts; no loader, transport or qualification.

The result is a worker report bound to an original request and registered
deployment. Matching these fields is consistency, not proof of loaded weights.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, SchemaViolationError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.models.registry import DeploymentSpec
from potbot.semantics.registry import strict_json

REQUEST = "syn://type/potbot-local-model-request-v1"
RESULT = "syn://type/potbot-local-model-result-v1"
MAX_RESULT_BYTES = 16384


def strict_object(raw: bytes | str, *, maximum: int = MAX_RESULT_BYTES) -> dict[str, Any]:
    try:
        if len(raw.encode() if isinstance(raw, str) else raw) > maximum:
            raise ValueError("byte limit")
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("object required")
        canonical_json(value).encode("utf-8")
        stack = [(value, 1)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if depth > 16 or nodes > 4096:
                raise ValueError("structure limit")
            if isinstance(item, dict):
                stack.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                stack.extend((v, depth + 1) for v in item)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, SchemaViolationError):
        raise InvalidInputError("Model result is not bounded literal JSON") from None


def validate_native_request(value: object, deployment: DeploymentSpec) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "model",
        "deployment_sha256",
        "system",
        "prompt",
        "max_output_tokens",
    }:
        raise InvalidInputError("Native model request has unsupported fields")
    result = strict_object(canonical_json(value))
    if (
        result["model"] != deployment.canonical_model
        or result["deployment_sha256"] != deployment.fingerprint
    ):
        raise StateMismatchError("Native request selects a different exact deployment")
    if (
        any(not isinstance(result[k], str) for k in ("prompt", "system"))
        or not result["prompt"].strip()
    ):
        raise InvalidInputError("Native request needs literal prompt and system text")
    if (
        type(result["max_output_tokens"]) is not int
        or not 1 <= result["max_output_tokens"] <= deployment.max_output_tokens
    ):
        raise InvalidInputError("Native output bound differs from deployment")
    return result


def parse_native_result(
    raw: bytes, *, request: object, job_id: UUID, deployment: DeploymentSpec
) -> dict[str, Any]:
    selected = validate_native_request(request, deployment)
    payload = strict_object(raw)
    if set(payload) != {"type_ref", "value"} or payload["type_ref"] != RESULT:
        raise StateMismatchError("Native result has a different declared output port")
    value = payload["value"]
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "invocation_id",
        "request_sha256",
        "deployment_sha256",
        "model",
        "provenance",
        "text",
        "finish_reason",
        "usage",
    }:
        raise InvalidInputError("Native result has unsupported fields")
    if (
        not isinstance(job_id, UUID)
        or value["schema"] != "potbot.native-model-result/1"
        or value["invocation_id"] != str(job_id)
        or value["request_sha256"] != content_hash(selected)
        or value["deployment_sha256"] != deployment.fingerprint
        or value["model"] != deployment.canonical_model
    ):
        raise StateMismatchError("Native result belongs to a different invocation or deployment")
    expected = {
        "weights_sha256": deployment.weights_digest,
        "backend": deployment.backend,
        "backend_version": deployment.backend_version,
        "tokenizer_revision": deployment.tokenizer_revision,
        "chat_template_sha256": deployment.chat_template_digest,
    }
    if value["provenance"] != expected:
        raise StateMismatchError("Native result provenance differs from the registered deployment")
    if (
        not isinstance(value["text"], str)
        or not value["text"].strip()
        or len(value["text"].encode()) > 8192
    ):
        raise InvalidInputError("Native result text is empty or exceeds its bound")
    usage = value["usage"]
    if (
        not isinstance(usage, dict)
        or set(usage) != {"input_tokens", "output_tokens", "total_tokens"}
        or any(
            type(usage[k]) is not int or not 0 <= usage[k] <= deployment.usable_context_tokens
            for k in usage
        )
        or (
            usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]
            or not 1 <= usage["output_tokens"] <= selected["max_output_tokens"]
            or usage["input_tokens"] < 1
        )
    ):
        raise InvalidInputError("Native result has invalid or over-limit token usage")
    if not isinstance(value["finish_reason"], str) or value["finish_reason"] not in {
        "stop",
        "length",
        "cancelled",
        "failed",
    }:
        raise InvalidInputError("Native result has an unsupported finish reason")
    return value
