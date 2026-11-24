"""Pure atomic reference algorithms for the offline benchmark, never Cog dispatch.

Only model-visible task packets enter these functions. Gold labels, file paths,
URLs, plugins and executable expressions are not accepted as reference inputs.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json

if TYPE_CHECKING:
    from potbot.evaluation.agentic_model_bench import Generation, ModelTarget
    from potbot.models.prompt_compiler import CompiledPrompt

REFERENCE_MODEL = "potbot/atomic-reference-v1"
REFERENCE_ROLES = ("evidence", "status")
REFERENCE_KINDS = frozenset({"parse_json", "extract_field", "check_exit_status", "check_hash"})
_PREFIX = "Benchmark task packet (data, NOT evaluation labels):\n"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_IMPORTED_SOURCE_SHA256 = hashlib.sha256(
    Path(__file__).read_text(encoding="utf-8").encode()
).hexdigest()


def reference_identity() -> str:
    # Normalize checkout line endings so Windows and Linux pin the same source.
    current = hashlib.sha256(Path(__file__).read_text(encoding="utf-8").encode()).hexdigest()
    if current != _IMPORTED_SOURCE_SHA256:
        raise InvalidInputError("reference source changed after import")
    return current


def reference_runtime() -> dict[str, Any]:
    return {
        "source_sha256": reference_identity(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "peak_ram_bytes": None,
        "peak_vram_bytes": None,
        "scope": "pure reference algorithms; no installed worker or Cog qualification",
    }


def _fields(value: object, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise InvalidInputError("reference input fields differ from its fixed contract")
    return value


def _constant(_value: str) -> Any:
    raise ValueError("non-finite JSON is outside the reference contract")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON fields are ambiguous")
        result[key] = value
    return result


def _strict_json(text: str) -> Any:
    return json.loads(text, parse_constant=_constant, object_pairs_hook=_object)


def evaluate_reference(kind: str, inputs: object) -> dict[str, Any]:
    """Evaluate one bounded, fixed contract without reading a reference answer."""
    if kind not in REFERENCE_KINDS or len(canonical_json(inputs).encode()) > 16384:
        raise InvalidInputError("unsupported or oversized reference input")
    if kind == "parse_json":
        data = _fields(inputs, {"text"})
        if not isinstance(data["text"], str):
            raise InvalidInputError("JSON reference requires text")
        try:
            value = _strict_json(data["text"])
        except (ValueError, RecursionError):
            return {"valid": False, "kind": "invalid"}
        kinds = {
            dict: "object",
            list: "array",
            str: "string",
            int: "number",
            float: "number",
            bool: "boolean",
            type(None): "null",
        }
        # Python may overflow a syntactically valid JSON exponent to infinity.
        # This contract classifies JSON syntax, not machine-float representability.
        return {"valid": True, "kind": kinds[type(value)]}
    if kind == "extract_field":
        data = _fields(inputs, {"document", "key"})
        if not isinstance(data["document"], dict) or not isinstance(data["key"], str):
            raise InvalidInputError("field reference requires an object and literal key")
        found = data["key"] in data["document"]
        return {"found": found, "value": data["document"].get(data["key"])}
    if kind == "check_hash":
        data = _fields(inputs, {"text", "sha256"})
        if (
            not isinstance(data["text"], str)
            or not isinstance(data["sha256"], str)
            or _HEX.fullmatch(data["sha256"]) is None
        ):
            raise InvalidInputError("hash reference requires text and a lowercase SHA-256")
        return {"matches": hashlib.sha256(data["text"].encode()).hexdigest() == data["sha256"]}
    data = _fields(inputs, {"state", "exit_code"})
    state, code = data["state"], data["exit_code"]
    if state not in {"completed", "running", "timed_out", "lost_reply", "unknown"}:
        raise InvalidInputError("unknown process receipt state")
    if code is not None and (type(code) is not int or not -255 <= code <= 255):
        raise InvalidInputError("invalid process exit code")
    if state != "completed" and code is not None:
        raise InvalidInputError("non-terminal receipt cannot supply an exit code")
    status = "indeterminate"
    if state == "completed" and code is not None:
        status = "succeeded" if code == 0 else "failed"
    elif state == "running":
        status = "running"
    return {"status": status}


def generate_reference(target: ModelTarget, prompt: CompiledPrompt) -> Generation:
    from potbot.evaluation.agentic_model_bench import Generation

    if (
        target.model != REFERENCE_MODEL
        or target.backend != "deterministic"
        or target.revision != reference_identity()
        or target.provider is not None
        or target.base_url is not None
        or set(target.roles) - set(REFERENCE_ROLES)
    ):
        raise InvalidInputError("reference target differs from its exact source pin")
    if not prompt.user.startswith(_PREFIX) or len(prompt.user.encode()) > 32768:
        raise InvalidInputError("reference accepts only bounded benchmark packets")
    packet = _fields(_strict_json(prompt.user[len(_PREFIX) :]), {"task", "context"})
    context = _fields(packet["context"], {"atomic_kind", "reference_input"})
    started = time.perf_counter_ns()
    output = evaluate_reference(context["atomic_kind"], context["reference_input"])
    return Generation(
        text=canonical_json(output),
        elapsed_ms=round((time.perf_counter_ns() - started) / 1_000_000),
        billed_microunits=0,
        returned_model=REFERENCE_MODEL,
        origin="deterministic_reference",
    )
