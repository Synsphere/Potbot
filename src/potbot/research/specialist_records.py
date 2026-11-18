"""Bounded provider metadata normalization and conservative work matching.

Metadata lineage describes the provider's claim. A primary metadata record is
not a verified primary research conclusion. Missing correction/retraction data
is unknown, and two indices of the same work are not independent publishers.
Dates retain their original precision; an archival event or indexing time must
never become a recent publication instant.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.research.specialist import (
    SPECIALIST_ENDPOINTS,
    doi_url,
    load_specialist_json,
    normalize_doi,
    pubmed_search_ids,
    specialist_descriptor,
)

MAX_NORMALIZED_SPECIALIST_BYTES = 40000
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_KINDS = {
    "crossref": ("primary", "publisher_registry"),
    "openalex": ("secondary", "bibliographic_index"),
    "arxiv": ("primary", "repository"),
    "pubmed": ("secondary", "bibliographic_index"),
    "semantic_scholar": ("secondary", "bibliographic_index"),
    "wikidata": ("secondary", "knowledge_graph"),
    "internet_archive": ("unknown", "archive_catalogue"),
}


def _text(value: Any, limit: int, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    try:
        size = len(value.encode("utf-8")) if isinstance(value, str) else limit + 1
    except UnicodeError:
        raise InvalidInputError("Specialist metadata is not valid UTF-8") from None
    if (
        not isinstance(value, str)
        or size > limit
        or any(ord(char) < 32 and char not in "\n\t\r" for char in value)
        or "\x7f" in value
    ):
        raise InvalidInputError("Specialist metadata text is invalid or excessive")
    return value


def _list(value: Any, limit: int = 100) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise InvalidInputError("Specialist metadata collection is invalid or excessive")
    return value


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidInputError("Specialist metadata record must be an object")
    return value


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidInputError("Specialist retrieval time requires an explicit timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _date(value: Any, provenance: str, *, literal_only: bool = False) -> dict[str, str] | None:
    if value in (None, ""):
        return None
    text = _text(value, 128)
    precision = "reported"
    if not literal_only:
        try:
            if re.fullmatch(r"\d{4}", text) and 1 <= int(text) <= 9999:
                precision = "year"
            elif re.fullmatch(r"\d{4}-\d{2}", text):
                date.fromisoformat(text + "-01")
                precision = "month"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                date.fromisoformat(text)
                precision = "day"
            elif "T" in text:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    text = _utc(parsed)
                    precision = "instant"
        except ValueError:
            # Preserve the provider's literal date without inventing precision.
            precision = "reported"
    return {"value": text, "precision": precision, "provenance": provenance}


def _parts(value: Any, provenance: str) -> dict[str, str] | None:
    if value is None:
        return None
    record = _object(value)
    if "date-parts" not in record:
        return None
    rows = _list(record["date-parts"], 1)
    if not rows:
        return None
    parts = _list(rows[0], 3)
    if not parts or any(type(value) is not int for value in parts):
        raise InvalidInputError("Publication date parts are invalid")
    year, *rest = parts
    if not 1 <= year <= 9999:
        raise InvalidInputError("Publication date year is invalid")
    month, day = (rest + [1, 1])[:2]
    try:
        date(year, month, day)
    except ValueError:
        raise InvalidInputError("Publication date components are invalid") from None
    text = f"{year:04d}"
    if len(parts) >= 2:
        text += f"-{parts[1]:02d}"
    if len(parts) == 3:
        text += f"-{parts[2]:02d}"
    return _date(text, provenance)


def _authors(values: list[Any]) -> list[str]:
    if len(values) > 64:
        raise InvalidInputError("Specialist author list exceeds the supported bound")
    result = []
    for value in values:
        text = _text(value, 512).strip()
        if not text:
            raise InvalidInputError("Specialist author identity is empty")
        result.append(text)
    return result


def _optional_doi(value: Any) -> str | None:
    return None if value in (None, "") else normalize_doi(value)


def _metadata(provider: str, record_id: str) -> dict[str, Any]:
    identity = _text(record_id, 512)
    if not identity:
        raise InvalidInputError("Specialist record identity is missing")
    role, kind = _KINDS[provider]
    return {
        "schema": "potbot-specialist-metadata/v1",
        "recordId": identity,
        "doi": None,
        "authors": [],
        "publicationDate": None,
        "eventDate": None,
        "updatedDate": None,
        "sourceRole": role,
        "recordKind": kind,
        "retraction": {"state": "unknown", "notices": []},
    }


def _notice(kind: str, relation: str, target: Any = None, when: Any = None) -> dict[str, Any]:
    return {"kind": kind, "relation": relation, "targetDoi": _optional_doi(target), "date": when}


def _crossref(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    doi = normalize_doi(row.get("DOI"))
    titles = _list(row.get("title"), 16)
    if not titles:
        raise InvalidInputError("Crossref work has no title")
    metadata = _metadata("crossref", doi)
    metadata["doi"] = doi
    metadata["authors"] = _authors(
        [
            " ".join(
                _text(author.get(part), 256, optional=True).strip() for part in ("given", "family")
            ).strip()
            or _text(author.get("name"), 512)
            for value in _list(row.get("author", []), 64)
            for author in [_object(value)]
        ]
    )
    for field in ("published", "issued", "published-online", "published-print"):
        metadata["publicationDate"] = _parts(row.get(field), "crossref:" + field)
        if metadata["publicationDate"] is not None:
            break
    if row.get("event") is not None:
        metadata["eventDate"] = _parts(_object(row["event"]).get("start"), "crossref:event.start")
    if row.get("deposited") is not None:
        metadata["updatedDate"] = _date(
            _object(row["deposited"]).get("date-time"), "crossref:deposited"
        )
    notices = []
    for entry in _list(row.get("update-to", []), 20):
        update = _object(entry)
        kind = update.get("type")
        if kind not in {"retraction", "correction", "expression_of_concern", "withdrawal"}:
            kind = "update"
        notices.append(
            _notice(
                kind,
                "updates",
                update.get("DOI"),
                _parts(update.get("updated"), "crossref:update-to.updated"),
            )
        )
    # Crossref's update-to points from a notice to the affected work. It never
    # establishes that the notice itself has been retracted.
    relations = _object(row.get("relation", {}))
    for relation, kind in (("is-retracted-by", "retraction"), ("is-corrected-by", "correction")):
        for entry in _list(relations.get(relation, []), 20):
            item = _object(entry)
            if item.get("id-type") != "doi":
                continue
            notices.append(_notice(kind, "updated_by", item.get("id")))
            if kind == "retraction":
                metadata["retraction"]["state"] = "reported_retracted"
    if len(notices) > 20:
        raise InvalidInputError("Crossref update notices exceed the supported bound")
    metadata["retraction"]["notices"] = notices
    return (
        doi_url(doi),
        _text(titles[0], 2048),
        _text(row.get("abstract"), 16384, optional=True),
        metadata,
    )


def _openalex(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    identity = _text(row.get("id"), 512)
    if re.fullmatch(r"https://openalex\.org/W[1-9]\d{0,15}", identity) is None:
        raise InvalidInputError("OpenAlex work identity is unsupported")
    metadata = _metadata("openalex", identity)
    metadata["doi"] = _optional_doi(row.get("doi"))
    metadata["authors"] = _authors(
        [
            _object(_object(item).get("author")).get("display_name")
            for item in _list(row.get("authorships", []), 64)
        ]
    )
    metadata["publicationDate"] = _date(row.get("publication_date"), "openalex:publication_date")
    metadata["updatedDate"] = _date(row.get("updated_date"), "openalex:updated_date")
    retracted = row.get("is_retracted")
    if retracted is not None and type(retracted) is not bool:
        raise InvalidInputError("OpenAlex retraction flag is invalid")
    if type(retracted) is bool:
        metadata["retraction"]["state"] = "reported_retracted" if retracted else "not_reported"
        if retracted:
            metadata["retraction"]["notices"] = [
                _notice("retraction", "record_flag", metadata["doi"])
            ]
    # Inverted abstracts can amplify payloads or create unsupported text
    # orderings; this profile retains the raw index without reconstructing it.
    return identity, _text(row.get("title") or row.get("display_name"), 2048), "", metadata


def _arxiv(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    identifier = _text(row.get("id"), 512)
    parts = urlsplit(identifier)
    if (
        parts.scheme not in {"http", "https"}
        or parts.netloc != "arxiv.org"
        or parts.query
        or parts.fragment
        or not parts.path.startswith("/abs/")
        or re.fullmatch(
            r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v[1-9]\d*)?", parts.path[5:]
        )
        is None
    ):
        raise InvalidInputError("arXiv entry does not identify a supported paper")
    identity = parts.path[5:]
    metadata = _metadata("arxiv", identity)
    metadata["doi"] = _optional_doi(row.get("doi"))
    metadata["authors"] = _authors(_list(row.get("authors"), 64))
    metadata["publicationDate"] = _date(row.get("published"), "arxiv:first_submission")
    metadata["updatedDate"] = _date(row.get("updated"), "arxiv:version_submission")
    return (
        "https://arxiv.org/abs/" + identity,
        _text(row.get("title"), 2048),
        _text(row.get("summary"), 16384, optional=True),
        metadata,
    )


def _pubmed(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    identity = _text(row.get("uid"), 20)
    if re.fullmatch(r"[1-9]\d{0,8}", identity) is None or row.get("error"):
        raise InvalidInputError("PubMed summary identity is unsupported")
    metadata = _metadata("pubmed", identity)
    dois = {
        normalize_doi(item.get("value"))
        for raw_item in _list(row.get("articleids", []), 32)
        for item in [_object(raw_item)]
        if item.get("idtype") == "doi"
    }
    if len(dois) > 1:
        raise InvalidInputError("PubMed work has conflicting DOI identities")
    metadata["doi"] = next(iter(dois), None)
    metadata["authors"] = _authors(
        [_object(item).get("name") for item in _list(row.get("authors", []), 64)]
    )
    metadata["publicationDate"] = _date(row.get("pubdate"), "pubmed:pubdate")
    for raw_type in _list(row.get("pubtype", []), 32):
        name = _text(raw_type, 128)
        if name == "Retracted Publication":
            metadata["retraction"]["state"] = "reported_retracted"
            metadata["retraction"]["notices"].append(
                _notice("retraction", "record_flag", metadata["doi"])
            )
        elif name in {"Retraction of Publication", "Published Erratum", "Expression of Concern"}:
            kind = {
                "Retraction of Publication": "retraction",
                "Published Erratum": "correction",
                "Expression of Concern": "expression_of_concern",
            }[name]
            metadata["retraction"]["notices"].append(_notice(kind, "updates"))
    return (
        "https://pubmed.ncbi.nlm.nih.gov/" + identity + "/",
        _text(row.get("title"), 2048),
        "",
        metadata,
    )


def _semantic_scholar(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    identity = _text(row.get("paperId"), 160)
    if re.fullmatch(r"[0-9a-fA-F]{40}", identity) is None:
        raise InvalidInputError("Semantic Scholar paper identity is unsupported")
    metadata = _metadata("semantic_scholar", identity.lower())
    metadata["doi"] = _optional_doi(_object(row.get("externalIds") or {}).get("DOI"))
    metadata["authors"] = _authors(
        [_object(item).get("name") for item in _list(row.get("authors", []), 64)]
    )
    publication = row.get("publicationDate")
    if publication is None and type(row.get("year")) is int and 1 <= row["year"] <= 9999:
        publication = str(row["year"])
    metadata["publicationDate"] = _date(publication, "semantic_scholar:publicationDate_or_year")
    return (
        "https://www.semanticscholar.org/paper/" + identity.lower(),
        _text(row.get("title"), 2048),
        _text(row.get("abstract"), 16384, optional=True),
        metadata,
    )


def _binding(row: dict[str, Any], field: str) -> str | None:
    if field not in row:
        return None
    value = _object(row[field])
    if value.get("type") not in {"uri", "literal", "typed-literal"}:
        raise InvalidInputError("Wikidata binding type is unsupported")
    return _text(value.get("value"), 2048)


def _wikidata(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    uri = _binding(row, "item") or ""
    matched = re.fullmatch(r"https?://www\.wikidata\.org/entity/(Q[1-9]\d{0,11})", uri)
    if matched is None or _object(row["item"]).get("type") != "uri":
        raise InvalidInputError("Wikidata item identity is unsupported")
    identity = matched[1]
    metadata = _metadata("wikidata", identity)
    metadata["doi"] = _optional_doi(_binding(row, "doi"))
    # WDQS's truthy dateTime projection does not include Wikibase time
    # precision. A midnight value may represent only a year; do not invent an
    # exact publication instant or use it for freshness.
    metadata["publicationDate"] = _date(
        _binding(row, "publicationDate"),
        "wikidata:P577_truthy_precision_unknown",
        literal_only=True,
    )
    metadata["eventDate"] = _date(
        _binding(row, "eventDate"), "wikidata:P585_truthy_precision_unknown", literal_only=True
    )
    return (
        "https://www.wikidata.org/wiki/" + identity,
        _text(_binding(row, "itemLabel") or identity, 2048),
        "",
        metadata,
    )


def _archive(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    identity = _text(row.get("identifier"), 160)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", identity) is None:
        raise InvalidInputError("Internet Archive item identity is unsupported")
    metadata = _metadata("internet_archive", identity)
    creators = row.get("creator", [])
    metadata["authors"] = _authors([creators] if isinstance(creators, str) else _list(creators, 64))
    # Archive 'date' is the date of the work/event, not necessarily publication;
    # 'publicdate' is the catalogue item's availability, not the work's age.
    metadata["eventDate"] = _date(row.get("date"), "internet_archive:date", literal_only=True)
    metadata["updatedDate"] = _date(
        row.get("publicdate"), "internet_archive:publicdate", literal_only=True
    )
    title = row.get("title")
    if isinstance(title, list):
        titles = _list(title, 16)
        title = titles[0] if titles else ""
    description = row.get("description")
    if isinstance(description, list):
        description = "\n".join(_text(item, 16384) for item in _list(description, 16))
    return (
        "https://archive.org/details/" + quote(identity, safe=""),
        _text(title, 2048),
        _text(description, 16384, optional=True),
        metadata,
    )


def _arxiv_rows(raw: bytes) -> list[dict[str, Any]]:
    if type(raw) is not bytes or len(raw) > 262144:
        raise InvalidInputError("arXiv response exceeds its complete byte bound")
    try:
        text = raw.decode("utf-8")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("XML declarations are not permitted")
        root = ET.fromstring(text)
        if root.tag != _ATOM + "feed":
            raise ValueError("Unexpected Atom root")
        pending = [(root, 0)]
        visited = 0
        while pending:
            element, depth = pending.pop()
            visited += 1
            if visited > 4096 or depth > 32:
                raise ValueError("XML shape is excessive")
            pending.extend((child, depth + 1) for child in element)
        rows = []
        for entry in root.findall(_ATOM + "entry"):
            row: dict[str, Any] = {
                field: entry.findtext(_ATOM + field)
                for field in ("id", "title", "summary", "published", "updated")
            }
            if isinstance(row["id"], str) and "/api/errors" in row["id"]:
                raise ValueError("arXiv reported an API error")
            row["doi"] = entry.findtext(_ARXIV + "doi")
            row["authors"] = [
                author.findtext(_ATOM + "name") for author in entry.findall(_ATOM + "author")
            ]
            rows.append(row)
            if len(rows) > 100:
                raise ValueError("Too many Atom entries")
        return rows
    except (ValueError, ET.ParseError, UnicodeError, RecursionError):
        raise InvalidInputError("arXiv response is not a supported bounded Atom feed") from None


def _rows(provider: str, raw: bytes, limit: int) -> list[Any]:
    if provider == "arxiv":
        return _arxiv_rows(raw)
    payload = load_specialist_json(raw)
    if any(field in payload for field in ("error", "errors")):
        raise InvalidInputError("Specialist endpoint reported an unsupported query or response")
    if provider == "crossref":
        if payload.get("status") != "ok":
            raise InvalidInputError("Crossref did not return a successful metadata response")
        message = _object(payload.get("message"))
        rows = message.get("items") if "items" in message else [message]
    elif provider == "openalex":
        rows = payload.get("results") if "results" in payload else [payload]
    elif provider == "pubmed":
        if "esearchresult" in payload:
            if pubmed_search_ids(raw, limit):
                raise InvalidInputError("PubMed search IDs require a separately admitted summary")
            return []
        result = _object(payload.get("result"))
        ids = _list(result.get("uids"), limit)
        if (
            any(
                not isinstance(identity, str) or re.fullmatch(r"[1-9]\d{0,8}", identity) is None
                for identity in ids
            )
            or len(set(ids)) != len(ids)
            or set(result) != {"uids", *ids}
            or any(_object(result[identity]).get("uid") != identity for identity in ids)
        ):
            raise InvalidInputError("PubMed summary IDs are inconsistent")
        rows = [result[identity] for identity in ids]
    elif provider == "semantic_scholar":
        rows = payload.get("data") if "data" in payload else [payload]
    elif provider == "wikidata":
        rows = _object(payload.get("results")).get("bindings")
    else:
        header = _object(payload.get("responseHeader"))
        if type(header.get("status")) is not int or header["status"] != 0:
            raise InvalidInputError(
                "Internet Archive did not return a successful catalogue response"
            )
        rows = _object(payload.get("response")).get("docs")
    return _list(rows)


def normalize_specialist_results(
    provider: str, raw: bytes, *, retrieved_at: datetime, max_results: int
) -> dict[str, Any]:
    specialist_descriptor(provider)
    if type(max_results) is not int or not 1 <= max_results <= 20:
        raise InvalidInputError("Specialist result count is invalid")
    retrieved = _utc(retrieved_at)
    rows = _rows(provider, raw, max_results)
    normalize = {
        "crossref": _crossref,
        "openalex": _openalex,
        "arxiv": _arxiv,
        "pubmed": _pubmed,
        "semantic_scholar": _semantic_scholar,
        "wikidata": _wikidata,
        "internet_archive": _archive,
    }[provider]
    items: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "provider": provider,
        "retrievedAt": retrieved,
        "items": items,
        "rejected": rejected,
    }
    # Reserve every rejected rank up front so the final batch bound includes
    # records whose normalized metadata cannot fit.
    reserved = len(
        canonical_json(
            {
                **result,
                "rejected": [
                    {"rank": rank, "reasonCode": "normalized_result_limit"}
                    for rank in range(1, len(rows) + 1)
                ],
            }
        ).encode("utf-8")
    )
    used = 0
    from potbot.research.providers import canonical_source_url

    for rank, source in enumerate(rows, 1):
        if rank > max_results:
            rejected.append({"rank": rank, "reasonCode": "result_limit"})
            continue
        try:
            url, title, snippet, metadata = normalize(_object(source))
            if not title.strip():
                raise InvalidInputError("Specialist result title is empty")
            url = canonical_source_url(url)
            publication = metadata["publicationDate"]
            published = (
                publication["value"]
                if publication is not None and publication["precision"] == "instant"
                else None
            )
            item = {
                "canonicalUrl": url,
                "title": title,
                "snippet": snippet,
                "provider": provider,
                "rank": rank,
                "retrievedAt": retrieved,
                "publishedAt": published,
                "publicationProvenance": publication["provenance"] if published else None,
                "specialist": metadata,
            }
            length = len(canonical_json(item).encode("utf-8")) + 1
        except (DomainError, ValueError, TypeError, KeyError, OverflowError):
            rejected.append({"rank": rank, "reasonCode": "invalid_result"})
            continue
        if reserved + used + length > MAX_NORMALIZED_SPECIALIST_BYTES:
            rejected.append({"rank": rank, "reasonCode": "normalized_result_limit"})
            continue
        items.append(item)
        used += length
    return result


def normalized_identity_text(value: str) -> str:
    """Literal Unicode normalization; no fuzzy author or title inference."""
    normalized = unicodedata.normalize("NFKC", value).lower()
    return " ".join("".join(char if char.isalnum() else " " for char in normalized).split())


def same_work(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Metadata matching is only identity grouping, never corroboration."""
    if left.get("canonicalUrl") == right.get("canonicalUrl"):
        return True
    first, second = left.get("specialist"), right.get("specialist")
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    doi_a, doi_b = first.get("doi"), second.get("doi")
    if doi_a is not None and doi_b is not None:
        return isinstance(doi_a, str) and isinstance(doi_b, str) and doi_a == doi_b
    title_a = normalized_identity_text(_text(left.get("title"), 2048))
    title_b = normalized_identity_text(_text(right.get("title"), 2048))
    authors_a = sorted(
        {normalized_identity_text(value) for value in _authors(_list(first.get("authors"), 64))}
    )
    authors_b = sorted(
        {normalized_identity_text(value) for value in _authors(_list(second.get("authors"), 64))}
    )
    return (
        len(title_a) >= 12
        and title_a == title_b
        and bool(authors_a)
        and "" not in authors_a
        and authors_a == authors_b
    )


