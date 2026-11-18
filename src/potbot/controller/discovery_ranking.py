"""Deterministic inspection shortlists with exclusions before scoring.

The reviewed class vocabulary is a retrieval aid, not another executable Type
registry. Only the existing operation owner can supply the installed preference.
Every returned result remains non-executable; invocation has its own admission.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from potbot.domain.discovery import (
    CAPABILITY_CLASSES,
    EFFECTS,
    PLATFORMS,
    RegistryCandidate,
    digest_value,
    text_value,
)
from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import type_identity

# Explicitly reviewed aliases. Repeated terms, candidate names, publishers,
# popularity and registration order do not participate in relevance scoring.
CLASS_PHRASES: dict[str, tuple[str, ...]] = {
    "repository_inspection": (
        "commit",
        "repository history",
        "git diff",
        "changed files",
        "revision history",
    ),
    "code_search": (
        "function definition",
        "call site",
        "code search",
        "symbol",
        "find implementation",
    ),
    "file_inspection": (
        "file contents",
        "read file",
        "inspect file",
        "directory listing",
        "list files",
    ),
    "text_search": ("search text", "text occurrences", "literal phrase", "find phrase", "grep"),
    "image_inspection": (
        "screenshot",
        "inspect image",
        "image contents",
        "picture",
        "visual inspection",
    ),
    "document_analysis": ("pdf", "document", "report paragraphs", "word file", "extract pages"),
    "spreadsheet_analysis": ("spreadsheet", "workbook", "excel", "csv", "table formulas"),
    "browser_inspection": ("web page", "browser", "dom", "page elements", "website layout"),
    "web_retrieval": (
        "web search",
        "online sources",
        "retrieve web",
        "internet research",
        "public article",
    ),
    "structured_api_read": (
        "api response",
        "rest endpoint",
        "graphql",
        "structured api",
        "service endpoint",
    ),
    "database_query": ("sql", "database", "query rows", "relational table", "read records"),
    "satisfiability": (
        "satisfiability",
        "sat",
        "constraints satisfiable",
        "logical constraints",
        "smt",
    ),
    "python_debugging": (
        "python breakpoint",
        "python stack",
        "debug python",
        "python debugger",
        "python variables",
    ),
    "remote_debugging": (
        "remote process",
        "remote breakpoint",
        "remote debugger",
        "remote python",
        "gdbserver",
    ),
    "hardware_debugging": (
        "hardware breakpoint",
        "board probe",
        "jtag",
        "swd",
        "firmware debugging",
    ),
    "android_inspection": (
        "android",
        "adb",
        "mobile emulator",
        "android screen",
        "android application",
    ),
    "apple_inspection": ("ios", "iphone", "apple host", "macos application", "xcode"),
    "profiling": (
        "cpu profile",
        "profiling",
        "memory allocation",
        "performance trace",
        "bottleneck",
    ),
    "skill_authoring": ("write skill", "author skill", "skill md", "skill package", "create skill"),
    "workflow_guidance": (
        "workflow",
        "procedure",
        "step by step",
        "repeatable process",
        "checklist",
    ),
}
_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "that",
        "the",
        "this",
        "to",
        "with",
        "would",
    }
)


def _tokens(value: str, *, limit: int = 512) -> tuple[str, ...]:
    values = re.findall(
        r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold(), flags=re.UNICODE
    )
    return tuple(
        sorted({value for value in values if value not in _STOP and len(value) <= 64})[:limit]
    )


def inferred_classes(query: str) -> tuple[str, ...]:
    normalized = (
        " "
        + " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", query).casefold()))
        + " "
    )
    return tuple(
        sorted(
            class_name
            for class_name, phrases in CLASS_PHRASES.items()
            if any(" " + phrase + " " in normalized for phrase in phrases)
        )
    )


def _constraints(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = {} if raw is None else raw
    fields = {
        "classes",
        "inputTypes",
        "outputTypes",
        "platform",
        "permittedEffects",
        "permittedScopes",
        "requireLocal",
    }
    if not isinstance(raw, dict) or set(raw) - fields:
        raise InvalidInputError("Discovery constraints contain unsupported fields")
    result: dict[str, Any] = {}
    for key in ("classes", "inputTypes", "outputTypes", "permittedEffects", "permittedScopes"):
        values = raw.get(key, [])
        if (
            not isinstance(values, list)
            or len(values) > 64
            or any(not isinstance(value, str) for value in values)
            or len(values) != len(set(values))
        ):
            raise InvalidInputError("Discovery constraint lists must be bounded and unique")
        for value in values:
            text_value(value, "Discovery constraint", 256)
        result[key] = values
    if (
        not set(result["classes"]) <= CAPABILITY_CLASSES
        or not set(result["permittedEffects"]) <= EFFECTS
    ):
        raise InvalidInputError("Discovery constraints use an unpublished classification")
    for key in ("inputTypes", "outputTypes"):
        result[key] = [type_identity(value) for value in result[key]]
    platform = raw.get("platform")
    if platform is not None and platform not in PLATFORMS:
        raise InvalidInputError("Discovery platform constraint is unsupported")
    result["platform"] = platform
    local = raw.get("requireLocal", False)
    if type(local) is not bool:
        raise InvalidInputError("Discovery local-runtime constraint must be boolean")
    result["requireLocal"] = local
    result["effectsSpecified"] = "permittedEffects" in raw
    result["scopesSpecified"] = "permittedScopes" in raw
    return result


def rank_candidates(
    candidates: Iterable[RegistryCandidate],
    query: str,
    *,
    constraints: dict[str, Any] | None = None,
    installed_sha256: Iterable[str] = (),
    denied_sha256: Iterable[str] = (),
    limit: int = 10,
) -> dict[str, Any]:
    text_value(query, "Discovery query", 500, empty=True)
    if type(limit) is not int or not 1 <= limit <= 4096:
        raise InvalidInputError("Discovery ranking limit must be 1 to 4096")
    items = tuple(candidates)
    if len(items) > 4096 or any(not isinstance(item, RegistryCandidate) for item in items):
        raise InvalidInputError("Discovery ranking requires at most 4096 validated observations")
    hashes = [item.sha256 for item in items]
    if len(hashes) != len(set(hashes)):
        raise InvalidInputError("Discovery ranking requires unique candidate observations")
    installed, denied = set(installed_sha256), set(denied_sha256)
    if len(installed) > 4096 or len(denied) > 4096:
        raise InvalidInputError("Discovery owner observations exceed allowance")
    for digest in installed | denied:
        digest_value(digest)
    selected = _constraints(constraints)
    tokens = set(_tokens(query, limit=64))
    requested_classes = set(selected["classes"]) or set(inferred_classes(query))
    output: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for candidate in items:
        digest = candidate.sha256
        capability = candidate.capabilities
        reasons = []
        if digest in denied:
            reasons.append("current_owner_policy_denied")
        if candidate.status in {"deleted", "deprecated", "unsupported"}:
            reasons.append("source_" + candidate.status)
        if selected["classes"] and not set(selected["classes"]) <= set(capability["classes"]):
            reasons.append("required_class_not_declared")
        for key in ("inputTypes", "outputTypes"):
            if not set(selected[key]) <= {type_identity(value) for value in capability[key]}:
                reasons.append(
                    "required_"
                    + ("input_type" if key == "inputTypes" else "output_type")
                    + "_not_declared"
                )
        if selected["platform"] and selected["platform"] not in capability["platforms"]:
            reasons.append("platform_unavailable_or_unknown")
        if selected["effectsSpecified"] and (
            not capability["effects"]
            or not set(capability["effects"]) <= set(selected["permittedEffects"])
        ):
            reasons.append("effects_denied_or_unknown")
        if selected["scopesSpecified"] and not set(capability["scopes"]) <= set(
            selected["permittedScopes"]
        ):
            reasons.append("scopes_denied")
        if selected["requireLocal"] and (
            not capability["platforms"] or "remote" in capability["platforms"]
        ):
            reasons.append("local_runtime_unproved")
        if reasons:
            excluded.append({"candidateSha256": digest, "reasons": reasons})
            continue
        matched = requested_classes & set(capability["classes"])
        declared = set(_tokens(candidate.description))
        common = tokens & declared
        # Bounded unique token recall and precision: repeating a word provides
        # no gain, and adding irrelevant text cannot improve precision.
        lexical = (
            (200 * len(common) // max(1, len(tokens)))
            + (100 * len(common) // max(1, len(declared)))
            if tokens
            else 0
        )
        typed = 1000 * len(matched) // max(1, len(requested_classes))
        if query and not matched and not common:
            excluded.append({"candidateSha256": digest, "reasons": ["no_supported_task_match"]})
            continue
        score = typed + lexical
        reasons = []
        if matched:
            reasons.extend("class_match:" + value for value in sorted(matched))
        if lexical:
            reasons.append("bounded_description_match")
        if (
            digest in installed
            and (matched or not requested_classes)
            and (not query or common or matched)
        ):
            score += 2000
            reasons.append("current_owner_installed_route_preferred")
        if candidate.version is None:
            reasons.append("exact_version_unresolved")
        if not capability["effects"]:
            reasons.append("effects_unknown")
        if not capability["classes"]:
            reasons.append("typed_capability_unknown")
        reasons.extend(["cost_latency_quality_unknown", "runtime_and_task_admission_required"])
        output.append(
            {"candidateSha256": digest, "score": score, "reasons": reasons, "executable": False}
        )
    # Ties are explicitly equal scores; a content hash is only a stable display
    # tie-breaker, not evidence that one tied candidate is more suitable.
    output.sort(key=lambda item: (-item["score"], item["candidateSha256"]))
    excluded.sort(key=lambda item: item["candidateSha256"])
    return {
        "schema": "potbot-discovery-ranking/v1",
        "query": query,
        "terms": sorted(tokens),
        "items": output[:limit],
        "excluded": excluded,
        "noSuitableCandidate": not output,
    }
