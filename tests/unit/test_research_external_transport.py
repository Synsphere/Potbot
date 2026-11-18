from __future__ import annotations

import asyncio
import base64
import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.research.external_transport import perform_external_request
from potbot.research.providers import fetch_request, search_request
from potbot.research.source_policy import (
    ExternalResearchPolicy,
    ResearchFetchHostRule,
    ResearchProviderRule,
)

SECRET = "fixture-private-research-token"
NOW = datetime(2026, 11, 18, tzinfo=UTC)


class Credentials:
    def __init__(self, secret=SECRET):
        self.secret = secret
        self.names = []

    def get(self, name):
        self.names.append(name)
        return self.secret


def policy():
    return ExternalResearchPolicy(
        allow_query_disclosure=True,
        providers=(ResearchProviderRule("brave", 10), ResearchProviderRule("tavily", 20)),
        fetch_hosts=(ResearchFetchHostRule("docs.example.com", True),),
    )


def run(request, handler, *, before=None, credentials=None):
    observed = []

    async def admit(selected):
        observed.append(selected.fingerprint)
        if before is not None:
            await before(selected)

    result = asyncio.run(
        perform_external_request(
            request,
            before_send=admit,
            credentials=credentials or Credentials(),
            transport_factory=lambda selected, authenticated: httpx.MockTransport(handler),
            clock=lambda: NOW,
        )
    )
    return result, observed


@pytest.mark.parametrize("provider", ["brave", "tavily"])
def test_exact_provider_send_requires_callback_and_brokers_only_its_credential(
    provider, monkeypatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://not-approved.invalid:9999")
    request = search_request(policy(), provider, "explicit query")
    credentials = Credentials()
    sent = []

    def handler(actual):
        sent.append(actual)
        assert actual.method == "POST" and str(actual.url) == request.url
        assert actual.content == request.body
        assert actual.headers["accept-encoding"] == "identity"
        assert "cookie" not in actual.headers and "proxy-authorization" not in actual.headers
        if provider == "brave":
            assert actual.headers["x-subscription-token"] == SECRET
            assert "authorization" not in actual.headers
        else:
            assert actual.headers["authorization"] == "Bearer " + SECRET
        return httpx.Response(200, json={"results": []})

    result, admissions = run(request, handler, credentials=credentials)
    assert len(sent) == 1 and admissions == [request.fingerprint]
    assert credentials.names == ["research." + provider]
    assert result.status == "fetched" and result.request_sent
    assert result.data == b'{"results":[]}'
    assert SECRET not in json.dumps(result.to_dict())


def test_denied_pre_send_admission_produces_zero_network_calls() -> None:
    sent = []

    async def deny(request):
        raise ScopeDeniedError("stale lease")

    with pytest.raises(ScopeDeniedError, match="stale lease"):
        run(
            search_request(policy(), "brave", "query"),
            lambda request: sent.append(request),
            before=deny,
        )
    assert sent == []


@pytest.mark.parametrize("secret", [None, "", "bad\r\nheader", "unicode-🔑"])
def test_missing_or_invalid_credential_has_no_admission_or_network_effect(secret) -> None:
    effects = []

    async def admit(request):
        effects.append("admission")

    with pytest.raises(CapabilityUnavailableError):
        asyncio.run(
            perform_external_request(
                search_request(policy(), "brave", "query"),
                before_send=admit,
                credentials=Credentials(secret),
                transport_factory=lambda request, authenticated: httpx.MockTransport(
                    lambda req: effects.append("send")
                ),
            )
        )
    assert effects == []


def test_fetch_is_unauthenticated_and_cannot_forward_search_credential() -> None:
    credentials = Credentials()

    def handler(request):
        assert (
            "authorization" not in request.headers and "x-subscription-token" not in request.headers
        )
        return httpx.Response(200, text="source")

    result, _ = run(
        fetch_request(policy(), "https://docs.example.com/source"), handler, credentials=credentials
    )
    assert result.data == b"source" and credentials.names == []


@pytest.mark.parametrize(
    "code,status",
    [
        (301, "redirect_denied"),
        (307, "redirect_denied"),
        (429, "rate_limited"),
        (432, "quota_exhausted"),
        (433, "quota_exhausted"),
        (500, "unavailable"),
        (401, "unavailable"),
    ],
)
def test_error_statuses_are_bounded_terminal_observations_without_redirect_or_retry(
    code, status
) -> None:
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(
            code, headers={"Location": "http://127.0.0.1/secret"}, content=b"private error body"
        )

    request = search_request(policy(), "tavily", "query")
    result, admitted = run(request, handler)
    assert result.status == status and result.data is None
    assert len(sent) == len(admitted) == 1
    assert "private error" not in json.dumps(result.to_dict())
    assert "127.0.0.1" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    "headers,body,status,reason",
    [
        ({"Content-Length": "999999"}, b"small", "over_limit", "response_byte_limit"),
        ({"Content-Length": "invalid"}, b"small", "unavailable", "response_length_invalid"),
        ({"Content-Length": "99"}, b"small", "unavailable", "response_length_mismatch"),
        (
            {"Content-Encoding": "gzip"},
            gzip.compress(b"expanded" * 10000),
            "unavailable",
            "encoded_response_denied",
        ),
    ],
)
def test_response_length_and_compression_cannot_bypass_source_limits(
    headers, body, status, reason
) -> None:
    result, _ = run(
        fetch_request(policy(), "https://docs.example.com/source"),
        lambda request: httpx.Response(200, headers=headers, content=body),
    )
    assert result.status == status and result.reason_codes == (reason,) and result.data is None


