"""Bounded internal request/response values for controller-owned research.

These values carry no execution authority and never contain credentials. The
research worker must admit each exact request through its live lease before the
transport sends it. Provider responses and downloaded documents remain data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.tools.pinned_https import canonical_https_host

PROVIDER_ENDPOINTS = {
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "tavily": "https://api.tavily.com/search",
}
RESEARCH_USER_AGENT = "PotbotResearch/1"
MAX_RESPONSE_BYTES = 262144
RESPONSE_STATUSES = frozenset(
    {
        "fetched",
        "rate_limited",
        "quota_exhausted",
        "redirect_denied",
        "over_limit",
        "unavailable",
        "timed_out",
        "indeterminate",
    }
)


def utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidInputError("Research timestamps require an explicit time zone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ExternalRequest:
    kind: str
    provider: str | None
    method: str
    url: str
    body: bytes
    max_bytes: int = MAX_RESPONSE_BYTES
    timeout_seconds: int = 15

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, str)
            or self.kind not in {"search", "robots", "fetch"}
            or type(self.body) is not bytes
            or (self.provider is not None and not isinstance(self.provider, str))
        ):
            raise InvalidInputError("Research request kind is unsupported")
        canonical_https_host(self.url, allow_query=self.kind == "fetch")
        if self.kind == "search":
            if (
                self.provider not in PROVIDER_ENDPOINTS
                or self.method != "POST"
                or self.url != PROVIDER_ENDPOINTS[self.provider]
                or type(self.body) is not bytes
                or not 1 <= len(self.body) <= 8192
            ):
                raise InvalidInputError(
                    "Research search requires the exact supported provider route"
                )
        elif self.provider is not None or self.method != "GET" or self.body != b"":
            raise InvalidInputError("Document research requires an unauthenticated GET")
        if self.kind == "robots" and urlsplit(self.url).path != "/robots.txt":
            raise InvalidInputError("Robots research requires the exact host robots route")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_RESPONSE_BYTES:
            raise InvalidInputError("Research response byte allowance is outside its bound")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 30:
            raise InvalidInputError("Research request deadline is outside its bound")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "potbot-external-research-request/v1",
            "kind": self.kind,
            "provider": self.provider,
            "method": self.method,
            "url": self.url,
            "bodySha256": hashlib.sha256(self.body).hexdigest(),
            "maxBytes": self.max_bytes,
            "timeoutSeconds": self.timeout_seconds,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class ExternalResponse:
    request_sha256: str
    status: str
    http_status: int | None
    data: bytes | None
    media_type: str | None
    retrieved_at: datetime
    received_bytes: int
    elapsed_ms: int
    request_sent: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_sha256, str)
            or len(self.request_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.request_sha256)
            or not isinstance(self.status, str)
            or self.status not in RESPONSE_STATUSES
            or (
                self.http_status is not None
                and (type(self.http_status) is not int or not 100 <= self.http_status <= 599)
            )
            or type(self.received_bytes) is not int
            or self.received_bytes < 0
            or type(self.elapsed_ms) is not int
            or self.elapsed_ms < 0
            or type(self.request_sent) is not bool
        ):
            raise InvalidInputError("Research transport response fields are invalid")
        utc_text(self.retrieved_at)
        if self.media_type is not None and (
            not isinstance(self.media_type, str)
            or not self.media_type
            or len(self.media_type) > 256
            or any(ord(char) < 32 or ord(char) > 126 for char in self.media_type)
        ):
            raise InvalidInputError("Research response media type is invalid")
        if (
            type(self.reason_codes) is not tuple
            or len(self.reason_codes) > 16
            or any(
                not isinstance(code, str)
                or not 1 <= len(code) <= 96
                or not code[0].islower()
                or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in code)
                for code in self.reason_codes
            )
        ):
            raise InvalidInputError("Research response reason codes are invalid")
        if self.status == "fetched":
            if (
                type(self.data) is not bytes
                or len(self.data) != self.received_bytes
                or len(self.data) > MAX_RESPONSE_BYTES
                or not self.request_sent
                or self.http_status is None
                or not 200 <= self.http_status < 300
            ):
                raise InvalidInputError("Fetched research bytes require a complete exact response")
        elif self.data is not None:
            raise InvalidInputError("Incomplete research responses cannot publish source bytes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "potbot-external-research-response/v1",
            "requestSha256": self.request_sha256,
            "status": self.status,
            "httpStatus": self.http_status,
            "sourceSha256": hashlib.sha256(self.data).hexdigest()
            if self.data is not None
            else None,
            "mediaType": self.media_type,
            "retrievedAt": utc_text(self.retrieved_at),
            "receivedBytes": self.received_bytes,
            "elapsedMs": self.elapsed_ms,
            "requestSent": self.request_sent,
            "reasonCodes": list(self.reason_codes),
        }