def merge_specialist_results(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every provider record; match only a pairwise-compatible group."""
    if not isinstance(batches, list) or len(batches) > 9:
        raise InvalidInputError("Specialist comparison accepts at most nine provider batches")
    from potbot.research.providers import canonical_source_url

    by_url: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for batch in batches:
        if (
            not isinstance(batch, dict)
            or not isinstance(batch.get("provider"), str)
            or batch["provider"] not in {*SPECIALIST_ENDPOINTS, "brave", "tavily"}
            or batch["provider"] in seen
            or not isinstance(batch.get("items"), list)
            or len(batch["items"]) > 20
        ):
            raise InvalidInputError("Specialist comparison requires distinct normalized batches")
        seen.add(batch["provider"])
        for item in batch["items"]:
            if not isinstance(item, dict) or item.get("provider") != batch["provider"]:
                raise InvalidInputError("Specialist source lineage is inconsistent")
            url = canonical_source_url(item.get("canonicalUrl"))
            if url not in by_url:
                by_url[url] = {
                    "id": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    "canonicalUrl": url,
                    "title": item["title"],
                    "snippet": item["snippet"],
                    "lineage": [],
                }
            by_url[url]["lineage"].append(dict(item))
    # Form exact URL buckets first: a contradictory metadata record cannot
    # create two groups with the same source identity or undo earlier URL
    # deduplication merely because it was returned by a later provider.
    groups: list[dict[str, Any]] = []
    for bucket in by_url.values():
        candidates = [
            group
            for group in groups
            if all(
                same_work(existing, item)
                for existing in group["lineage"]
                for item in bucket["lineage"]
            )
        ]
        # Do not bridge separately established identities when a DOI-less
        # record happens to match more than one of them.
        if len(candidates) == 1:
            candidates[0]["lineage"].extend(bucket["lineage"])
        else:
            groups.append(bucket)
    return groups