def test_streamed_oversize_never_publishes_a_truncated_source() -> None:
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 20
            yield b"b" * 20

    request = replace(fetch_request(policy(), "https://docs.example.com/source"), max_bytes=30)
    result, _ = run(request, lambda req: httpx.Response(200, stream=Chunks()))
    assert result.status == "over_limit" and result.received_bytes == 40 and result.data is None


def test_empty_response_media_type_is_recorded_as_unavailable_after_one_admitted_send() -> None:
    result, admissions = run(
        fetch_request(policy(), "https://docs.example.com/source"),
        lambda request: httpx.Response(200, headers={"Content-Type": ""}, content=b"body"),
    )
    assert result.status == "unavailable" and result.reason_codes == (
        "response_media_type_invalid",
    )
    assert result.media_type is None and result.data is None and len(admissions) == 1


@pytest.mark.parametrize("body", [SECRET.encode(), base64.b64encode(SECRET.encode())])
def test_provider_secret_reflection_never_reaches_artifacts_or_receipts(body) -> None:
    result, _ = run(
        search_request(policy(), "brave", "query"), lambda req: httpx.Response(200, content=body)
    )
    assert result.data is None and result.reason_codes == ("credential_reflection_denied",)
    assert SECRET not in repr(result.to_dict())


@pytest.mark.parametrize(
    "error,status",
    [
        (httpx.ReadTimeout("private endpoint detail"), "timed_out"),
        (httpx.ReadError("private endpoint detail"), "indeterminate"),
        (ScopeDeniedError("mixed DNS answer"), "unavailable"),
    ],
)
def test_ambiguous_network_outcomes_are_not_retried_or_promoted_to_success(error, status) -> None:
    sent = []

    def handler(request):
        sent.append(request)
        raise error

    result, admitted = run(search_request(policy(), "brave", "query"), handler)
    assert result.status == status and result.data is None and result.request_sent
    assert len(sent) == len(admitted) == 1
    assert "private endpoint detail" not in repr(result)


def test_request_duration_excludes_controller_rate_wait(monkeypatch) -> None:
    readings = iter([0, 60000000000, 60007000000])
    monkeypatch.setattr(
        "potbot.research.external_transport.time.monotonic_ns", lambda: next(readings)
    )
    result, _ = run(
        fetch_request(policy(), "https://docs.example.com/source"),
        lambda req: httpx.Response(200, text="ok"),
    )
    assert result.elapsed_ms == 7
