"""Pure task-aware discovery over bounded observations of existing owners.

The sealed SYN catalogue supplies directional subtype checks: supplied input
must be a subtype of an accepted input, and a produced output must be a subtype
of the requested output. Lists describe independently advertised capabilities,
not an inferred positional signature. Discovery never grants execution authority.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any
from uuid import UUID

from potbot.controller.discovery_ranking import CLASS_PHRASES, _tokens, inferred_classes
from potbot.domain.discovery import (
    CAPABILITY_CLASSES,
    EFFECTS,
    PLATFORMS,
    digest_value,
    text_value,
)
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.semantics.catalog import (
    builtin_types,
    canonical_type_ref,
    catalog_fingerprint,
    extension_types,
    type_ancestors,
)

MAX_TASK_CANDIDATES = 256
MAX_TASK_CANDIDATE_BYTES = 1024 * 1024
_CAPABILITIES = frozenset(
    {"classes", "inputTypes", "outputTypes", "effects", "scopes", "platforms"}
)
_CANDIDATE_FIELDS = frozenset(
    {
        "id",
        "kind",
        "name",
        "description",
        "ownerId",
        "bindingId",
        "ownerSha256",
        "capabilities",
        "requiredTaskCapability",
        "installed",
        "authorityCurrent",
        "reasonCodes",
        "taskEvidence",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {"status", "suiteId", "reportId", "reportSha256", "caseCount", "passedCases"}
)
_WEIGHTS = {
    "classes": 1000,
    "lexicalRecall": 200,
    "lexicalPrecision": 100,
    "exactTypes": 300,
    "compatibleTypes": 200,
    "installed": 2000,
    "evidence": 250,
    "semantic": 400,
}


@lru_cache(maxsize=1)
def _ancestry() -> dict[str, tuple[str, ...]]:
    return type_ancestors((*builtin_types(), *extension_types()))


def _strings(value: Any, label: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise InvalidInputError(f"{label} must be a bounded list")
    result = [text_value(item, label, 256) for item in value]
    if len(set(result)) != len(result):
        raise InvalidInputError(f"{label} must contain unique values")
    return sorted(result)


def _uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("Discovery owner references require canonical UUIDs") from None
    return value


def normalize_constraints(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the exact canonical constraint envelope; ``None`` means unrestricted.

    Unknown query types are rejected against the sealed catalogue. Both public
    SYN schemes are accepted and normalized; aliases cannot add duplicate types.
    An empty permitted-effects/scopes list is a real constraint, unlike ``None``.
    """
    raw = {} if raw is None else raw
    fields = {
        "classes",
        "inputTypes",
        "outputTypes",
        "platform",
        "permittedEffects",
        "permittedScopes",
        "requireLocal",
        "requireTaskEvidence",
    }
    if not isinstance(raw, dict) or set(raw) - fields:
        raise InvalidInputError("Task discovery constraints contain unsupported fields")
    result: dict[str, Any] = {}
    for key in ("classes", "inputTypes", "outputTypes"):
        result[key] = _strings(raw.get(key, []), "Task discovery " + key)
    if not set(result["classes"]) <= CAPABILITY_CLASSES:
        raise InvalidInputError("Task discovery classes must use the published vocabulary")
    for key in ("inputTypes", "outputTypes"):
        normalized = [str(canonical_type_ref(value)) for value in result[key]]
        if len(normalized) != len(set(normalized)):
            raise InvalidInputError("Task discovery type aliases must identify unique types")
        if not set(normalized) <= _ancestry().keys():
            raise InvalidInputError("Task discovery type is absent from the sealed catalogue")
        result[key] = sorted(normalized)
    platform = raw.get("platform")
    if platform is not None and (not isinstance(platform, str) or platform not in PLATFORMS):
        raise InvalidInputError("Task discovery platform is unsupported")
    result["platform"] = platform
    for key in ("permittedEffects", "permittedScopes"):
        value = raw.get(key)
        result[key] = None if value is None else _strings(value, "Task discovery " + key)
    if result["permittedEffects"] is not None and not set(result["permittedEffects"]) <= EFFECTS:
        raise InvalidInputError("Task discovery effects must use the published vocabulary")
    for key in ("requireLocal", "requireTaskEvidence"):
        value = raw.get(key, False)
        if type(value) is not bool:
            raise InvalidInputError("Task discovery requirement flags must be boolean")
        result[key] = value
    return result


