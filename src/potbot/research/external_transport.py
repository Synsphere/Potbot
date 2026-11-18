"""One explicitly admitted research request through the existing pinned HTTPS owner.

No retries, fallback, redirects, cookies, proxy environment or automatic source
fetches occur here. The durable research worker owns those decisions and must
provide a live pre-send admission callback. Failed responses never publish
partial bytes as source evidence; uncertain sends remain explicitly uncertain.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

import httpx

from potbot.credentials import CredentialStore, credential_profile
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.research.external_contracts import (
    RESEARCH_USER_AGENT,
    ExternalRequest,
    ExternalResponse,
)
from potbot.tools.pinned_https import PinnedHttpsTransport
from potbot.tools.secrets import CaptureRedactor

_CREDENTIALS = {
    "brave": ("research.brave", "POTBOT_BRAVE_SEARCH_API_KEY"),
    "tavily": ("research.tavily", "POTBOT_TAVILY_API_KEY"),
    "openalex": ("research.openalex", "POTBOT_OPENALEX_API_KEY"),
    "semantic_scholar": ("research.semantic_scholar", "POTBOT_SEMANTIC_SCHOLAR_API_KEY"),
}


class ResearchCredentials(Protocol):
    def get(self, name: str) -> str | None: ...


TransportFactory = Callable[[ExternalRequest, bool], httpx.AsyncBaseTransport]


def _credential(
    provider: str, credentials: ResearchCredentials | None, *, optional: bool = False
) -> str | None:
    name, environment = _CREDENTIALS[provider]
    if credentials is not None:
        secret = credentials.get(name)
    else:
        secret = os.environ.get(environment)
        if secret is None:
            secret = CredentialStore(credential_profile()).get(name)
    if secret is None:
        if optional:
            return None
        raise CapabilityUnavailableError("The selected research provider credential is unavailable")
    if (
        not isinstance(secret, str)
        or not 1 <= len(secret) <= 4096
        or any(not 33 <= ord(char) <= 126 for char in secret)
    ):
        raise CapabilityUnavailableError("The selected research provider credential is invalid")
    return secret


async def perform_external_request(
    request: ExternalRequest,
    *,
    before_send: Callable[[ExternalRequest], Awaitable[None]],
    credentials: ResearchCredentials | None = None,
    transport_factory: TransportFactory | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ExternalResponse:
    """Prepare one exact request, admit it, then send once and bound all output.

    ``transport_factory`` and ``credentials`` are trusted constructor seams for
    deterministic tests, never public request fields. Production always uses
    PinnedHttpsTransport's exact route/public DNS/numeric dial/TLS boundary.
    The callback is awaited after credential resolution and before network I/O;
    its exception propagates without sending or manufacturing a response.
    """
    if type(request) is not ExternalRequest or not callable(before_send):
        raise InvalidInputError(
            "Research transport requires an exact request and controller admission"
        )
    selected_clock = clock or (lambda: datetime.now(UTC))
    started = time.monotonic_ns()
    sent = False
    received = 0
    status_code: int | None = None
    media_type: str | None = None

    def result(status: str, reason: str, data: bytes | None = None) -> ExternalResponse:
        return ExternalResponse(
            request.fingerprint,
            status,
            status_code,
            data,
            media_type,
            selected_clock(),
            received,
            max(0, (time.monotonic_ns() - started) // 1000000),
            sent,
            (reason,),
        )

    headers = {
        "Accept": "application/json" if request.kind == "search" else "*/*",
        "Accept-Encoding": "identity",
        "User-Agent": RESEARCH_USER_AGENT,
    }
    secret: str | None = None
    from potbot.research.specialist import SPECIALIST_ENDPOINTS, specialist_wire_request

    wire_method, wire_url, wire_body = request.method, request.url, request.body
    if request.provider in SPECIALIST_ENDPOINTS:
        if request.provider in _CREDENTIALS:
            secret = _credential(request.provider, credentials, optional=True)
        wire_method, wire_url, wire_body, provider_headers = specialist_wire_request(
            request.provider,
            request.body,
            credential=secret,
        )
        headers.update(provider_headers)
    elif request.provider is not None:
        secret = _credential(request.provider, credentials)
        assert secret is not None
        if request.provider == "brave":
            headers["X-Subscription-Token"] = secret
        else:
            headers["Authorization"] = "Bearer " + secret
        headers["Content-Type"] = "application/json"
    # PinnedHttpsTransport's flag binds the Authorization header specifically;
    # provider-specific API-key headers are independently fixed above.
    authenticated = "Authorization" in headers
    transport = (
        transport_factory(request, authenticated)
        if transport_factory is not None
        else PinnedHttpsTransport(
            wire_method,
            wire_url,
            authenticated=authenticated,
            allow_query=request.kind == "fetch" or request.provider in SPECIALIST_ENDPOINTS,
        )
    )
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=request.timeout_seconds,
        headers=headers,
    ) as client:
        await before_send(request)
        started = time.monotonic_ns()
        try:
            async with asyncio.timeout(request.timeout_seconds):
                sent = True
                async with client.stream(
                    wire_method,
                    wire_url,
                    content=wire_body,
                    follow_redirects=False,
                ) as response:
                    status_code = response.status_code
                    raw_type = response.headers.get("content-type", "application/octet-stream")
                    if (
                        not raw_type.strip()
                        or len(raw_type) > 256
                        or any(not 32 <= ord(c) <= 126 for c in raw_type)
                    ):
                        return result("unavailable", "response_media_type_invalid")
                    media_type = raw_type
                    if 300 <= status_code < 400:
                        return result("redirect_denied", "redirect_not_admitted")
                    if status_code == 429:
                        return result("rate_limited", "provider_rate_limited")
                    if status_code == 402 or (
                        request.provider == "tavily" and status_code in {432, 433}
                    ):
                        return result("quota_exhausted", "provider_quota_exhausted")
                    if not 200 <= status_code < 300:
                        return result("unavailable", "response_not_successful")
                    if (
                        response.headers.get("content-encoding", "identity").strip().lower()
                        != "identity"
                    ):
                        return result("unavailable", "encoded_response_denied")
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        if not declared.isascii() or not declared.isdecimal() or len(declared) > 12:
                            return result("unavailable", "response_length_invalid")
                        if int(declared) > request.max_bytes:
                            return result("over_limit", "response_byte_limit")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        received += len(chunk)
                        if received > request.max_bytes:
                            return result("over_limit", "response_byte_limit")
                        data.extend(chunk)
                    if declared is not None and int(declared) != received:
                        return result("unavailable", "response_length_mismatch")
                    complete = bytes(data)
                    if secret is not None and any(
                        pattern in complete for pattern in CaptureRedactor((secret,)).patterns
                    ):
                        return result("unavailable", "credential_reflection_denied")
                    return result("fetched", "complete_response", complete)
        except (TimeoutError, httpx.TimeoutException):
            return result("timed_out", "request_deadline")
        except ScopeDeniedError:
            return result("unavailable", "egress_policy_denied")
        except (httpx.HTTPError, OSError):
            return result("indeterminate", "request_outcome_unknown")
