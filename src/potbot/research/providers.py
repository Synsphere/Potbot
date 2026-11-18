"""Fixed search adapters and deterministic result/source-location helpers.

Provider wire contracts were checked against their primary documentation:
https://api-dashboard.search.brave.com/api-reference/web/search/post
https://docs.tavily.com/documentation/api-reference/endpoint/search

No provider result is a verified source citation. Fetch authority comes from the
configured exact host policy and separately admitted request, never a result URL.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.research.external_contracts import (
    MAX_RESPONSE_BYTES,
    PROVIDER_ENDPOINTS,
    RESEARCH_USER_AGENT,
    ExternalRequest,
    utc_text,
)
from potbot.research.source_policy import ExternalResearchPolicy
from potbot.tools.pinned_https import canonical_https_host

MAX_NORMALIZED_SEARCH_BYTES = 100000


def provider_descriptor(provider: str) -> dict[str, Any]:
    if not isinstance(provider, str) or provider not in PROVIDER_ENDPOINTS:
        raise InvalidInputError("Research provider is unsupported")
    return {
        "id": provider,
        "method": "POST",
        "endpoint": PROVIDER_ENDPOINTS[provider],
        "adapterRevision": 1,
        "mode": "web_search",
        "scope": "explicit_query_disclosure",
    }


def search_request(policy: ExternalResearchPolicy, provider: str, query: str) -> ExternalRequest:
    policy.require_provider(provider)
    try:
        query_bytes = query.encode("utf-8") if isinstance(query, str) else b""
    except UnicodeError:
        raise InvalidInputError("Search query must contain valid UTF-8 text") from None
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 600
        or len(query_bytes) > 4096
        or len(query.split()) > 75
        or any(ord(char) < 32 or ord(char) == 127 for char in query)
    ):
        raise InvalidInputError("Search query must contain at most 600 characters and 75 words")
    if provider == "brave":
        body: dict[str, Any] = {
            "q": query,
            "count": policy.max_results,
            "spellcheck": False,
            "text_decorations": False,
            "result_filter": ["web"],
        }
    else:
        body = {
            "query": query,
            "max_results": policy.max_results,
            "search_depth": "basic",
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_published_date": True,
            "include_usage": True,
            "auto_parameters": False,
        }
    return ExternalRequest(
        "search",
        provider,
        "POST",
        PROVIDER_ENDPOINTS[provider],
        canonical_json(body).encode("utf-8"),
        policy.max_source_bytes,
        policy.timeout_seconds,
    )


def canonical_source_url(value: object) -> str:
    """Normalize an observed HTTPS location without changing query semantics."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InvalidInputError("Research source URL is invalid")
    try:
        split = urlsplit(value)
        if split.username is not None or split.password is not None:
            raise ValueError
        normalized = str(
            httpx.URL(urlunsplit((split.scheme, split.netloc, split.path or "/", split.query, "")))
        )
        canonical_https_host(normalized, allow_query=True)
        return normalized
    except (ValueError, httpx.InvalidURL):
        raise InvalidInputError("Research source requires a canonical public HTTPS URL") from None


def robots_request(policy: ExternalResearchPolicy, url: str) -> ExternalRequest:
    rule = policy.require_fetch_url(url)
    return ExternalRequest(
        "robots",
        None,
        "GET",
        f"https://{rule.host}/robots.txt",
        b"",
        min(policy.max_source_bytes, 65536),
        policy.timeout_seconds,
    )


def fetch_request(policy: ExternalResearchPolicy, url: str) -> ExternalRequest:
    policy.require_fetch_url(url)
    return ExternalRequest(
        "fetch",
        None,
        "GET",
        url,
        b"",
        policy.max_source_bytes,
        policy.timeout_seconds,
    )


def robots_permits(raw: bytes, target_url: str) -> bool:
    """Replay one complete successful robots response; absence never grants access.

    The lifecycle must prove the bytes came from the exact host's successful
    robots request. Crawl-delay/request-rate constraints are returned separately
    by ``robots_interval_ms`` and must be enforced by the per-send controller.
    """
    groups, target = _robots(raw, target_url)
    matched = [
        (len(pattern.replace("*", "").removesuffix("$")), allowed)
        for rules, _ in groups
        for pattern, allowed in rules
        if _robots_matches(pattern, target)
    ]
    return max(matched)[1] if matched else True


def robots_interval_ms(raw: bytes, target_url: str) -> int:
    groups, _ = _robots(raw, target_url)
    return max((interval for _, interval in groups), default=0)


