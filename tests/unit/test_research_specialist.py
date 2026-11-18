from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.research.specialist import (
    SPECIALIST_ENDPOINTS,
    SPECIALIST_MIN_INTERVAL_MS,
    merge_specialist_results,
    normalize_doi,
    normalize_specialist_results,
    pubmed_summary_body,
    specialist_descriptor,
    specialist_search_body,
    specialist_wire_request,
    verify_pubmed_summary,
)
from potbot.research.specialist_records import (
    MAX_NORMALIZED_SPECIALIST_BYTES,
    same_work,
)
from potbot.tools.pinned_https import PinnedHttpsTransport

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DOI = "10.1234/test.paper"
TITLE = "A controlled research example"


def encoded(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def crossref_record(**changes: object) -> dict[str, object]:
    return {
        "DOI": DOI,
        "title": [TITLE],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "issued": {"date-parts": [[1843, 9]]},
        "event": {"start": {"date-parts": [[1842, 7, 12]]}},
        "deposited": {"date-time": "2026-11-01T12:00:00Z"},
        **changes,
    }


def normalize(provider: str, value: object, limit: int = 8) -> dict[str, object]:
    return normalize_specialist_results(
        provider,
        value if isinstance(value, bytes) else encoded(value),
        retrieved_at=NOW,
        max_results=limit,
    )


def crossref_batch(**changes: object) -> dict[str, object]:
    return normalize(
        "crossref", {"status": "ok", "message": {"items": [crossref_record(**changes)]}}
    )


def openalex_batch(**changes: object) -> dict[str, object]:
    return normalize(
        "openalex",
        {
            "results": [
                {
                    "id": "https://openalex.org/W1234",
                    "doi": "https://doi.org/" + DOI,
                    "title": TITLE,
                    "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
                    "publication_date": "1843-09-01",
                    "is_retracted": False,
                    **changes,
                }
            ]
        },
    )


def first(batch: dict[str, object]) -> dict[str, object]:
    assert isinstance(batch["items"], list)
    assert len(batch["items"]) == 1, batch
    return batch["items"][0]


@pytest.mark.parametrize(
    "value",
    [
        "10.1234/TEST.Paper",
        "doi:10.1234/TEST.Paper",
        "https://doi.org/10.1234/TEST.Paper",
        "http://dx.doi.org/10.1234%2FTEST.Paper",
    ],
)
def test_doi_normalizes_known_identifier_forms_without_guessing(value: str) -> None:
    assert normalize_doi(value) == DOI


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "test.paper",
        "10.1/paper",
        "10.1234/has space",
        "https://evil.example/10.1234/paper",
        "https://doi.org@evil.example/10.1234/paper",
        "https://doi.org/10.1234/paper?token=x",
        "https://doi.org/10.1234/paper#section",
        "10.1234/x\x00",
        "10.1234/x\\y",
        "10.1234/" + "a" * 240,
    ],
)
def test_doi_rejects_ambiguous_unbounded_and_credential_urls(value: object) -> None:
    with pytest.raises(InvalidInputError):
        normalize_doi(value)


@pytest.mark.parametrize("provider", list(SPECIALIST_ENDPOINTS))
def test_every_provider_has_fixed_bounded_read_only_wire_contract(provider: str) -> None:
    descriptor = specialist_descriptor(provider)
    assert set(descriptor) == {"id", "method", "endpoint", "adapterRevision", "mode", "scope"}
    assert descriptor["mode"] == "specialist_retrieval"
    body = specialist_search_body(provider, "Quantum computing", 3)
    method, url, content, headers = specialist_wire_request(provider, body)
    assert method == "GET" and content == b""
    assert url.startswith(SPECIALIST_ENDPOINTS[provider] + "?")
    assert "Accept" in headers
    assert urlsplit(url).hostname == urlsplit(SPECIALIST_ENDPOINTS[provider]).hostname
    assert SPECIALIST_MIN_INTERVAL_MS[provider] >= 400
    # The real shared route owner must accept exactly the generated request.
    pinned = PinnedHttpsTransport(method, url, allow_query=True)
    assert pinned.url == url
    import asyncio

    asyncio.run(pinned.aclose())


