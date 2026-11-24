"""Versioned, bounded opt-in Ollama benchmark transport; no production RPC route.

The v1 profile is immutable. A changed generation policy requires another
profile/backend version and a separately frozen experiment, never silent tuning.
Server manifest/residency checks are declarations, not weight attestation.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json

if TYPE_CHECKING:
    from potbot.evaluation.agentic_model_bench import Generation, ModelTarget
    from potbot.models.prompt_compiler import CompiledPrompt

BACKEND = "loopback_ollama_v1"


def transport_profile() -> dict[str, Any]:
    """Fresh detached identity; imports and plan preparation never contact a model."""
    return {
        "schema": "potbot.ollama-benchmark/1",
        "think": False,
        "temperature": 0,
        "seed": 42,
        "num_ctx": 4096,
        "keep_alive": "2m",
        "stream": False,
        "timeout_seconds": 90,
        "max_response_bytes": 131072,
        "force_response_schema": False,
        "automatic_retry": False,
        "manifest_before_and_residency_after": True,
    }


def expected_manifest(revision: str) -> str:
    if not isinstance(revision, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", revision):
        raise InvalidInputError("Native Ollama benchmarks require a pinned source manifest")
    return revision.removeprefix("sha256:")


def _json(client: Any, method: str, url: str, deadline: float, payload: Any = None) -> Any:
    import httpx

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Ollama benchmark request exceeded its shared deadline")
    options = {"json": payload} if payload is not None else {}
    with client.stream(
        method,
        url,
        follow_redirects=False,
        timeout=httpx.Timeout(remaining, connect=min(5.0, remaining)),
        **options,
    ) as response:
        if response.status_code != 200:
            raise InvalidInputError("Ollama benchmark returned a non-success response")
        chunks, total = [], 0
        for chunk in response.iter_bytes(chunk_size=8192):
            total += len(chunk)
            if total > 131072 or time.monotonic() > deadline:
                raise InvalidInputError("Ollama benchmark response exceeded its bound")
            chunks.append(chunk)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidInputError("Duplicate Ollama response field")
            result[key] = value
        return result

    value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=pairs)
    canonical_json(value)  # Reject nonfinite values without coercion.
    return value


def generate_ollama(
    target: ModelTarget, prompt: CompiledPrompt, http_client: Any = None
) -> Generation:
    import httpx

    from potbot.evaluation.agentic_model_bench import Generation
    from potbot.evaluation.agentic_model_transport import validate_ollama_url

    if target.backend != BACKEND or target.base_url is None:
        raise InvalidInputError("Not a versioned native Ollama benchmark target")
    base, digest = validate_ollama_url(target.base_url), expected_manifest(target.revision)
    profile = transport_profile()
    started = time.monotonic()
    deadline = started + profile["timeout_seconds"]
    own_client = http_client is None
    client = http_client or httpx.Client(
        follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=1)
    )
    try:
        tags = _json(client, "GET", base + "/tags", deadline)
        records = tags.get("models") if isinstance(tags, dict) else None
        matched = (
            [row for row in records if isinstance(row, dict) and row.get("name") == target.model]
            if isinstance(records, list)
            else []
        )
        if len(matched) != 1 or matched[0].get("digest") != digest:
            raise InvalidInputError("Installed Ollama manifest differs from the frozen target")
        reply = _json(
            client,
            "POST",
            base + "/chat",
            deadline,
            {
                "model": target.model,
                "messages": [
                    {"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user},
                ],
                "stream": False,
                "think": False,
                "keep_alive": profile["keep_alive"],
                "options": {
                    "temperature": profile["temperature"],
                    "seed": profile["seed"],
                    "num_ctx": profile["num_ctx"],
                    "num_predict": target.max_output_tokens,
                },
            },
        )
        message = reply.get("message") if isinstance(reply, dict) else None
        if (
            not isinstance(reply, dict)
            or reply.get("done") is not True
            or reply.get("done_reason") not in {"stop", "length"}
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), str)
            or message.get("thinking") not in (None, "")
            or not isinstance(reply.get("model"), str)
            or any(
                type(reply.get(k)) is not int or reply[k] < 0
                for k in ("prompt_eval_count", "eval_count")
            )
        ):
            raise InvalidInputError("Ollama completion is unresolved or violates its profile")
        # A returned model mismatch must reach the ordinary runner's stop fence.
        if reply["model"] == target.model:
            resident = _json(client, "GET", base + "/ps", deadline)
            rows = resident.get("models") if isinstance(resident, dict) else None
            loaded = (
                [row for row in rows if isinstance(row, dict) and row.get("name") == target.model]
                if isinstance(rows, list)
                else []
            )
            if len(loaded) != 1 or loaded[0].get("digest") != digest:
                raise InvalidInputError("Loaded Ollama manifest differs from the frozen target")
        return Generation(
            message["content"],
            round((time.monotonic() - started) * 1000),
            reply["prompt_eval_count"],
            reply["eval_count"],
            None,
            reply["model"],
            "ollama-native-v1-" + reply["done_reason"],
        )
    finally:
        if own_client:
            client.close()