def _robots_matches(pattern: str, target: str) -> bool:
    """Bounded literal segment matching, avoiding adversarial regular expressions."""
    exact = pattern.endswith("$")
    pieces = pattern.removesuffix("$").split("*")
    if not target.startswith(pieces[0]):
        return False
    cursor = len(pieces[0])
    if len(pieces) == 1:
        return cursor == len(target) if exact else True
    for part in pieces[1:-1]:
        found = target.find(part, cursor)
        if found < 0:
            return False
        cursor = found + len(part)
    last = pieces[-1]
    if exact:
        return len(target) - len(last) >= cursor and target.endswith(last)
    return target.find(last, cursor) >= 0


def _robots_path(value: str, *, pattern: bool) -> str:
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    output, offset = [], 0
    while offset < len(value):
        char = value[offset]
        if char == "%":
            token = value[offset + 1 : offset + 3]
            if len(token) != 2 or any(c not in "0123456789abcdefABCDEF" for c in token):
                raise InvalidInputError("Robots path has invalid percent encoding")
            decoded = chr(int(token, 16))
            output.append(decoded if decoded in unreserved else "%" + token.upper())
            offset += 3
            continue
        if ord(char) > 126 or char == " " or (not pattern and char in "*$"):
            output.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            output.append(char)
        offset += 1
    return "".join(output)


def _robots(
    raw: bytes,
    target_url: str,
) -> tuple[list[tuple[list[tuple[str, bool]], int]], str]:
    """Conservative bounded RFC 9309 groups, escapes and wildcard matching.

    HTTP error/redirect policy is deliberately stricter than the RFC: the
    lifecycle requires a successful robots response before this parser runs.
    https://www.rfc-editor.org/rfc/rfc9309.html
    """
    canonical_https_host(target_url, allow_query=True)
    if type(raw) is not bytes or len(raw) > 65536:
        raise InvalidInputError("Robots source exceeds its byte allowance")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidInputError("Robots source must be complete UTF-8 text") from None
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise InvalidInputError("Robots source contains unsupported controls")
    # Do not mistake an HTML error/interstitial for an empty robots policy.
    if "<" in value or ">" in value:
        raise InvalidInputError("Robots source is not a supported plain-text policy")
    groups: list[tuple[list[str], list[tuple[str, bool]], int]] = []
    agents: list[str] = []
    rules: list[tuple[str, bool]] = []
    interval, has_rules = 0, False
    meaningful, recognized = False, False
    lines = value.lstrip("\ufeff").splitlines()
    if len(lines) > 4096:
        raise InvalidInputError("Robots source has too many records")
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        meaningful = True
        if len(line) > 4096 or ":" not in line:
            raise InvalidInputError("Robots source has an unsupported record")
        name, entry = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            recognized = True
            if has_rules:
                groups.append((agents, rules, interval))
                agents, rules, interval, has_rules = [], [], 0, False
            if not re.fullmatch(r"[A-Za-z_-]{1,128}|\*", entry):
                raise InvalidInputError("Robots user-agent token is invalid")
            agents.append(entry.lower())
        elif name in {"allow", "disallow"} and agents:
            has_rules = True
            if entry:
                if not entry.startswith("/") or "$" in entry.removesuffix("$"):
                    raise InvalidInputError("Robots path rule is unsupported")
                rules.append((_robots_path(entry, pattern=True), name == "allow"))
        elif name in {"crawl-delay", "request-rate"} and agents:
            has_rules = True
            if name == "crawl-delay" and re.fullmatch(r"\d{1,7}(?:\.\d{1,3})?", entry):
                whole, _, fraction = entry.partition(".")
                delay = int(whole) * 1000 + int((fraction + "000")[:3])
            elif name == "request-rate" and re.fullmatch(r"\d{1,7}/\d{1,7}", entry):
                requests, seconds = map(int, entry.split("/"))
                if requests < 1 or seconds < 1:
                    raise InvalidInputError("Robots request rate must be positive")
                delay = (seconds * 1000 + requests - 1) // requests
            else:
                raise InvalidInputError("Robots timing record is unsupported")
            if delay > 3600000:
                raise InvalidInputError("Source robots rate exceeds the supported execution window")
            interval = max(interval, delay)
        elif name == "sitemap" and entry.startswith(("http://", "https://")):
            # Sitemap locations are retained as inert robots syntax only;
            # this profile never fetches or follows them.
            recognized = True
    if meaningful and not recognized:
        raise InvalidInputError("Robots source contains no supported policy records")
    if agents:
        groups.append((agents, rules, interval))
    token = RESEARCH_USER_AGENT.split("/", 1)[0].lower()
    selected = [(rules, interval) for agents, rules, interval in groups if token in agents]
    if not selected:
        selected = [(rules, interval) for agents, rules, interval in groups if "*" in agents]
    parts = urlsplit(target_url)
    target = _robots_path(parts.path + ("?" + parts.query if parts.query else ""), pattern=False)
    return selected, target


