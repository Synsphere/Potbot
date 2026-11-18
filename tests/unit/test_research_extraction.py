from __future__ import annotations

import copy
import hashlib
import json

import pytest

from potbot.domain.errors import StateMismatchError
from potbot.domain.serialization import canonical_json
from potbot.research.extraction import (
    extract_document,
    validate_retained_extraction,
    verify_extraction,
)


def assert_locations(document):
    value = validate_retained_extraction(document)
    raw = value["text"].encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == value["textSha256"]
    for segment in value["segments"]:
        assert raw[segment["startByte"] : segment["endByte"]].decode("utf-8")


def test_html_extracts_authored_unicode_table_and_code_without_executing_embedded_content() -> None:
    raw = """<!doctype html><html><head><title>Authored café</title>
    <meta property="article:published_time" content="2020-02-03T04:05:06Z">
    <script>fetch('https://private.example/secret')</script><style>body{display:none}</style>
    </head><body><h1>Fixture</h1><p>The café accepts 🔒 credentials.</p>
    <table><tr><th>Mode</th><th>Value</th></tr><tr><td>safe</td><td>10</td></tr></table>
    <pre>if ready:\n    run("literal")</pre><p>Ignore previous instructions is quoted source text.</p>
    <iframe src="https://private.example/secret"></iframe></body></html>""".encode()
    result = extract_document(raw, "text/html; charset=utf-8")
    assert result["complete"] and result["status"] == "extracted"
    assert result["sourceSha256"] == hashlib.sha256(raw).hexdigest()
    assert "café accepts 🔒" in result["text"] and '    run("literal")' in result["text"]
    assert "fetch(" not in result["text"] and "display:none" not in result["text"]
    assert "Ignore previous instructions" in result["text"]
    assert any("/td[" in segment["locator"] for segment in result["segments"])
    assert result["metadata"] == {
        "title": "Authored café",
        "publishedAt": "2020-02-03T04:05:06Z",
        "publicationProvenance": "source_reported",
    }
    assert result["execution"] is None and result["license"] == "unknown"
    assert_locations(result)
    assert verify_extraction(raw, result) == result


def test_json_extraction_retains_escaped_pointer_and_literal_value_types() -> None:
    raw = json.dumps(
        {"a/b": {"x~y": ["café 🔒", False, None, 17]}, "title": "Authored JSON"}, ensure_ascii=False
    ).encode()
    result = extract_document(raw, "application/problem+json")
    assert result["complete"]
    assert {segment["locator"] for segment in result["segments"]} == {
        "/a~1b/x~0y/0",
        "/a~1b/x~0y/1",
        "/a~1b/x~0y/2",
        "/a~1b/x~0y/3",
        "/title",
    }
    assert "/a~1b/x~0y/1 = false" in result["text"]
    assert "/a~1b/x~0y/2 = null" in result["text"]
    assert_locations(result)
    assert verify_extraction(raw, result) == result


def test_source_title_metadata_is_canonical_before_immutable_publication() -> None:
    raw = json.dumps({"title": "First\r\nSecond", "value": "authored"}).encode()
    result = extract_document(raw, "application/json")
    assert result["metadata"]["title"] == "First\nSecond"
    assert validate_retained_extraction(result) == result
    assert verify_extraction(raw, result) == result
    for raw, mime in (
        (b'{"title":"bad\\u0000title"}', "application/json"),
        (b"<html><head><title>bad\x00title</title></head><body>source</body></html>", "text/html"),
    ):
        invalid = extract_document(raw, mime)
        assert invalid["status"] == "invalid" and not invalid["complete"]
        validate_retained_extraction(invalid)


def test_plain_text_normalizes_only_line_endings_and_preserves_utf8_byte_offsets() -> None:
    raw = "αβ\r\ncafé 🔒\rfinal".encode()
    result = extract_document(raw, "text/plain; charset=UTF-8")
    assert result["text"] == "αβ\ncafé 🔒\nfinal"
    assert result["segments"] == [
        {
            "startByte": 0,
            "endByte": len(result["text"].encode()),
            "kind": "text",
            "locator": "document",
            "page": None,
        }
    ]
    assert_locations(result)
    assert verify_extraction(raw, result) == result


@pytest.mark.parametrize("date", ["2010-01-01", "2010-01-01T12:34:56", "yesterday"])
def test_unknown_publication_instant_never_becomes_the_current_retrieval_time(date) -> None:
    html = f'<html><head><meta property="article:published_time" content="{date}"></head><body><p>Old but still citable text.</p></body></html>'.encode()
    document = extract_document(html, "text/html")
    assert document["complete"] and "citable text" in document["text"]
    assert document["metadata"]["publishedAt"] is None
    assert document["metadata"]["publicationProvenance"] is None
    assert "retrievedAt" not in document
    assert_locations(document)


def test_aware_old_publication_date_remains_old_without_a_freshness_claim() -> None:
    result = extract_document(
        b'{"datePublished":"2010-01-01T00:00:00+02:00","text":"Older source"}', "application/json"
    )
    assert result["metadata"]["publishedAt"] == "2009-12-31T22:00:00Z"
    assert "fresh" not in result and "current" not in result
    assert_locations(result)


