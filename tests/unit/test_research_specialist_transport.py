"""Exercise actual pinned route/header checks with a synthetic final socket backend."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from potbot.research.external_transport import perform_external_request
from potbot.research.providers import search_request
from potbot.research.source_policy import ExternalResearchPolicy, ResearchProviderRule
from potbot.research.specialist import SPECIALIST_ENDPOINTS
from potbot.tools.connect_policy import ConnectPolicy

SECRET = "test-specialist-key"


class Credentials:
    def get(self, name):
        assert name.startswith("research.")
        return SECRET


@pytest.mark.parametrize("provider", ["brave", "tavily", *SPECIALIST_ENDPOINTS])
def test_fixed_wire_route_and_credential_state_pass_real_pinned_transport(provider, monkeypatch):
    selected = ExternalResearchPolicy(True, (ResearchProviderRule(provider, 0),))
    request = search_request(selected, provider, "A retained paper")
    sent, admissions, resolved = [], [], []

    def resolve(policy, authority, *, resolver):
        resolved.append(authority)
        assert authority.endswith(":443")
        return "93.184.216.34"

    async def socket_request(backend, actual):
        sent.append(actual)
        assert actual.url.host == "93.184.216.34"
        assert actual.headers["host"] + ":443" == resolved[0]
        assert actual.extensions["sni_hostname"] == actual.headers["host"]
        assert actual.method == ("GET" if provider in SPECIALIST_ENDPOINTS else "POST")
        if provider == "brave":
            assert actual.headers["x-subscription-token"] == SECRET
            assert "authorization" not in actual.headers
        elif provider == "semantic_scholar":
            assert actual.headers["x-api-key"] == SECRET
            assert "authorization" not in actual.headers
        elif provider in {"openalex", "tavily"}:
            assert actual.headers["authorization"] == "Bearer " + SECRET
        else:
            assert "authorization" not in actual.headers and "x-api-key" not in actual.headers
        assert SECRET not in str(actual.url)
        return httpx.Response(200, json={"complete": True})

    async def admit(actual):
        admissions.append(actual.fingerprint)

    monkeypatch.setattr(ConnectPolicy, "resolve", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", socket_request)
    response = asyncio.run(
        perform_external_request(request, before_send=admit, credentials=Credentials())
    )
    assert response.status == "fetched", response
    assert len(sent) == len(resolved) == 1
    assert admissions == [request.fingerprint]
    assert SECRET not in json.dumps(response.to_dict())