def _text(value: object, maximum: int, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > maximum
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise InvalidInputError("Provider result text is invalid or excessive")
    return value


def _published(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            # A publication date is not an instant. Do not invent midnight or
            # a time zone for freshness; the raw source retains the exact date.
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object field")
        value[key] = item
    return value


def normalize_search_results(
    provider: str,
    raw: bytes,
    *,
    retrieved_at: datetime,
    max_results: int,
) -> dict[str, Any]:
    provider_descriptor(provider)
    retrieved = utc_text(retrieved_at)
    if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
        raise InvalidInputError("Provider response exceeds the supported byte bound")
    if type(max_results) is not int or not 1 <= max_results <= 20:
        raise InvalidInputError("Provider result count is outside its supported bound")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError
        if provider == "brave":
            section = payload.get("web")
            if section is None and isinstance(payload.get("query"), dict):
                rows: Any = []
            elif isinstance(section, dict):
                rows = section.get("results")
            else:
                raise ValueError
        else:
            rows = payload.get("results")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidInputError(
            "Provider response does not match its supported result schema"
        ) from None
    items, rejected = [], []
    # Reserve enough room to explain every rejected rank, including later rows.
    # Full raw responses remain immutable source artifacts; display normalization
    # cannot amplify one small provider response into an unreviewable API body.
    reserved = len(
        canonical_json(
            {
                "provider": provider,
                "retrievedAt": retrieved,
                "items": [],
                "rejected": [
                    {"rank": rank, "reasonCode": "normalized_result_limit"}
                    for rank in range(1, len(rows) + 1)
                ],
            }
        ).encode("utf-8")
    )
    item_bytes = 0
    for rank, row in enumerate(rows, 1):
        if rank > max_results:
            rejected.append({"rank": rank, "reasonCode": "result_limit"})
            continue
        try:
            if not isinstance(row, dict):
                raise InvalidInputError("Provider result requires an object")
            url = canonical_source_url(row.get("url"))
            title = _text(row.get("title"), 2048)
            snippet = _text(
                row.get("description" if provider == "brave" else "content"), 16384, optional=True
            )
            published = _published(row.get("page_age" if provider == "brave" else "published_date"))
        except (DomainError, ValueError, TypeError):
            rejected.append({"rank": rank, "reasonCode": "invalid_result"})
            continue
        item = {
            "canonicalUrl": url,
            "title": title,
            "snippet": snippet,
            "provider": provider,
            "rank": rank,
            "retrievedAt": retrieved,
            "publishedAt": published,
            "publicationProvenance": (
                "provider_reported_published_or_modified"
                if provider == "brave"
                else "provider_reported"
            )
            if published
            else None,
        }
        length = len(canonical_json(item).encode("utf-8")) + 1
        if reserved + item_bytes + length > MAX_NORMALIZED_SEARCH_BYTES:
            rejected.append({"rank": rank, "reasonCode": "normalized_result_limit"})
            continue
        items.append(item)
        item_bytes += length
    return {"provider": provider, "retrievedAt": retrieved, "items": items, "rejected": rejected}


def merge_search_results(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate exact canonical URLs while preserving each provider/rank record."""
    if not isinstance(batches, list) or len(batches) > 2:
        raise InvalidInputError("Search comparison requires at most two supported provider batches")
    merged: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for batch in batches:
        if (
            not isinstance(batch, dict)
            or not isinstance(batch.get("provider"), str)
            or batch.get("provider") not in PROVIDER_ENDPOINTS
            or batch["provider"] in seen
            or not isinstance(batch.get("items"), list)
            or len(batch["items"]) > 20
        ):
            raise InvalidInputError(
                "Search comparison requires distinct normalized provider batches"
            )
        seen.add(batch["provider"])
        for item in batch["items"]:
            if not isinstance(item, dict) or item.get("provider") != batch["provider"]:
                raise InvalidInputError("Search result lineage is inconsistent")
            url = canonical_source_url(item.get("canonicalUrl"))
            if url not in merged:
                merged[url] = {
                    "id": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    "canonicalUrl": url,
                    "title": item["title"],
                    "snippet": item["snippet"],
                    "lineage": [],
                }
            merged[url]["lineage"].append(dict(item))
    return list(merged.values())