def validate_task_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and copy the safe source projection, failing before any truncation."""
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    size = 2
    for candidate in candidates:
        if len(output) >= MAX_TASK_CANDIDATES:
            raise InvalidInputError("Task discovery exceeds its 256-candidate allowance")
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
            raise InvalidInputError("Task discovery candidate fields are invalid")
        identity = digest_value(candidate["id"], "Candidate identity")
        if identity in seen:
            raise InvalidInputError("Task discovery candidate identities must be unique")
        seen.add(identity)
        if not isinstance(candidate["kind"], str) or candidate["kind"] not in {
            "registry",
            "operation",
            "mcp",
        }:
            raise InvalidInputError("Task discovery candidate kind is unsupported")
        text_value(candidate["name"], "Candidate name", 256)
        text_value(candidate["description"], "Candidate description", 4096, empty=True)
        if candidate["kind"] == "operation":
            digest_value(candidate["ownerId"], "Operation owner identity")
        else:
            _uuid(candidate["ownerId"])
        if candidate["bindingId"] is not None:
            _uuid(candidate["bindingId"])
        if (candidate["kind"] == "mcp") != (candidate["bindingId"] is not None):
            raise InvalidInputError("Only MCP candidates require a binding identity")
        digest_value(candidate["ownerSha256"], "Candidate owner digest")
        for key in ("installed", "authorityCurrent"):
            if type(candidate[key]) is not bool:
                raise InvalidInputError("Candidate owner observations must be boolean")
        required = candidate["requiredTaskCapability"]
        if required is not None:
            text_value(required, "Required task capability", 256)
        capabilities = candidate["capabilities"]
        if not isinstance(capabilities, dict) or set(capabilities) != _CAPABILITIES:
            raise InvalidInputError("Candidate capabilities must use the exact bounded schema")
        capabilities = {
            key: _strings(capabilities[key], "Candidate " + key, maximum=32)
            for key in sorted(_CAPABILITIES)
        }
        if (
            not set(capabilities["classes"]) <= CAPABILITY_CLASSES
            or not set(capabilities["effects"]) <= EFFECTS
            or not set(capabilities["platforms"]) <= PLATFORMS
        ):
            raise InvalidInputError("Candidate classifications must use the published vocabulary")
        for key in ("inputTypes", "outputTypes"):
            refs = [str(canonical_type_ref(value)) for value in capabilities[key]]
            if len(refs) != len(set(refs)):
                raise InvalidInputError("Candidate type aliases must identify unique types")
            # Unknown, syntactically valid owner types remain explicit. They do
            # not become canonical simply because a source declared them.
            capabilities[key] = sorted(refs)
        reasons = _strings(candidate["reasonCodes"], "Candidate reason codes", maximum=64)
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason, flags=re.ASCII) for reason in reasons
        ):
            raise InvalidInputError("Candidate reason codes must be bounded lowercase codes")
        evidence = candidate["taskEvidence"]
        if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS:
            raise InvalidInputError("Task evidence fields are invalid")
        if not isinstance(evidence["status"], str) or evidence["status"] not in {
            "none",
            "matching_pass",
            "matching_failure",
            "historical",
        }:
            raise InvalidInputError("Task evidence status is unsupported")
        for key in ("caseCount", "passedCases"):
            if type(evidence[key]) is not int or not 0 <= evidence[key] <= 8:
                raise InvalidInputError("Task evidence case counts are invalid")
        if evidence["passedCases"] > evidence["caseCount"]:
            raise InvalidInputError("Task evidence passed cases exceed the report count")
        if evidence["status"] == "none":
            if any(evidence[key] is not None for key in ("suiteId", "reportId", "reportSha256")):
                raise InvalidInputError("Absent task evidence may not claim a report")
            if evidence["caseCount"] or evidence["passedCases"]:
                raise InvalidInputError("Absent task evidence may not claim case results")
        else:
            _uuid(evidence["suiteId"])
            _uuid(evidence["reportId"])
            digest_value(evidence["reportSha256"], "Task evidence digest")
            if not evidence["caseCount"]:
                raise InvalidInputError("Retained task evidence requires a bounded case result")
        if (
            evidence["status"] == "matching_pass"
            and evidence["passedCases"] != evidence["caseCount"]
        ):
            raise InvalidInputError("Passing task evidence must account for every case")
        copied = {
            **candidate,
            "capabilities": capabilities,
            "reasonCodes": reasons,
            "taskEvidence": dict(evidence),
        }
        size += len(canonical_json(copied).encode("utf-8")) + bool(output)
        if size > MAX_TASK_CANDIDATE_BYTES:
            raise InvalidInputError("Task discovery exceeds its one-MiB candidate allowance")
        output.append(copied)
    return sorted(output, key=lambda item: item["id"])


def policy_sha256() -> str:
    """Bind scoring, filtering, vocabulary and the exact existing type hierarchy."""
    return content_hash(
        {
            "schema": "potbot-task-discovery-policy/v1",
            "weights": _WEIGHTS,
            "classPhrases": CLASS_PHRASES,
            "typesSha256": catalog_fingerprint(),
            "inputVariance": "supplied_subtype_of_accepted",
            "outputVariance": "produced_subtype_of_requested",
            "unknownHardConstraints": "deny",
            "taskEvidence": "exact_current_task_only",
            "executionAuthority": False,
            "maxCandidates": MAX_TASK_CANDIDATES,
            "maxCandidateBytes": MAX_TASK_CANDIDATE_BYTES,
        }
    )


def _type_fit(candidate: dict[str, Any], selected: dict[str, Any]) -> str:
    graph = _ancestry()
    fits = []
    for key in ("inputTypes", "outputTypes"):
        requested = selected[key]
        if not requested:
            continue
        declared = candidate["capabilities"][key]
        for expected in requested:
            if expected in declared:
                fits.append("exact")
            elif any(
                (actual in graph[expected] if key == "inputTypes" else expected in graph[actual])
                for actual in declared
                if actual in graph
            ):
                fits.append("compatible")
            elif not declared or any(actual not in graph for actual in declared):
                fits.append("unknown")
            else:
                fits.append("incompatible")
    if not fits:
        return "not_requested"
    for outcome in ("incompatible", "unknown", "compatible"):
        if outcome in fits:
            return outcome
    return "exact"


def _hard_filter(
    candidate: dict[str, Any], selected: dict[str, Any], task_capabilities: set[str]
) -> tuple[list[str], str, str]:
    reasons = []
    capabilities = candidate["capabilities"]
    if not candidate["authorityCurrent"]:
        reasons.append("current_owner_authority_unavailable")
    required = candidate["requiredTaskCapability"]
    permission = (
        "unknown"
        if required is None
        else ("permitted" if required in task_capabilities else "denied")
    )
    if permission == "denied":
        reasons.append("task_capability_denied")
    if not set(selected["classes"]) <= set(capabilities["classes"]):
        reasons.append("required_class_not_declared")
    fit = _type_fit(candidate, selected)
    if fit in {"unknown", "incompatible"}:
        reasons.append("required_types_" + fit)
    if selected["platform"] and selected["platform"] not in capabilities["platforms"]:
        reasons.append("platform_unavailable_or_unknown")
    if selected["permittedEffects"] is not None and (
        not capabilities["effects"]
        or not set(capabilities["effects"]) <= set(selected["permittedEffects"])
    ):
        reasons.append("effects_denied_or_unknown")
    if selected["permittedScopes"] is not None and (
        "canonical_scopes_unknown" in candidate["reasonCodes"]
        or not set(capabilities["scopes"]) <= set(selected["permittedScopes"])
    ):
        reasons.append("scopes_denied_or_unknown")
    if selected["requireLocal"] and (
        not capabilities["platforms"] or "remote" in capabilities["platforms"]
    ):
        reasons.append("local_runtime_unproved")
    if selected["requireTaskEvidence"] and candidate["taskEvidence"]["status"] != "matching_pass":
        reasons.append("matching_task_evidence_required")
    return sorted(reasons), fit, permission


def _prepare(
    candidates: Iterable[dict[str, Any]],
    query: str,
    constraints: dict[str, Any] | None,
    task_capabilities: Iterable[str],
) -> tuple[list[dict[str, Any]], dict[str, Any], set[str], set[str], set[str]]:
    text_value(query, "Task discovery query", 500, empty=True)
    selected = normalize_constraints(constraints)
    capabilities = list(task_capabilities)
    task_set = set(_strings(capabilities, "Task capabilities", maximum=256))
    tokens = set(_tokens(query, limit=64))
    requested = set(selected["classes"]) or set(inferred_classes(query))
    return validate_task_candidates(candidates), selected, task_set, tokens, requested


def _relevance(candidate: dict[str, Any], tokens: set[str], requested: set[str]) -> tuple[int, int]:
    matched = requested & set(candidate["capabilities"]["classes"])
    declared = set(_tokens(candidate["description"]))
    common = tokens & declared
    lexical = (
        (
            _WEIGHTS["lexicalRecall"] * len(common) // max(1, len(tokens))
            + _WEIGHTS["lexicalPrecision"] * len(common) // max(1, len(declared))
        )
        if tokens
        else 0
    )
    classes = _WEIGHTS["classes"] * len(matched) // max(1, len(requested))
    return classes, lexical


def semantic_pool(
    candidates: Iterable[dict[str, Any]],
    query: str,
    constraints: dict[str, Any] | None,
    *,
    task_capabilities: Iterable[str],
    limit: int = 16,
) -> list[dict[str, Any]]:
    """Return at most sixteen hard-admitted observations for transient encoding.

    Lexical/class relevance orders the bounded pool but is not an admission gate;
    a paraphrase can therefore acquire positive semantic evidence. The pool does
    not include denied content or retain provider vectors in a shared corpus.
    """
    if type(limit) is not int or not 1 <= limit <= 16:
        raise InvalidInputError("Task discovery semantic pool limit must be 1 to 16")
    items, selected, task_set, tokens, requested = _prepare(
        candidates, query, constraints, task_capabilities
    )
    admitted = [
        item
        for item in items
        if item["description"] and not _hard_filter(item, selected, task_set)[0]
    ]
    admitted.sort(key=lambda item: (-sum(_relevance(item, tokens, requested)), item["id"]))
    return admitted[:limit]


def rank_task_candidates(
    candidates: Iterable[dict[str, Any]],
    query: str,
    constraints: dict[str, Any] | None,
    *,
    task_capabilities: Iterable[str],
    semantic_scores: Mapping[str, int] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Explain a shortlist with hard exclusions before any preference or similarity.

    ``semantic_scores`` is the trusted transient owner's nonnegative cosine
    score in millionths. Positive semantic evidence may support a paraphrase;
    installation and task evidence only prefer already relevant candidates.
    The internal limit is 256; the service owns the public ten-item page bound.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_TASK_CANDIDATES:
        raise InvalidInputError("Task discovery ranking limit must be 1 to 256")
    items, selected, task_set, tokens, requested = _prepare(
        candidates, query, constraints, task_capabilities
    )
    scores = {} if semantic_scores is None else semantic_scores
    if not isinstance(scores, Mapping) or len(scores) > 16:
        raise InvalidInputError("Task discovery semantic evidence must be a bounded mapping")
    identities = {item["id"] for item in items}
    for identity, score in scores.items():
        digest_value(identity, "Semantic candidate identity")
        if identity not in identities or type(score) is not int or not 0 <= score <= 1_000_000:
            raise InvalidInputError("Task discovery semantic score is invalid")
    output, excluded = [], []
    for candidate in items:
        denials, fit, permission = _hard_filter(candidate, selected, task_set)
        if denials:
            excluded.append({"id": candidate["id"], "reasonCodes": denials})
            continue
        classes, lexical = _relevance(candidate, tokens, requested)
        typed = (
            _WEIGHTS["exactTypes"]
            if fit == "exact"
            else (_WEIGHTS["compatibleTypes"] if fit == "compatible" else 0)
        )
        semantic = (_WEIGHTS["semantic"] * scores.get(candidate["id"], 0) + 999_999) // 1_000_000
        if not classes and not lexical and not typed and not semantic:
            excluded.append({"id": candidate["id"], "reasonCodes": ["no_supported_task_match"]})
            continue
        adequate = bool(classes or not requested)
        installed = _WEIGHTS["installed"] if candidate["installed"] and adequate else 0
        evidence = (
            _WEIGHTS["evidence"] if candidate["taskEvidence"]["status"] == "matching_pass" else 0
        )
        features = {
            "classes": classes,
            "lexical": lexical,
            "types": typed,
            "installed": installed,
            "evidence": evidence,
            "semantic": semantic,
        }
        reasons = list(candidate["reasonCodes"])
        if classes:
            reasons.extend(
                "class_match_" + value
                for value in sorted(requested & set(candidate["capabilities"]["classes"]))
            )
        if lexical:
            reasons.append("bounded_description_match")
        if typed:
            reasons.append("directional_types_" + fit)
        if semantic:
            reasons.append("bounded_transient_semantic_match")
        if installed:
            reasons.append("current_owner_installed_route_preferred")
        if evidence:
            reasons.append("exact_task_report_pass_preferred")
        if not candidate["capabilities"]["effects"]:
            reasons.append("effects_unknown")
        if not candidate["capabilities"]["classes"]:
            reasons.append("capability_classes_unknown")
        if permission == "unknown":
            reasons.append("task_permission_unknown")
        reasons.extend(["cost_latency_quality_unknown", "runtime_and_task_admission_required"])
        output.append(
            {
                **candidate,
                "score": sum(features.values()),
                "features": features,
                "typeFit": fit,
                "taskPermission": permission,
                "reasons": sorted(set(reasons)),
                "executable": False,
            }
        )
    output.sort(key=lambda item: (-item["score"], item["id"]))
    return {
        "items": output[:limit],
        "excluded": excluded,
        "terms": sorted(tokens),
        "inferredClasses": list(inferred_classes(query)),
        "policySha256": policy_sha256(),
        "noSuitableCandidate": not output,
    }
