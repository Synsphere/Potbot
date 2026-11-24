"""Provider-neutral schemas for two frozen reasoning-role output contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash

ROLE_SCHEMA_VARIANT = "model_role_schema_v1"
ROLE_SCHEMA_PROMPT_VERSION = "model-aware-reasoning-role-schema-v1"
STRUCTURED_ROLES = frozenset({"hypothesis_generator", "critic"})


def _object(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


def _array(items: dict[str, Any], *, most: int, least: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": least, "maxItems": most}


def _hypotheses() -> dict[str, Any]:
    text = {"type": "string"}
    question = _object(
        {
            "channel": {
                "type": "string",
                "enum": ["external_libraries", "external_tools", "internal_workflows"],
            },
            "query": text,
        }
    )
    item = _object(
        {
            "hypothesis": text,
            "assumptions": _array(text, most=3),
            "questions": _array(question, most=3),
        }
    )
    return _object({"hypotheses": _array(item, least=1, most=3)})


def _critique(branch_ids: tuple[str, ...]) -> dict[str, Any]:
    text = {"type": "string"}
    # Enumerated integers work across the selected schema transports. Exact
    # branch coverage still requires the existing independent role validator.
    score = {"type": "integer", "enum": [0, 1, 2, 3, 4]}
    item = _object(
        {
            "node_id": {"type": "string", "enum": list(branch_ids)},
            "relevance": score,
            "evidence_alignment": score,
            "falsification_risk": score,
            "uncertainty": score,
            "recommendation": {"type": "string", "enum": ["keep", "prune", "reject"]},
            "rationale": text,
            "issues": _array(text, most=6),
            "tests": _array(text, most=6),
        }
    )
    size = len(branch_ids)
    return _object({"evaluations": _array(item, least=size, most=size)})


@dataclass(frozen=True, slots=True)
class ReasoningOutputSpec:
    """A small typed selector for an application-owned schema."""

    role: str
    branch_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in STRUCTURED_ROLES:
            raise InvalidInputError("The reasoning role has no structured schema")
        if (
            not isinstance(self.branch_ids, tuple)
            or any(
                not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isprintable()
                for value in self.branch_ids
            )
            or len(set(self.branch_ids)) != len(self.branch_ids)
            or (self.role == "critic" and not 1 <= len(self.branch_ids) <= 12)
            or (self.role != "critic" and self.branch_ids)
        ):
            raise InvalidInputError("A critique needs distinct bounded frozen branch IDs")
        object.__setattr__(self, "branch_ids", tuple(sorted(self.branch_ids)))

    @classmethod
    def from_context(cls, role: str, context: object) -> ReasoningOutputSpec:
        if not isinstance(context, dict):
            raise InvalidInputError("Reasoning output requires frozen context")
        if role != "critic":
            return cls(role)
        branches = context.get("branches")
        if not isinstance(branches, list) or not 1 <= len(branches) <= 12:
            raise InvalidInputError("A critique needs its frozen branches")
        branch_ids = []
        for branch in branches:
            value = branch.get("node_id", branch.get("id")) if isinstance(branch, dict) else None
            if not isinstance(value, str):
                raise InvalidInputError("A critique needs exact frozen branch IDs")
            branch_ids.append(value)
        return cls(role, tuple(branch_ids))

    @property
    def name(self) -> str:
        return (
            "potbot_hypotheses_v1" if self.role == "hypothesis_generator" else "potbot_critique_v1"
        )

    def schema(self) -> dict[str, Any]:
        return _hypotheses() if self.role == "hypothesis_generator" else _critique(self.branch_ids)

    @property
    def fingerprint(self) -> str:
        return content_hash(self.schema())
