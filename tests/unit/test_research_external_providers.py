from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from potbot.domain.errors import DomainError, InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.research.external_contracts import ExternalRequest
from potbot.research.providers import (
    canonical_source_url,
    fetch_request,
    merge_search_results,
    normalize_search_results,
    provider_descriptor,
    robots_interval_ms,
    robots_permits,
    robots_request,
    search_request,
)
from potbot.research.source_policy import (
    ExternalResearchPolicy,
    ResearchFetchHostRule,
    ResearchProviderRule,
)

NOW = datetime(2026, 11, 18, tzinfo=UTC)


def policy(**changes):
    return replace(
        ExternalResearchPolicy(
            allow_query_disclosure=True,
            providers=(ResearchProviderRule("brave", 10, 0), ResearchProviderRule("tavily", 20, 0)),
            fetch_hosts=(ResearchFetchHostRule("docs.example.com", True, 0),),
            fallback_statuses=("rate_limited", "quota_exhausted"),
        ),
        **changes,
    )


def test_legacy_source_catalogue_import_does_not_require_optional_http_dependencies() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from potbot.research.source_policy import ResearchSourceCatalog",
        ],
        env={"PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_source_policy_roundtrip_is_exact_and_credential_free(tmp_path) -> None:
    selected = policy()
    raw = selected.to_dict()
    assert ExternalResearchPolicy.from_dict(raw) == selected
    assert selected.fingerprint == selected.digest
    path = tmp_path / "research.json"
    path.write_text(json.dumps(raw))
    assert ExternalResearchPolicy.read(path) == selected
    assert "credential" not in json.dumps(raw).lower()
    assert "endpoint" not in json.dumps(raw).lower()
    assert replace(selected, max_documents=1).fingerprint != selected.fingerprint
    assert (
        replace(selected, providers=tuple(reversed(selected.providers))).fingerprint
        != selected.fingerprint
    )


@pytest.mark.parametrize(
    "original,ambiguous",
    [
        (
            '"allow_query_disclosure": true',
            '"allow_query_disclosure": false, "allow_query_disclosure": true',
        ),
        ('"terms_approved": true', '"terms_approved": false, "terms_approved": true'),
        ('"provider": "brave"', '"provider": "tavily", "provider": "brave"'),
        ('"max_requests": 24', '"max_requests": 24, "max_requests": 24'),
    ],
)
def test_policy_file_rejects_ambiguous_top_level_and_nested_authority(
    tmp_path, original, ambiguous
) -> None:
    raw = json.dumps(policy().to_dict())
    assert original in raw
    path = tmp_path / "ambiguous-research.json"
    path.write_text(raw.replace(original, ambiguous, 1))
    with pytest.raises(InvalidInputError, match="unreadable or invalid"):
        ExternalResearchPolicy.read(path)


@pytest.mark.parametrize(
    "change",
    [
        {"allow_query_disclosure": 1},
        {"max_requests": True},
        {"max_source_bytes": 262145},
        {"max_documents": 9},
        {"max_results": 21},
        {"timeout_seconds": 31},
        {"fallback_statuses": ("timed_out",)},
        {"fallback_statuses": ("indeterminate",)},
        {"providers": (ResearchProviderRule("brave", 0), ResearchProviderRule("brave", 0))},
    ],
)
def test_source_policy_cannot_expand_or_coerce_authority(change) -> None:
    with pytest.raises(InvalidInputError):
        policy(**change)


def test_unknown_provider_endpoint_override_and_unapproved_disclosure_are_denied() -> None:
    raw = policy().to_dict()
    raw["providers"][0]["endpoint"] = "https://attacker.example/search"
    with pytest.raises(InvalidInputError):
        ExternalResearchPolicy.from_dict(raw)
    with pytest.raises(InvalidInputError):
        ResearchProviderRule("unsupported", 0)
    with pytest.raises(ScopeDeniedError):
        search_request(policy(allow_query_disclosure=False), "brave", "literal question")
    with pytest.raises(ScopeDeniedError):
        search_request(policy(providers=(ResearchProviderRule("brave", 0),)), "tavily", "question")


@pytest.mark.parametrize("provider", ["brave", "tavily"])
def test_provider_requests_use_fixed_post_and_preserve_exact_query(provider) -> None:
    query = "Unicode café exact_identifier"
    request = search_request(policy(), provider, query)
    descriptor = provider_descriptor(provider)
    assert request.method == descriptor["method"] == "POST"
    assert request.url == descriptor["endpoint"] and "?" not in request.url
    assert query == json.loads(request.body)["q" if provider == "brave" else "query"]
    assert search_request(policy(), provider, query).fingerprint == request.fingerprint
    assert search_request(policy(), provider, query + " changed").fingerprint != request.fingerprint
    assert "body" not in request.to_dict()
    assert "api_key" not in request.body.decode()
    if provider == "tavily":
        assert json.loads(request.body)["include_answer"] is False
        assert json.loads(request.body)["include_raw_content"] is False


@pytest.mark.parametrize(
    "query", ["", "x" * 601, "word " * 76, "secret\nsecond line", "bad\x00", "\ud800"]
)
def test_search_query_bounds_fail_before_transport(query) -> None:
    with pytest.raises(InvalidInputError):
        search_request(policy(), "brave", query)


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.example.com/a",
        "https://127.0.0.1/a",
        "https://user:pass@docs.example.com/a",
        "https://docs.example.com:444/a",
        "https://docs.example.com\\@evil.example/a",
        "https://docs.example.com/a\n",
        "https://xn--example-9d0b.com/a",
    ],
)
def test_source_url_canonicalization_cannot_hide_noncanonical_destinations(url) -> None:
    with pytest.raises(DomainError):
        canonical_source_url(url)


