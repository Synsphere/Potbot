"""Bounded, data-only scientific and archival query contracts.

The public research owner admits a canonical POST envelope at one fixed logical
endpoint. This module deterministically derives its read-only wire request. No
network I/O, credential lookup, retry, follow-up or authority lives here. PubMed
ESummary is a separately admitted child request derived from retained ESearch
bytes, never a hidden second HTTP call.

Primary wire references (reviewed 18 November 2026):
https://github.com/CrossRef/rest-api-doc
https://help.openalex.org/api/authentication/
https://help.openalex.org/data/works/attributes/
https://info.arxiv.org/help/api/user-manual.html
https://info.arxiv.org/help/api/tou.html
https://www.ncbi.nlm.nih.gov/sites/books/NBK25499/
https://api.semanticscholar.org/api-docs
https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual
https://archive.org/advancedsearch.php

Only installed code may define routes and generated SPARQL. Provider downtime,
changed contracts and missing access remain unavailable, never another endpoint.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json

SPECIALIST_ENDPOINTS = {
    "crossref": "https://api.crossref.org/works",
    "openalex": "https://api.openalex.org/works",
    "arxiv": "https://export.arxiv.org/api/query",
    "pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
    "semantic_scholar": "https://api.semanticscholar.org/graph/v1/paper/search",
    "wikidata": "https://query.wikidata.org/sparql",
    "internet_archive": "https://archive.org/advancedsearch.php",
}
# These are conservative floors, not a promise of quota or account entitlement.
# arXiv's limit applies across all machines controlled by the operator; the
# deployment must also serialize that provider across its workers/installations.
SPECIALIST_MIN_INTERVAL_MS = {
    "crossref": 1000,
    "openalex": 1000,
    "arxiv": 3000,
    "pubmed": 400,
    "semantic_scholar": 1000,
    "wikidata": 1000,
    "internet_archive": 1000,
}
PUBMED_SUMMARY_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_SCHEMA = "potbot-specialist-query/v1"
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\\]{1,230}\Z", re.ASCII)
_ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v[1-9]\d*)?\Z")
_PMID = re.compile(r"[1-9]\d{0,8}\Z", re.ASCII)


def normalize_doi(value: object) -> str:
    """Normalize one explicit DOI, preserving suffix punctuation and identity."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise InvalidInputError("A DOI requires a bounded explicit identifier")
    text = value.strip()
    if text.lower().startswith("doi:"):
        text = text[4:].strip()
    if text.lower().startswith(("http://", "https://")):
        parts = urlsplit(text)
        if (
            parts.hostname not in {"doi.org", "dx.doi.org"}
            or parts.netloc.lower() not in {"doi.org", "dx.doi.org"}
            or parts.query
            or parts.fragment
        ):
            raise InvalidInputError("Only a literal DOI resolver URL identifies a DOI")
        try:
            text = unquote(parts.path.removeprefix("/"), errors="strict")
        except UnicodeError:
            raise InvalidInputError("DOI URL encoding is invalid") from None
    if (
        not text.isascii()
        or len(text) > 255
        or _DOI.fullmatch(text) is None
        or any(ord(char) < 33 or ord(char) == 127 for char in text)
    ):
        raise InvalidInputError("DOI syntax is unsupported")
    return text.lower()


def doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(normalize_doi(doi), safe="/()-.;:_")


def specialist_descriptor(provider: str) -> dict[str, Any]:
    if not isinstance(provider, str) or provider not in SPECIALIST_ENDPOINTS:
        raise InvalidInputError("Specialist research provider is unsupported")
    return {
        "id": provider,
        "method": "POST",
        "endpoint": SPECIALIST_ENDPOINTS[provider],
        "adapterRevision": 1,
        "mode": "specialist_retrieval",
        "scope": "explicit_query_disclosure",
    }


def _limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 20:
        raise InvalidInputError("Specialist results must be bounded to 1-20 records")
    return value


