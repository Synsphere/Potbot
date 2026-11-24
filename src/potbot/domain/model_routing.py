"""Pure contracts for controller-owned model routing over existing checked jobs.

These values constrain routing; they grant no authority, qualification or retry.
The PostgreSQL service owns transitions, acceptance, allocation and dispatch.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.jobs import COG_JOB, JobSpec, selected_models
from potbot.domain.operation_workbench import operation_id, parse_request
from potbot.domain.serialization import canonical_json, content_hash


class RoutingStage(StrEnum):
    DRAFT = "draft"
    SHADOW = "shadow"
    CANARY = "canary"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    BLOCKED = "blocked"
    DISABLED = "disabled"


def routing_transition(current: str, requested: str) -> RoutingStage:
    """Validate stage order only; independent evidence is checked by its owner."""
    try:
        before, after = RoutingStage(current), RoutingStage(requested)
    except (ValueError, TypeError):
        raise InvalidInputError("Unknown model routing stage") from None
    forward = {
        RoutingStage.DRAFT: {RoutingStage.SHADOW},
        RoutingStage.SHADOW: {RoutingStage.CANARY},
        RoutingStage.CANARY: {RoutingStage.PROMOTED},
        RoutingStage.PROMOTED: set(),
        RoutingStage.ROLLED_BACK: {RoutingStage.SHADOW},
        RoutingStage.BLOCKED: {RoutingStage.SHADOW},
        RoutingStage.DISABLED: set(),
    }
    allowed = forward[before] | {RoutingStage.DISABLED}
    if before in {RoutingStage.SHADOW, RoutingStage.CANARY, RoutingStage.PROMOTED}:
        allowed |= {RoutingStage.ROLLED_BACK, RoutingStage.BLOCKED}
    if after == before or after not in allowed:
        raise StateMismatchError("Model route transition skips its required stage")
    return after


def routing_choice(
    stage: str,
    policy_id: UUID,
    epoch: int,
    allocation_key: str,
    canary_bps: int,
) -> str:
    """Choose a target, without requesting it or granting execution permission.

    Stable allocation is not permission to repeat work. The durable owner must
    bind this key to one task/request and preserve its original outcome.
    """
    if (
        not isinstance(policy_id, UUID)
        or type(epoch) is not int
        or epoch < 1
        or not isinstance(allocation_key, str)
        or not 1 <= len(allocation_key.encode()) <= 160
        or type(canary_bps) is not int
        or not 1 <= canary_bps <= 1000
    ):
        raise InvalidInputError("Invalid bounded routing allocation")
    try:
        mode = RoutingStage(stage)
    except (ValueError, TypeError):
        raise InvalidInputError("Unknown model routing stage") from None
    if mode in {RoutingStage.DRAFT, RoutingStage.BLOCKED, RoutingStage.DISABLED}:
        raise StateMismatchError("This model route is not active")
    if mode == RoutingStage.PROMOTED:
        return "candidate"
    if mode == RoutingStage.CANARY:
        bucket = int(content_hash([str(policy_id), epoch, allocation_key])[:16], 16) % 10000
        if bucket < canary_bps:
            return "candidate"
    return "baseline"


def route_target(value: object) -> dict[str, Any]:
    """Bind an existing catalogue operation and its exact executor projection."""
    if (
        not isinstance(value, dict)
        or set(value) != {"operation_id", "executor", "runtime_identity_sha256"}
        or not isinstance(value["executor"], dict)
    ):
        raise InvalidInputError("Model route target fields differ")
    operation_id(value["operation_id"])
    operation_id(value["runtime_identity_sha256"])
    projection = value["executor"]
    if set(projection) != {"job_type", "selected_models", "input"} or not isinstance(
        projection["input"], dict
    ):
        raise InvalidInputError("Route target requires a bounded executor projection")
    if projection["job_type"] == "model.generate":
        choices = selected_models(projection["selected_models"])
        if len(choices) != 1 or projection["input"]:
            raise InvalidInputError("Hosted routes require exactly one model identity")
    elif projection["job_type"] == COG_JOB:
        required = {"operation_ref", "profile_sha256", "type_ref", "value"}
        if (
            projection["selected_models"] != []
            or set(projection["input"]) != required
            or any(
                not isinstance(projection["input"][k], str) or not projection["input"][k]
                for k in ("operation_ref", "type_ref")
            )
            or not isinstance(projection["input"]["value"], dict)
            or not projection["input"]["value"]
        ):
            raise InvalidInputError(
                "Native model routes require a pinned Cog and model input identity"
            )
        operation_id(projection["input"]["profile_sha256"])
    else:
        raise InvalidInputError("Route target has no supported checked job handler")
    encoded = canonical_json(value)
    if len(encoded.encode()) > 4096:
        raise InvalidInputError("Route executor projection exceeds its bound")
    import json

    result: dict[str, Any] = json.loads(encoded)
    return result


def _matches(projection: Any, actual: Any, depth: int = 0) -> bool:
    if depth > 12:
        return False
    if isinstance(projection, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches(child, actual[key], depth + 1)
            for key, child in projection.items()
        )
    return canonical_json(projection) == canonical_json(actual)


def bind_route_request(target: object, request: object) -> JobSpec:
    """Retain all ordinary workbench, task, capability and sandbox admission."""
    selected = route_target(target)
    spec = parse_request(request)
    if not _matches(selected["executor"], spec.to_dict()):
        raise StateMismatchError("Checked request differs from the selected route executor")
    return spec