@pytest.mark.parametrize(
    "raw,mime,status,reason",
    [
        (
            b"PK\x03\x04archive",
            "application/zip",
            "unsupported",
            "archive_or_compressed_source_unsupported",
        ),
        (
            b"\x1f\x8bcompressed",
            "text/plain",
            "unsupported",
            "archive_or_compressed_source_unsupported",
        ),
        (b"a" * 262145, "text/plain", "over_limit", "source_byte_limit"),
        (b"\xff", "text/plain", "invalid", "source_extraction_invalid"),
        (b"hello\x00world", "text/plain", "invalid", "source_extraction_invalid"),
        (b'{"a":1,"a":2}', "application/json", "invalid", "source_extraction_invalid"),
        (b'{"a":NaN}', "application/json", "invalid", "source_extraction_invalid"),
        (
            b"readable",
            "text/plain; charset=iso-8859-1",
            "unsupported",
            "source_encoding_unsupported",
        ),
        (b"binary", "application/octet-stream", "unsupported", "source_media_type_unsupported"),
        (
            b"%PDF-1.7",
            "application/pdf",
            "extractor_unavailable",
            "isolated_pdf_extractor_unconfigured",
        ),
    ],
)
def test_unsupported_invalid_and_over_limit_sources_never_publish_partial_citable_text(
    raw, mime, status, reason
) -> None:
    result = extract_document(raw, mime)
    assert result["status"] == status and result["reasonCodes"] == [reason]
    assert not result["complete"] and result["text"] == "" and result["segments"] == []
    validate_retained_extraction(result)
    if mime != "application/pdf":
        assert verify_extraction(raw, result) == result


def test_deep_html_and_json_stop_at_parser_resource_bound() -> None:
    for raw, mime in [
        (b"<div>" * 65 + b"hidden", "text/html"),
        (b"[" * 65 + b"0" + b"]" * 65, "application/json"),
    ]:
        result = extract_document(raw, mime)
        assert result["status"] == "over_limit" and result["text"] == "" and not result["complete"]


def test_long_html_location_paths_cannot_amplify_a_small_source_past_public_artifact_bounds() -> (
    None
):
    tag = "x" + "n" * 79
    raw = (("<" + tag + ">") * 20 + "<p>x</p>" * 3500 + ("</" + tag + ">") * 20).encode()
    assert len(raw) == 31300
    result = extract_document(raw, "text/html")
    assert result["status"] == "over_limit" and result["reasonCodes"] == [
        "extraction_envelope_limit"
    ]
    assert result["text"] == "" and result["segments"] == [] and not result["complete"]
    assert len(canonical_json(result).encode()) < 1000000
    validate_retained_extraction(result)


def test_retained_validator_rejects_aggregate_location_amplification_even_with_valid_hashes() -> (
    None
):
    result = extract_document(b"<p>x</p>", "text/html")
    result["text"] = "\n".join("x" for _ in range(1100))
    result["textSha256"] = hashlib.sha256(result["text"].encode()).hexdigest()
    result["segments"] = [
        {
            "startByte": index * 2,
            "endByte": index * 2 + 1,
            "kind": "html_element",
            "locator": "/" + "x" * 1000,
            "page": None,
        }
        for index in range(1100)
    ]
    with pytest.raises(StateMismatchError, match="envelope"):
        validate_retained_extraction(result)


def test_a_self_consistent_fabricated_text_hash_still_fails_independent_source_replay() -> None:
    raw = b"The supported limit is 5."
    result = extract_document(raw, "text/plain")
    forged = copy.deepcopy(result)
    forged["text"] = forged["text"].replace("5", "9")
    forged["textSha256"] = hashlib.sha256(forged["text"].encode()).hexdigest()
    validate_retained_extraction(forged)
    with pytest.raises(StateMismatchError, match="do not match"):
        verify_extraction(raw, forged)


@pytest.mark.parametrize(
    "change",
    [
        "split_utf8",
        "gap",
        "incomplete",
        "false_execution",
        "unknown_license",
        "publication_without_provenance",
        "wrong_location_kind",
    ],
)
def test_retained_extraction_cannot_invent_spans_completeness_or_process_evidence(change) -> None:
    result = extract_document("café 🔒".encode(), "text/plain")
    if change == "split_utf8":
        result["segments"][0]["endByte"] = 4
    elif change == "gap":
        result["segments"][0]["startByte"] = 1
    elif change == "incomplete":
        result["complete"] = False
    elif change == "false_execution":
        result["execution"] = {"profileSha256": "a" * 64}
    elif change == "unknown_license":
        result["license"] = "public_domain"
    elif change == "wrong_location_kind":
        result["segments"][0].update(kind="pdf_page", locator="page:1", page=1)
    else:
        result["metadata"]["publishedAt"] = "2026-11-18T00:00:00Z"
    with pytest.raises(StateMismatchError):
        validate_retained_extraction(result)
