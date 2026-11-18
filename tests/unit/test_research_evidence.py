"""Exact Unicode source spans, provenance and conservative literal comparison."""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.research_evidence import (
    build_citation,
    citation_spec,
    claim_specs,
    compare_claims,
)
from potbot.domain.serialization import content_hash
from potbot.research.contracts import ResearchScope
from potbot.research.extraction import extract_document


def source_fixture(text="Heading\nα🍀 value 42\n", *, freshness=0, published=None):
    raw = text.encode()
    extracted = extract_document(raw, "text/plain")
    extracted["metadata"]["publishedAt"] = published
    extracted["metadata"]["publicationProvenance"] = "source_reported" if published else None
    source_id = hashlib.sha256(b"source" + raw).hexdigest()
    source = {
        "id": source_id,
        "sourceId": source_id,
        "requestedUrl": "https://docs.example.com/guide",
        "resolvedUrl": "https://docs.example.com/guide",
        "retrievedAt": "2026-11-18T12:00:00+00:00",
        "rawSha256": hashlib.sha256(raw).hexdigest(),
    }
    scope = ResearchScope(*(uuid4() for _ in range(6)), "a" * 64)
    start = raw.index("α".encode()) if "α" in text else 0
    end = len(raw.rstrip(b"\n"))
    spec = {
        "runId": str(uuid4()),
        "sourceId": source_id,
        "startByte": start,
        "endByte": end,
        "quoteSha256": hashlib.sha256(raw[start:end]).hexdigest(),
    }

    def build(chosen=None):
        return build_citation(
            spec if chosen is None else chosen,
            source,
            extracted,
            research_scope=scope,
            receipt_sha256="b" * 64,
            freshness_seconds=freshness,
        )

    return spec, source, extracted, build


def test_exact_multibyte_citation_retains_source_location_and_hash():
    spec, source, extracted, build = source_fixture()
    citation = build()
    assert citation["quote"] == "α🍀 value 42"
    assert citation["startByte"] == len(b"Heading\n")
    assert citation["endByte"] - citation["startByte"] == len(citation["quote"].encode())
    assert citation["lineStart"] == citation["lineEnd"] == 2
    assert citation["textSha256"] == extracted["textSha256"]
    assert citation["rawSha256"] == source["rawSha256"]
    assert citation["quoteSha256"] == spec["quoteSha256"]
    assert citation["sha256"] == content_hash({k: v for k, v in citation.items() if k != "sha256"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("startByte", True),
        ("startByte", -1),
        ("endByte", False),
        ("endByte", 4 * 1024 * 1024 + 1),
        ("runId", "not-a-uuid"),
        ("sourceId", "A" * 64),
        ("quoteSha256", "short"),
    ],
)
def test_citation_spec_rejects_ambiguous_identity_or_range(field, value):
    spec, _, _, _ = source_fixture()
    spec[field] = value
    with pytest.raises(InvalidInputError):
        citation_spec(spec)


@pytest.mark.parametrize("start_delta,end_delta", [(1, 0), (0, -10), (0, 100)])
def test_citation_rejects_partial_codepoints_and_outside_source(start_delta, end_delta):
    spec, _, _, build = source_fixture()
    spec["startByte"] += start_delta
    spec["endByte"] += end_delta
    with pytest.raises((InvalidInputError, StateMismatchError)):
        build(spec)


@pytest.mark.parametrize(
    "change",
    [
        "quote_hash",
        "text_hash",
        "source_hash",
        "wrong_source",
        "incomplete",
        "unsupported",
        "changed_text",
        "crlf",
    ],
)
def test_incomplete_or_modified_source_cannot_produce_a_citation(change):
    spec, source, extracted, build = source_fixture()
    if change == "quote_hash":
        spec["quoteSha256"] = "c" * 64
    elif change == "text_hash":
        extracted["textSha256"] = "c" * 64
    elif change == "source_hash":
        extracted["sourceSha256"] = "c" * 64
    elif change == "wrong_source":
        source["sourceId"] = "c" * 64
    elif change == "incomplete":
        extracted["complete"] = False
    elif change == "unsupported":
        extracted["status"] = "unsupported"
    elif change == "changed_text":
        extracted["text"] = "another actual source"
    else:
        extracted["text"] += "\r\n"
    with pytest.raises((InvalidInputError, StateMismatchError)):
        build(spec)


def test_quote_bound_and_unknown_fields_are_not_silently_ignored():
    spec, _, _, _ = source_fixture()
    with pytest.raises(InvalidInputError):
        citation_spec({**spec, "quote": "caller supplied"})
    with pytest.raises(InvalidInputError):
        citation_spec({**spec, "endByte": spec["startByte"]})
    with pytest.raises(InvalidInputError):
        citation_spec({**spec, "endByte": spec["startByte"] + 16385})