@pytest.mark.parametrize("provider", list(SPECIALIST_ENDPOINTS))
def test_doi_queries_retain_explicit_identifier_contract(provider: str) -> None:
    body = specialist_search_body(provider, "doi:10.1234/TEST.Paper", 3)
    assert json.loads(body)["operation"] == "doi"
    assert json.loads(body)["query"] == DOI
    method, url, _, _ = specialist_wire_request(provider, body)
    assert method == "GET"
    if provider == "crossref":
        assert url.endswith("/10.1234%2Ftest.paper")
    elif provider == "openalex":
        assert parse_qs(urlsplit(url).query)["filter"] == ["doi:" + DOI]
    elif provider == "semantic_scholar":
        assert "/paper/DOI:10.1234%2Ftest.paper?" in url


def test_arxiv_versioned_lookup_and_terms_floor() -> None:
    body = specialist_search_body("arxiv", "arxiv:2301.01234v2", 2)
    _, url, _, _ = specialist_wire_request("arxiv", body)
    assert parse_qs(urlsplit(url).query)["id_list"] == ["2301.01234v2"]
    assert SPECIALIST_MIN_INTERVAL_MS["arxiv"] == 3000


def test_query_preflight_honors_actual_encoded_transport_route_limit() -> None:
    with pytest.raises(InvalidInputError, match="route bound"):
        specialist_search_body("wikidata", "界" * 600, 3)
    body = specialist_search_body("crossref", "https://dx.doi.org/10.1234/TEST.Paper", 3)
    assert json.loads(body)["operation"] == "doi"


@pytest.mark.parametrize(
    "provider,header", [("openalex", "Authorization"), ("semantic_scholar", "x-api-key")]
)
def test_optional_credentials_enter_headers_and_never_urls(provider: str, header: str) -> None:
    secret = "private-test-key-123"
    body = specialist_search_body(provider, "test", 2)
    _, url, content, headers = specialist_wire_request(provider, body, secret)
    assert secret in headers[header]
    assert secret not in url and secret.encode() not in content and secret.encode() not in body


def test_no_arbitrary_route_or_raw_sparql_parameter_can_be_smuggled() -> None:
    body = json.loads(specialist_search_body("crossref", "test", 2))
    body["url"] = "https://evil.example"
    with pytest.raises(InvalidInputError):
        specialist_wire_request("crossref", encoded(body))
    attack = 'x" . } SERVICE <https://evil.example/> { ?x ?y ?z } #'
    _, url, _, _ = specialist_wire_request(
        "wikidata", specialist_search_body("wikidata", attack, 2)
    )
    sparql = parse_qs(urlsplit(url).query)["query"][0]
    assert json.dumps(attack, ensure_ascii=False) + "@en" in sparql
    assert sparql.endswith("LIMIT 2")
    with pytest.raises(InvalidInputError):
        specialist_wire_request("crossref", specialist_search_body("openalex", "test", 2))


@pytest.mark.parametrize(
    "query,limit", [("", 2), ("x\n", 2), ("x" * 601, 2), ("x", 0), ("x", 21), ("x", True)]
)
def test_query_contract_rejects_unbounded_or_untyped_fields(query: str, limit: int) -> None:
    with pytest.raises(InvalidInputError):
        specialist_search_body("crossref", query, limit)