def test_results_strip_only_fragments_and_keep_query_semantics_and_source_lineage() -> None:
    source = "https://docs.example.com/spec?a=1&b=2"
    brave = normalize_search_results(
        "brave",
        json.dumps(
            {
                "web": {
                    "results": [
                        {
                            "url": source + "#anchor",
                            "title": "First title",
                            "description": "Engine one",
                            "page_age": "2020-01-01T02:00:00Z",
                        },
                    ]
                }
            }
        ).encode(),
        retrieved_at=NOW,
        max_results=8,
    )
    tavily = normalize_search_results(
        "tavily",
        json.dumps(
            {
                "results": [
                    {
                        "url": source,
                        "title": "Second title",
                        "content": "Engine two",
                        "published_date": "2020-01-01",
                    },
                    {
                        "url": "http://insecure.example/page",
                        "title": "Rejected",
                        "content": "no fetch",
                    },
                ]
            }
        ).encode(),
        retrieved_at=NOW,
        max_results=8,
    )
    merged = merge_search_results([brave, tavily])
    assert len(merged) == 1
    assert merged[0]["canonicalUrl"] == source
    assert [hit["provider"] for hit in merged[0]["lineage"]] == ["brave", "tavily"]
    assert merged[0]["lineage"][0]["publishedAt"] == "2020-01-01T02:00:00Z"
    assert merged[0]["lineage"][1]["publishedAt"] is None
    assert merged[0]["lineage"][1]["retrievedAt"] == "2026-11-18T00:00:00Z"
    assert tavily["rejected"] == [{"rank": 2, "reasonCode": "invalid_result"}]


def test_malformed_provider_schema_and_duplicate_json_keys_are_not_empty_successes() -> None:
    for raw in (b"[]", b'{"results":null}', b'{"results":[],"results":[]}'):
        with pytest.raises(InvalidInputError):
            normalize_search_results("tavily", raw, retrieved_at=NOW, max_results=8)
    with pytest.raises(InvalidInputError):
        normalize_search_results("brave", b"{}", retrieved_at=NOW, max_results=8)


def test_normalizer_preserves_rejected_result_limit_instead_of_inventing_completeness() -> None:
    response = normalize_search_results(
        "tavily",
        json.dumps(
            {
                "results": [
                    {"url": "https://docs.example.com/one", "title": "One"},
                    {"url": "https://docs.example.com/two", "title": "Two"},
                ]
            }
        ).encode(),
        retrieved_at=NOW,
        max_results=1,
    )
    assert len(response["items"]) == 1
    assert response["rejected"] == [{"rank": 2, "reasonCode": "result_limit"}]


