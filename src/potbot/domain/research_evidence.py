"""Literal, bounded citations and source-agreement reports.

The caller authenticates the research execution and immutable source artifacts.
These functions derive exact byte spans and compare cited values; they do not
infer that a quoted source is truthful or that an operator label is entailed.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.result_assertions import bounded_value
from potbot.domain.serialization import content_hash
from potbot.research.contracts import ResearchScope

CITATION_SCHEMA = "potbot-research-citation/v1"
COMPARISON_SCHEMA = "potbot-research-comparison/v1"
MAX_QUOTE_BYTES = 16_384
MAX_CLAIMS = 16
MAX_CITATIONS = 32
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z")


def digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("Research evidence requires a lowercase SHA-256")
    return value


def identity(value: object) -> UUID:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or str(parsed) != value:
        raise InvalidInputError("Research evidence requires a canonical UUID")
    return parsed


def timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 80:
        raise InvalidInputError("Research evidence requires an explicit timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidInputError("Research evidence timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidInputError("Research evidence timestamp requires a timezone")
    return parsed


def citation_spec(value: object) -> dict[str, Any]:
    checked = bounded_value(value, maximum=2048)
    if not isinstance(checked, dict) or set(checked) != {
        "runId",
        "sourceId",
        "startByte",
        "endByte",
        "quoteSha256",
    }:
        raise InvalidInputError("Citation fields are invalid")
    identity(checked["runId"])
    digest(checked["sourceId"])
    digest(checked["quoteSha256"])
    start, end = checked["startByte"], checked["endByte"]
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= 4 * 1024 * 1024
        or end - start > MAX_QUOTE_BYTES
    ):
        raise InvalidInputError("Citation requires a bounded nonempty UTF-8 byte range")
    return dict(checked)


def claim_specs(value: object) -> list[dict[str, Any]]:
    checked = bounded_value(value, maximum=65_536)
    if not isinstance(checked, list) or not 1 <= len(checked) <= MAX_CLAIMS:
        raise InvalidInputError("A comparison requires 1 to 16 claims")
    keys: set[str] = set()
    total = 0
    for item in checked:
        if not isinstance(item, dict) or set(item) != {"key", "label", "citations"}:
            raise InvalidInputError("Research claim fields are invalid")
        key, label, citations = item["key"], item["label"], item["citations"]
        if not isinstance(key, str) or not _KEY.fullmatch(key) or key in keys:
            raise InvalidInputError("Research claim keys must be distinct literal identifiers")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label.encode("utf-8")) > 512
            or any(ord(char) < 32 for char in label)
        ):
            raise InvalidInputError("Research claim labels must be bounded literal text")
        if not isinstance(citations, list) or not 1 <= len(citations) <= 16:
            raise InvalidInputError("A claim requires 1 to 16 citation specifications")
        specs = [citation_spec(spec) for spec in citations]
        if len({content_hash(spec) for spec in specs}) != len(specs):
            raise InvalidInputError("A claim cannot repeat the same citation")
        total += len(specs)
        keys.add(key)
    if total > MAX_CITATIONS:
        raise InvalidInputError("A comparison exceeds its citation allowance")
    return list(checked)


def build_citation(
    spec: object,
    source: dict[str, Any],
    extraction: dict[str, Any],
    *,
    research_scope: ResearchScope,
    receipt_sha256: str,
    freshness_seconds: int,
) -> dict[str, Any]:
    """Derive a codepoint-safe quote from an independently authenticated source."""
    selected = citation_spec(spec)
    digest(receipt_sha256)
    if (
        not isinstance(research_scope, ResearchScope)
        or type(freshness_seconds) is not int
        or not 0 <= freshness_seconds <= 30 * 86400
        or source.get("id") != selected["sourceId"]
        or source.get("sourceId") != selected["sourceId"]
        or extraction.get("schema") != "potbot-extracted-source/v1"
        or extraction.get("status") != "extracted"
        or extraction.get("complete") is not True
        or extraction.get("sourceSha256") != source.get("rawSha256")
        or not isinstance(extraction.get("text"), str)
        or "\r" in extraction["text"]
        or extraction.get("license") != "unknown"
    ):
        raise StateMismatchError("Citation source is incomplete or does not match its identity")
    raw_digest = digest(source.get("rawSha256"))
    text_digest = digest(extraction.get("textSha256"))
    try:
        raw = extraction["text"].encode("utf-8", "strict")
        start, end = selected["startByte"], selected["endByte"]
        if end > len(raw):
            raise ValueError
        # Decode the prefix as well: a valid selected suffix must not hide an
        # offset that began in the middle of an earlier multibyte character.
        raw[:start].decode("utf-8", "strict")
        quote = raw[start:end].decode("utf-8", "strict")
    except (UnicodeError, ValueError):
        raise InvalidInputError("Citation range is not an exact UTF-8 text span") from None
    if hashlib.sha256(raw).hexdigest() != text_digest:
        raise StateMismatchError("Extracted source text does not match its retained digest")
    if hashlib.sha256(raw[start:end]).hexdigest() != selected["quoteSha256"]:
        raise StateMismatchError("Citation quote differs from the requested exact bytes")
    metadata = extraction.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {
        "title",
        "publishedAt",
        "publicationProvenance",
    }:
        raise StateMismatchError("Extracted publication metadata is invalid")
    published = metadata["publishedAt"]
    if published is not None:
        timestamp(published)
        if metadata["publicationProvenance"] != "source_reported":
            raise StateMismatchError("Publication time lacks its source-reported provenance")
    elif metadata["publicationProvenance"] is not None:
        raise StateMismatchError("Unknown publication time cannot have reported provenance")
    timestamp(source.get("retrievedAt"))
    locations = extraction.get("segments")
    if not isinstance(locations, list) or len(locations) > 8192:
        raise StateMismatchError("Extracted source locations exceed their allowance")
    segments = []
    for item in locations:
        if (
            not isinstance(item, dict)
            or set(item) != {"startByte", "endByte", "kind", "locator", "page"}
            or type(item["startByte"]) is not int
            or type(item["endByte"]) is not int
            or not 0 <= item["startByte"] <= item["endByte"] <= len(raw)
            or item["kind"] not in {"text", "html_element", "json_pointer", "pdf_page"}
            or not isinstance(item["locator"], str)
            or len(item["locator"].encode("utf-8")) > 2048
            or (
                item["page"] is not None
                and (type(item["page"]) is not int or not 1 <= item["page"] <= 10_000)
            )
        ):
            raise StateMismatchError("Extracted source has an invalid location")
        if item["startByte"] < end and item["endByte"] > start:
            segments.append(dict(item))
    if not segments:
        raise InvalidInputError("Citation must intersect an authored source location")
    if len(segments) > 64:
        raise InvalidInputError("Choose a citation covering at most 64 source locations")
    citation = {
        "schema": CITATION_SCHEMA,
        **selected,
        "rawSha256": raw_digest,
        "textSha256": text_digest,
        "receiptSha256": receipt_sha256,
        "quote": quote,
        "requestedUrl": source["requestedUrl"],
        "resolvedUrl": source["resolvedUrl"],
        "retrievedAt": source["retrievedAt"],
        "publishedAt": published,
        "publicationProvenance": metadata["publicationProvenance"],
        "license": "unknown",
        "lineStart": raw[:start].count(b"\n") + 1,
        "lineEnd": raw[: end - 1].count(b"\n") + 1,
        "segments": segments,
        "researchScope": research_scope.to_dict(),
        "freshnessSeconds": freshness_seconds,
    }
    return {**citation, "sha256": content_hash(citation)}


def compare_claims(
    specs: object,
    citations: dict[str, dict[str, Any]],
    unavailable: dict[str, str],
    *,
    evaluated_at: datetime,
) -> list[dict[str, Any]]:
    """Compare literal quoted values without interpreting operator labels."""
    requested = claim_specs(specs)
    if not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is None:
        raise InvalidInputError("Comparison requires a timezone-aware evaluation time")
    results = []
    for item in requested:
        selected = []
        reasons: set[str] = set()
        missing = 0
        for spec in item["citations"]:
            key = content_hash(spec)
            citation = citations.get(key)
            if key in unavailable or citation is None:
                missing += 1
                reasons.add(unavailable.get(key, "citation_unavailable"))
                continue
            if citation.get("sha256") != content_hash(
                {name: value for name, value in citation.items() if name != "sha256"}
            ):
                raise StateMismatchError("Verified citation identity changed before comparison")
            selected.append(citation)
            freshness = citation["freshnessSeconds"]
            if freshness:
                published = citation["publishedAt"]
                if published is None:
                    reasons.add("publication_time_unknown")
                else:
                    age = (evaluated_at - timestamp(published)).total_seconds()
                    if age < 0:
                        reasons.add("publication_time_in_future")
                    elif age > freshness:
                        reasons.add("publication_time_stale")
        values = list(dict.fromkeys(citation["quote"] for citation in selected))
        if len(values) > 1:
            reasons.add("conflicting_cited_values")
        status = "unsupported" if not selected else "unresolved" if reasons else "supported"
        results.append(
            {
                "key": item["key"],
                "label": item["label"],
                "status": status,
                "citations": selected,
                "values": values,
                "reasonCodes": sorted(reasons),
                "unavailableCitationCount": missing,
            }
        )
    return results