def test_crossref_preserves_dates_and_notice_direction() -> None:
    batch = crossref_batch(
        **{
            "update-to": [
                {
                    "DOI": "10.5678/affected",
                    "type": "retraction",
                    "updated": {"date-parts": [[2026, 10, 1]]},
                }
            ]
        }
    )
    item = first(batch)
    metadata = item["specialist"]
    assert item["publishedAt"] is None
    assert metadata["publicationDate"] == {
        "value": "1843-09",
        "precision": "month",
        "provenance": "crossref:issued",
    }
    assert metadata["eventDate"]["value"] == "1842-07-12"
    assert metadata["updatedDate"]["value"] == "2026-11-01T12:00:00Z"
    assert metadata["retraction"]["state"] == "unknown"
    notice = metadata["retraction"]["notices"][0]
    assert notice["relation"] == "updates" and notice["targetDoi"] == "10.5678/affected"
    assert metadata["sourceRole"] == "primary"


def test_openalex_retraction_flag_is_provider_reported_not_inferred_truth() -> None:
    item = first(openalex_batch(is_retracted=True))
    assert item["specialist"]["retraction"]["state"] == "reported_retracted"
    assert item["specialist"]["sourceRole"] == "secondary"
    assert item["publishedAt"] is None
    assert (
        first(openalex_batch(is_retracted=False))["specialist"]["retraction"]["state"]
        == "not_reported"
    )
    assert openalex_batch(is_retracted="false")["rejected"] == [
        {"rank": 1, "reasonCode": "invalid_result"}
    ]