def _query(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidInputError("Specialist query must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise InvalidInputError("Specialist query must be valid UTF-8") from None
    if (
        not value.strip()
        or len(value) > 600
        or len(encoded) > 4096
        or len(value.split()) > 75
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InvalidInputError("Specialist query exceeds its literal text contract")
    return value


def specialist_search_body(provider: str, query: str, max_results: int) -> bytes:
    """Retain exact query text; explicit DOI and arXiv ID inputs select lookups."""
    body = _search_body(provider, query, max_results)
    # Validate the generated wire route now, before the owner admits a run.
    # Percent-encoded Unicode and the fixed SPARQL template can exceed the
    # shared transport's 2048-byte URL limit before the text bound is reached.
    specialist_wire_request(provider, body)
    return body


def _search_body(provider: str, query: str, max_results: int) -> bytes:
    specialist_descriptor(provider)
    _limit(max_results)
    text = _query(query)
    operation = "search"
    if (
        text.strip()
        .lower()
        .startswith(
            (
                "doi:",
                "10.",
                "https://doi.org/",
                "http://doi.org/",
                "https://dx.doi.org/",
                "http://dx.doi.org/",
            )
        )
    ):
        text = normalize_doi(text)
        operation = "doi"
    elif provider == "arxiv" and text.strip().lower().startswith("arxiv:"):
        text = text.strip()[6:]
        if _ARXIV_ID.fullmatch(text) is None:
            raise InvalidInputError("arXiv lookup requires an exact supported identifier")
        operation = "identifier"
    return canonical_json(
        {
            "schema": _SCHEMA,
            "provider": provider,
            "operation": operation,
            "query": text,
            "maxResults": max_results,
        }
    ).encode("utf-8")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON value")


def load_specialist_json(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > 262144:
        raise InvalidInputError("Specialist response exceeds its complete byte bound")
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidInputError("Specialist response is not a supported JSON object") from None
    if not isinstance(value, dict):
        raise InvalidInputError("Specialist response requires a JSON object")
    return value


def _envelope(provider: str, body: bytes) -> dict[str, Any]:
    specialist_descriptor(provider)
    if type(body) is not bytes or not 1 <= len(body) <= 8192:
        raise InvalidInputError("Specialist query envelope is unbounded")
    value = load_specialist_json(body)
    if value.get("operation") == "summary":
        if set(value) != {"schema", "provider", "operation", "ids", "maxResults"}:
            raise InvalidInputError("PubMed summary envelope has unsupported fields")
        ids = _pubmed_ids(value.get("ids"), _limit(value.get("maxResults")))
        if provider != "pubmed" or not ids:
            raise InvalidInputError("Only nonempty PubMed IDs can request a summary")
    else:
        if set(value) != {"schema", "provider", "operation", "query", "maxResults"}:
            raise InvalidInputError("Specialist query envelope has unsupported fields")
        expected = _search_body(provider, value["query"], _limit(value["maxResults"]))
        # Identifier operations carry canonical bare IDs, not the user prefix.
        if value.get("operation") == "identifier" and provider == "arxiv":
            expected = _search_body(provider, "arxiv:" + value["query"], value["maxResults"])
        if expected != body:
            raise InvalidInputError("Specialist query envelope is not canonical")
    if (
        value["schema"] != _SCHEMA
        or value["provider"] != provider
        or canonical_json(value).encode("utf-8") != body
    ):
        raise InvalidInputError("Specialist query does not bind its selected provider")
    return value


def _literal(text: str) -> str:
    # JSON string escapes are a safe subset of SPARQL string escapes for the
    # bounded query's printable Unicode; always emit a single literal token.
    return json.dumps(text, ensure_ascii=False)


def _quoted_search(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wikidata_query(text: str, operation: str, limit: int) -> str:
    if operation == "doi":
        selector = "?item wdt:P356 " + _literal(text.upper()) + " ."
    elif re.fullmatch(r"Q[1-9]\d{0,11}", text):
        selector = "VALUES ?item { wd:" + text + " }"
    else:
        # Exact English label matching avoids arbitrary SPARQL, federation and
        # full-store substring scans. Broad semantic/label search is outside
        # this declared profile, and must never silently select another API.
        selector = "?item rdfs:label " + _literal(text) + "@en ."
    return (
        "PREFIX wd: <http://www.wikidata.org/entity/> "
        "PREFIX wdt: <http://www.wikidata.org/prop/direct/> "
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
        "SELECT DISTINCT ?item ?itemLabel ?doi ?publicationDate ?eventDate WHERE { "
        + selector
        + ' OPTIONAL { ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "en") } '
        "OPTIONAL { ?item wdt:P356 ?doi } "
        "OPTIONAL { ?item wdt:P577 ?publicationDate } "
        "OPTIONAL { ?item wdt:P585 ?eventDate } "
        "} ORDER BY STR(?item) LIMIT " + str(limit)
    )


def specialist_wire_request(
    provider: str, body: bytes, credential: str | None = None
) -> tuple[str, str, bytes, dict[str, str]]:
    """Derive one GET at a fixed API; credential values enter headers only."""
    request = _envelope(provider, body)
    if credential is not None and (
        provider not in {"openalex", "semantic_scholar"}
        or not isinstance(credential, str)
        or not 1 <= len(credential) <= 4096
        or any(not 33 <= ord(char) <= 126 for char in credential)
    ):
        raise InvalidInputError("Specialist credential is unsupported or invalid")
    headers = {"Accept": "application/json"}
    if credential is not None:
        if provider == "openalex":
            headers["Authorization"] = "Bearer " + credential
        else:
            headers["x-api-key"] = credential
    endpoint = SPECIALIST_ENDPOINTS[provider]
    limit, operation = request["maxResults"], request["operation"]
    text = request.get("query", "")
    params: list[tuple[str, str]]
    if provider == "crossref":
        if operation == "doi":
            endpoint += "/" + quote(text, safe="")
            params = []
        else:
            params = [("query.bibliographic", text), ("rows", str(limit))]
    elif provider == "openalex":
        params = [
            ("filter", "doi:" + text) if operation == "doi" else ("search", text),
            ("per-page", str(limit)),
        ]
    elif provider == "arxiv":
        headers["Accept"] = "application/atom+xml"
        params = [
            ("id_list", text)
            if operation == "identifier"
            else ("search_query", "all:" + _quoted_search(text)),
            ("start", "0"),
            ("max_results", str(limit)),
        ]
    elif provider == "pubmed":
        if operation == "summary":
            endpoint = PUBMED_SUMMARY_ENDPOINT
            params = [("db", "pubmed"), ("id", ",".join(request["ids"])), ("retmode", "json")]
        else:
            term = _quoted_search(text) + ("[AID]" if operation == "doi" else "[All Fields]")
            params = [
                ("db", "pubmed"),
                ("term", term),
                ("retmode", "json"),
                ("retmax", str(limit)),
                ("retstart", "0"),
                ("usehistory", "n"),
            ]
    elif provider == "semantic_scholar":
        fields = "title,abstract,authors,externalIds,publicationDate,year,publicationTypes,url"
        if operation == "doi":
            endpoint = "https://api.semanticscholar.org/graph/v1/paper/DOI:" + quote(text, safe="")
            params = [("fields", fields)]
        else:
            params = [("query", text), ("limit", str(limit)), ("fields", fields)]
    elif provider == "wikidata":
        headers["Accept"] = "application/sparql-results+json"
        params = [("query", _wikidata_query(text, operation, limit)), ("format", "json")]
    else:
        params = [
            ("q", _quoted_search(text)),
            ("rows", str(limit)),
            ("page", "1"),
            ("output", "json"),
        ] + [
            ("fl[]", field)
            for field in (
                "identifier",
                "title",
                "creator",
                "description",
                "date",
                "publicdate",
                "mediatype",
            )
        ]
    url = endpoint + ("?" + urlencode(params) if params else "")
    if len(url) > 2048:
        raise InvalidInputError("Generated specialist request exceeds its route bound")
    return "GET", url, b"", headers


def _pubmed_ids(value: Any, limit: int) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(item, str) or _PMID.fullmatch(item) is None for item in value)
        or len(set(value)) != len(value)
    ):
        raise InvalidInputError("PubMed IDs are invalid, duplicate or outside the reviewed limit")
    return list(value)


def pubmed_search_ids(raw: bytes, max_results: int) -> list[str]:
    payload = load_specialist_json(raw)
    result = payload.get("esearchresult")
    if not isinstance(result, dict) or "error" in payload or result.get("errorlist"):
        raise InvalidInputError("PubMed search schema or query is unsupported")
    return _pubmed_ids(result.get("idlist"), _limit(max_results))


def pubmed_summary_body(raw: bytes, max_results: int) -> bytes | None:
    ids = pubmed_search_ids(raw, max_results)
    if not ids:
        return None
    return canonical_json(
        {
            "schema": _SCHEMA,
            "provider": "pubmed",
            "operation": "summary",
            "ids": ids,
            "maxResults": max_results,
        }
    ).encode("utf-8")


def verify_pubmed_summary(summary_raw: bytes, search_raw: bytes, max_results: int) -> None:
    """Reject injected, missing or duplicate records before publishing metadata."""
    expected = pubmed_search_ids(search_raw, max_results)
    payload = load_specialist_json(summary_raw)
    result = payload.get("result")
    if not isinstance(result, dict) or "error" in payload:
        raise InvalidInputError("PubMed summary schema is unsupported")
    ids = _pubmed_ids(result.get("uids"), _limit(max_results))
    if ids != expected or set(result) != {"uids", *expected}:
        raise InvalidInputError("PubMed summary does not match its retained search IDs")
    for identity in expected:
        value = result[identity]
        if not isinstance(value, dict) or value.get("uid") != identity or value.get("error"):
            raise InvalidInputError("PubMed summary record identity is invalid")


# Lazy imports keep the fixed route table usable by ExternalRequest without an
# import cycle through the existing web-provider normalizers.
def normalize_specialist_results(
    provider: str,
    raw: bytes,
    *,
    retrieved_at: datetime,
    max_results: int,
) -> dict[str, Any]:
    from potbot.research.specialist_records import normalize_specialist_results as normalize

    return normalize(provider, raw, retrieved_at=retrieved_at, max_results=max_results)


def merge_specialist_results(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from potbot.research.specialist_records import merge_specialist_results as merge

    return merge(batches)
