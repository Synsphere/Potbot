"""Bounded live workspace changes and exact native node-service reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any
from uuid import UUID

from potbot.domain.configuration import DEFAULTS, resolve_configuration
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json

SCHEMA = "potbot-live-controls/v1"
NODE_OPERATION = "node_service_control/v1"
EDITABLE = tuple(name for name in DEFAULTS if name not in {"secret_refs", "source_access"})
WORKER_PREFIX = "potbot-node-"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,249}\Z")


def identity(value: Any) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = UUID(value)
        if str(result) != value or result.int == 0:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("A canonical, nonzero identity is required") from None


def revision(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise InvalidInputError("A bounded expected revision is required")
    return value


def digest(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvalidInputError("An exact SHA-256 identity is required")
    return value


def overrides(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - set(EDITABLE):
        raise InvalidInputError("Only the declared workspace configuration fields can be edited")
    if len(canonical_json(value).encode()) > 16_384:
        raise InvalidInputError("Workspace configuration exceeds its byte allowance")
    resolve_configuration({"workspace": value})
    for name in ("reasoning_models", "coding_models", "allowed_tools"):
        for item in value.get(name, []):
            if not _NAME.fullmatch(item) or ".." in item:
                raise InvalidInputError("Selections require bounded model or tool identities")
    return dict(json.loads(canonical_json(value)))


def configuration_review(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"requestId", "expectedRevision", "expectedSha256", "values", "confirm"}
        or value["confirm"] is not True
    ):
        raise InvalidInputError("An exact confirmed workspace configuration review is required")
    identity(value["requestId"])
    revision(value["expectedRevision"])
    digest(value["expectedSha256"])
    return {**value, "values": overrides(value["values"])}


def node_review(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "requestId",
            "nodeId",
            "expectedGeneration",
            "expectedRevision",
            "expectedIncarnation",
            "action",
            "confirm",
        }
        or value["confirm"] is not True
    ):
        raise InvalidInputError("An exact confirmed node-service review is required")
    for key in ("requestId", "nodeId", "expectedIncarnation"):
        identity(value[key])
    revision(value["expectedGeneration"], minimum=1)
    revision(value["expectedRevision"], minimum=1)
    if not isinstance(value["action"], str) or value["action"] not in {"drain", "restart"}:
        raise InvalidInputError("Node service action must be drain or restart")
    return dict(value)


def proof(workspace_id: UUID, principal_id: UUID, review: dict[str, Any], token: str) -> str:
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise InvalidInputError("Native setup authority is unavailable")
    payload = [SCHEMA, str(workspace_id), str(principal_id), node_review(review)]
    return hmac.new(token.encode(), canonical_json(payload).encode(), hashlib.sha256).hexdigest()


def worker_identity(node_id: UUID, incarnation: UUID) -> str:
    return WORKER_PREFIX + node_id.hex + "-" + incarnation.hex