@pytest.mark.parametrize("kind", ["html", "pdf"])
def test_synthesized_block_or_page_separator_has_no_citable_source_location(tmp_path, kind):
    spec, source, extracted, build = source_fixture("first\nsecond")
    if kind == "html":
        actual = extract_document(b"<p>first</p><p>second</p>", "text/html")
    else:
        # The supervised fixture tests location assembly, not live PDF isolation.
        from .test_research_pdf_extraction import RAW, fixture_extractor

        parser, _ = fixture_extractor(tmp_path)
        actual = extract_document(RAW, "application/pdf", pdf_extractor=parser)
    source["rawSha256"] = actual["sourceSha256"]
    extracted.clear()
    extracted.update(actual)
    start, end = actual["segments"][0]["endByte"], actual["segments"][1]["startByte"]
    assert end > start
    gap = actual["text"].encode("utf-8")[start:end]
    assert not gap.strip()
    with pytest.raises(InvalidInputError, match="authored source location"):
        build(
            {
                **spec,
                "startByte": start,
                "endByte": end,
                "quoteSha256": hashlib.sha256(gap).hexdigest(),
            }
        )


def claim(specs):
    return [
        {"key": "literal-value", "label": "Operator-selected reported value", "citations": specs}
    ]


def test_literal_agreement_keeps_distinct_source_citations():
    first, _, _, build_first = source_fixture("42")
    second, _, _, build_second = source_fixture("42")
    citations = {content_hash(first): build_first(), content_hash(second): build_second()}
    result = compare_claims(
        claim([first, second]), citations, {}, evaluated_at=datetime(2026, 11, 18, tzinfo=UTC)
    )
    assert result[0]["status"] == "supported"
    assert result[0]["values"] == ["42"]
    assert len(result[0]["citations"]) == 2


def test_planted_conflicting_claims_remain_unresolved():
    first, _, _, build_first = source_fixture("42")
    second, _, _, build_second = source_fixture("43")
    result = compare_claims(
        claim([first, second]),
        {
            content_hash(first): build_first(),
            content_hash(second): build_second(),
        },
        {},
        evaluated_at=datetime(2026, 11, 18, tzinfo=UTC),
    )
    assert result[0]["status"] == "unresolved"
    assert result[0]["values"] == ["42", "43"]
    assert result[0]["reasonCodes"] == ["conflicting_cited_values"]


@pytest.mark.parametrize(
    "published,reason",
    [
        (None, "publication_time_unknown"),
        ("2020-01-01T00:00:00+00:00", "publication_time_stale"),
        ("2026-11-19T00:00:00+00:00", "publication_time_in_future"),
    ],
)
def test_recent_retrieval_never_substitutes_for_publication_freshness(published, reason):
    spec, _, _, build = source_fixture("42", freshness=86400, published=published)
    citation = build()
    result = compare_claims(
        claim([spec]),
        {content_hash(spec): citation},
        {},
        evaluated_at=datetime(2026, 11, 18, 12, tzinfo=UTC),
    )
    assert result[0]["status"] == "unresolved"
    assert reason in result[0]["reasonCodes"]


def test_known_publication_in_declared_window_can_support_literal_agreement():
    spec, _, _, build = source_fixture("42", freshness=86400, published="2026-11-18T10:00:00+00:00")
    result = compare_claims(
        claim([spec]),
        {content_hash(spec): build()},
        {},
        evaluated_at=datetime(2026, 11, 18, 12, tzinfo=UTC),
    )
    assert result[0]["status"] == "supported"


def test_unavailable_evidence_does_not_disappear_from_comparison():
    spec, _, _, build = source_fixture("42")
    missing, _, _, _ = source_fixture("42")
    result = compare_claims(
        claim([spec, missing]),
        {content_hash(spec): build()},
        {content_hash(missing): "citation_unavailable"},
        evaluated_at=datetime(2026, 11, 18, tzinfo=UTC),
    )
    assert result[0]["status"] == "unresolved"
    assert result[0]["unavailableCitationCount"] == 1
    absent = compare_claims(
        claim([missing]), {}, {}, evaluated_at=datetime(2026, 11, 18, tzinfo=UTC)
    )
    assert absent[0]["status"] == "unsupported"


def test_comparison_rejects_changed_verified_citation_identity():
    spec, _, _, build = source_fixture("42")
    citation = build()
    citation["quote"] = "43"
    with pytest.raises(StateMismatchError):
        compare_claims(
            claim([spec]),
            {content_hash(spec): citation},
            {},
            evaluated_at=datetime(2026, 11, 18, tzinfo=UTC),
        )


def test_claim_count_duplicate_and_total_bounds():
    spec, _, _, _ = source_fixture()
    with pytest.raises(InvalidInputError):
        claim_specs(claim([spec, spec]))
    with pytest.raises(InvalidInputError):
        claim_specs(claim([spec]) * 2)
    with pytest.raises(InvalidInputError):
        claim_specs([])
    many = []
    for index in range(3):
        entries = [{**spec, "runId": str(uuid4())} for _ in range(16)]
        many.append({"key": f"claim-{index}", "label": "bounded", "citations": entries})
    with pytest.raises(InvalidInputError):
        claim_specs(many)
    bad = copy.deepcopy(claim([spec]))
    bad[0]["label"] = "🪴" * 129
    with pytest.raises(InvalidInputError):
        claim_specs(bad)