ARXIV = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2301.01234v2</id><title>A controlled research example</title>
<summary>Authored abstract.</summary><author><name>Ada Lovelace</name></author>
<published>2023-01-03T10:00:00-02:00</published><updated>2023-02-05T12:00:00Z</updated>
<arxiv:doi>10.1234/test.paper</arxiv:doi></entry></feed>"""


def test_arxiv_atom_preserves_versioned_identity_and_submission_dates() -> None:
    item = first(normalize("arxiv", ARXIV))
    assert item["canonicalUrl"] == "https://arxiv.org/abs/2301.01234v2"
    assert item["publishedAt"] == "2023-01-03T12:00:00Z"
    assert item["specialist"]["recordId"] == "2301.01234v2"
    assert item["specialist"]["retraction"]["state"] == "unknown"


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE feed [<!ENTITY example "expand">]><feed xmlns="http://www.w3.org/2005/Atom">&example;</feed>',
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad_query</id></entry></feed>',
        b"<rss></rss>",
        b"<feed",
        b"\xff",
    ],
)
def test_arxiv_denies_entities_error_feeds_and_unsupported_xml(payload: bytes) -> None:
    with pytest.raises(InvalidInputError):
        normalize("arxiv", payload)


def pubmed_sources() -> tuple[bytes, bytes]:
    return (
        encoded({"esearchresult": {"idlist": ["1234", "1235"]}}),
        encoded(
            {
                "result": {
                    "uids": ["1234", "1235"],
                    "1234": {
                        "uid": "1234",
                        "title": TITLE,
                        "authors": [{"name": "Ada Lovelace"}],
                        "pubdate": "2022 Nov 14",
                        "articleids": [{"idtype": "doi", "value": DOI}],
                        "pubtype": ["Journal Article", "Retracted Publication"],
                    },
                    "1235": {
                        "uid": "1235",
                        "title": "Correction of earlier result",
                        "authors": [],
                        "pubtype": ["Published Erratum"],
                    },
                }
            }
        ),
    )


def test_pubmed_summary_is_explicit_and_bound_to_exact_observed_search_order() -> None:
    search, summary = pubmed_sources()
    body = pubmed_summary_body(search, 3)
    assert body is not None
    method, url, content, _ = specialist_wire_request("pubmed", body)
    assert method == "GET" and content == b""
    assert urlsplit(url).path.endswith("/esummary.fcgi")
    assert parse_qs(urlsplit(url).query)["id"] == ["1234,1235"]
    verify_pubmed_summary(summary, search, 3)
    batch = normalize("pubmed", summary, 3)
    assert batch["items"][0]["specialist"]["retraction"]["state"] == "reported_retracted"
    assert batch["items"][1]["specialist"]["retraction"]["state"] == "unknown"
    assert batch["items"][1]["specialist"]["retraction"]["notices"][0]["kind"] == "correction"
    assert batch["items"][0]["publishedAt"] is None
    with pytest.raises(InvalidInputError):
        normalize("pubmed", search, 3)


@pytest.mark.parametrize("ids", [["9999"], ["1235", "1234"], ["1234", "1234"]])
def test_pubmed_rejects_injected_missing_reordered_or_duplicate_summary_ids(ids: list[str]) -> None:
    search, summary = pubmed_sources()
    payload = json.loads(summary)
    payload["result"]["uids"] = ids
    with pytest.raises(InvalidInputError):
        verify_pubmed_summary(encoded(payload), search, 3)


def test_pubmed_empty_search_needs_no_child_request() -> None:
    raw = encoded({"esearchresult": {"idlist": []}})
    assert pubmed_summary_body(raw, 3) is None
    assert normalize("pubmed", raw, 3)["items"] == []


def test_semantic_scholar_supplies_secondary_metadata_with_unknown_retraction() -> None:
    item = first(
        normalize(
            "semantic_scholar",
            {
                "data": [
                    {
                        "paperId": "a" * 40,
                        "title": TITLE,
                        "abstract": "Abstract text",
                        "externalIds": {"DOI": DOI},
                        "authors": [{"name": "Ada Lovelace"}],
                        "year": 2023,
                    }
                ]
            },
        )
    )
    assert item["specialist"]["publicationDate"]["precision"] == "year"
    assert item["specialist"]["retraction"]["state"] == "unknown"
    assert item["canonicalUrl"].endswith("/" + "a" * 40)


def test_wikidata_truthy_dates_do_not_invent_precision_or_freshness() -> None:
    item = first(
        normalize(
            "wikidata",
            {
                "results": {
                    "bindings": [
                        {
                            "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q123"},
                            "itemLabel": {"type": "literal", "value": TITLE},
                            "doi": {"type": "literal", "value": DOI.upper()},
                            "publicationDate": {"type": "literal", "value": "1843-01-01T00:00:00Z"},
                            "eventDate": {"type": "literal", "value": "1842-01-01T00:00:00Z"},
                        }
                    ]
                }
            },
        )
    )
    assert item["specialist"]["doi"] == DOI
    assert item["publishedAt"] is None
    assert item["specialist"]["publicationDate"]["precision"] == "reported"
    assert item["specialist"]["eventDate"]["value"].startswith("1842")


def test_archive_dates_never_relabel_historical_item_as_recent_publication() -> None:
    item = first(
        normalize(
            "internet_archive",
            {
                "responseHeader": {"status": 0},
                "response": {
                    "docs": [
                        {
                            "identifier": "old-work",
                            "title": "A historical work",
                            "creator": "Author",
                            "date": "1820-01-01T00:00:00Z",
                            "publicdate": "2026-11-17T12:00:00Z",
                        }
                    ]
                },
            },
        )
    )
    metadata = item["specialist"]
    assert item["publishedAt"] is None and metadata["publicationDate"] is None
    assert metadata["eventDate"]["value"].startswith("1820")
    assert metadata["updatedDate"]["value"].startswith("2026")
    assert metadata["sourceRole"] == "unknown"


def test_doi_match_preserves_differing_provider_titles_and_retraction_evidence() -> None:
    first_batch = crossref_batch()
    second_batch = openalex_batch(title="Alternate provider title", is_retracted=True)
    merged = merge_specialist_results([first_batch, second_batch])
    assert len(merged) == 1 and len(merged[0]["lineage"]) == 2
    assert merged[0]["lineage"][1]["specialist"]["retraction"]["state"] == "reported_retracted"
    assert set(merged[0]) == {"id", "canonicalUrl", "title", "snippet", "lineage"}


def test_conservative_title_author_match_needs_every_author_and_no_conflicting_doi() -> None:
    left = first(crossref_batch())
    right = first(openalex_batch(doi=None, title="Ａ controlled research example!"))
    assert same_work(left, right)
    right["specialist"]["authors"] = ["Other Author"]
    assert not same_work(left, right)
    right = first(openalex_batch(doi="10.5678/other"))
    assert not same_work(left, right)
    right["specialist"]["doi"] = None
    right["specialist"]["authors"] = []
    assert not same_work(left, right)


def test_doi_less_record_cannot_bridge_conflicting_work_identities() -> None:
    batches = [crossref_batch(), openalex_batch(doi="10.5678/other")]
    third = normalize(
        "semantic_scholar",
        {"data": [{"paperId": "a" * 40, "title": TITLE, "authors": [{"name": "Ada Lovelace"}]}]},
    )
    merged = merge_specialist_results([*batches, third])
    assert len(merged) == 3
    assert len({group["id"] for group in merged}) == 3


def test_exact_url_buckets_precede_metadata_matching_and_keep_web_lineage() -> None:
    batches = [crossref_batch(), openalex_batch()]
    web_item = copy.deepcopy(first(batches[1]))
    web_item.pop("specialist")
    web_item["provider"] = "brave"
    web_batch = {
        "provider": "brave",
        "retrievedAt": web_item["retrievedAt"],
        "items": [web_item],
        "rejected": [],
    }
    merged = merge_specialist_results([*batches, web_batch])
    # The web result supports only URL equivalence, so its OpenAlex bucket
    # cannot claim DOI equivalence with a differently located Crossref record.
    assert len(merged) == 2
    assert len(merged[1]["lineage"]) == 2


def test_conflicting_later_doi_on_existing_url_cannot_duplicate_group_identity() -> None:
    first_batch = crossref_batch()
    second_batch = openalex_batch(doi="10.5678/other")
    third_item = copy.deepcopy(first(first_batch))
    third_item["provider"] = "semantic_scholar"
    third_item["specialist"]["doi"] = "10.5678/other"
    third_batch = {
        "provider": "semantic_scholar",
        "retrievedAt": third_item["retrievedAt"],
        "items": [third_item],
        "rejected": [],
    }
    merged = merge_specialist_results([first_batch, second_batch, third_batch])
    assert len(merged) == 2
    assert len({group["id"] for group in merged}) == 2
    assert len(merged[0]["lineage"]) == 2
    assert {item["specialist"]["doi"] for item in merged[0]["lineage"]} == {DOI, "10.5678/other"}


@pytest.mark.parametrize("raw", [b'{"status":"ok","status":"ok"}', b'{"value":NaN}', b"[]", b"{"])
def test_ambiguous_and_nonfinite_json_fails_closed(raw: bytes) -> None:
    with pytest.raises(InvalidInputError):
        normalize("crossref", raw)


def test_batch_byte_bound_retains_rejected_ranks_instead_of_truncating_metadata() -> None:
    records = [
        crossref_record(DOI=f"10.1234/paper-{index}", abstract="q" * 16000) for index in range(12)
    ]
    batch = normalize("crossref", {"status": "ok", "message": {"items": records}}, 12)
    assert len(encoded(batch)) <= MAX_NORMALIZED_SPECIALIST_BYTES
    assert len(batch["items"]) < 12
    assert len(batch["items"]) + len(batch["rejected"]) == 12
    assert {entry["reasonCode"] for entry in batch["rejected"]} == {"normalized_result_limit"}


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("crossref", {"status": "failed", "message": {}}),
        ("openalex", {"error": "invalid api key"}),
        ("semantic_scholar", {"error": "not found"}),
        ("wikidata", {"message": "timeout"}),
        ("internet_archive", {"responseHeader": {"status": 1}, "response": {"docs": []}}),
    ],
)
def test_unavailable_or_changed_endpoint_schema_is_explicit(provider: str, payload: object) -> None:
    with pytest.raises(InvalidInputError):
        normalize(provider, payload)