def test_normalized_provider_byte_limit_preserves_exact_admitted_lineage_and_rejected_ranks() -> (
    None
):
    batches = []
    snippet = "source text " * 900
    for provider in ("brave", "tavily"):
        rows = [
            {
                "url": f"https://docs.example.com/{provider}/{rank}",
                "title": f"Result {rank}",
                "description" if provider == "brave" else "content": snippet,
            }
            for rank in range(20)
        ]
        raw = json.dumps(
            {"web": {"results": rows}} if provider == "brave" else {"results": rows}
        ).encode()
        assert len(raw) < 262144
        batch = normalize_search_results(provider, raw, retrieved_at=NOW, max_results=20)
        assert len(canonical_json(batch).encode()) <= 100000
        assert batch["items"] and batch["rejected"]
        assert all(item["snippet"] == snippet for item in batch["items"])
        assert all(item["reasonCode"] == "normalized_result_limit" for item in batch["rejected"])
        assert sorted([row["rank"] for row in batch["items"] + batch["rejected"]]) == list(
            range(1, 21)
        )
        batches.append(batch)
    merged = merge_search_results(batches)
    assert len(canonical_json(merged).encode()) < 400000
    assert all(item["lineage"][0]["snippet"] == snippet for item in merged)


def test_fetch_and_robots_require_exact_reviewed_host_and_never_carry_provider_credentials() -> (
    None
):
    target = "https://docs.example.com/spec?v=1"
    assert robots_request(policy(), target).url == "https://docs.example.com/robots.txt"
    request = fetch_request(policy(), target)
    assert request.method == "GET" and request.provider is None and request.body == b""
    for url in ("https://evil.example/spec", "https://sub.docs.example.com/spec"):
        with pytest.raises(ScopeDeniedError):
            fetch_request(policy(), url)
    with pytest.raises(ScopeDeniedError):
        fetch_request(
            policy(fetch_hosts=(ResearchFetchHostRule("docs.example.com", False),)), target
        )
    with pytest.raises(InvalidInputError):
        ExternalRequest("robots", None, "GET", target, b"")
    with pytest.raises(InvalidInputError):
        ExternalRequest("fetch", None, "GET", target, bytearray())


def test_robots_wildcards_longest_allow_and_user_agent_groups_are_not_permissive_guesses() -> None:
    raw = b"User-agent: *\nDisallow: /\n\nUser-agent: PotbotResearch\nDisallow: /private/*\nAllow: /private/public$\nCrawl-delay: 0.125\nUser-agent: PotbotResearch\nDisallow: /other\nRequest-rate: 2/5\n"
    assert not robots_permits(raw, "https://docs.example.com/private/secret")
    assert robots_permits(raw, "https://docs.example.com/private/public")
    assert not robots_permits(raw, "https://docs.example.com/private/public/child")
    assert not robots_permits(raw, "https://docs.example.com/other")
    assert robots_permits(raw, "https://docs.example.com/readable")
    assert robots_interval_ms(raw, "https://docs.example.com/readable") == 2500


def test_robots_percent_encoding_and_allow_ties_follow_exact_path_matching() -> None:
    raw = "User-agent: *\nDisallow: /café\nDisallow: /secret\nAllow: /secret\nDisallow: /literal%2A$\n".encode()
    assert not robots_permits(raw, "https://docs.example.com/caf%C3%A9")
    assert robots_permits(raw, "https://docs.example.com/secret")
    assert not robots_permits(raw, "https://docs.example.com/literal*")
    assert robots_permits(raw, "https://docs.example.com/literal-anything")


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>Error</html>",
        b'{"error":"temporary provider failure"}',
        b"error: temporarily unavailable",
        b"User-agent: *\nDisallow: /bad%xx",
        b"User-agent: *\nCrawl-delay: infinity",
        b"User-agent: *\nRequest-rate: 0/1",
        b"\xff",
        b"x" * 65537,
    ],
)
def test_invalid_or_excessive_robots_never_grants_fetch(raw) -> None:
    with pytest.raises(InvalidInputError):
        robots_permits(raw, "https://docs.example.com/page")
