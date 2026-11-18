"""Research HTTP authority, exact requests and read-only citation boundaries."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_research import MAX_BODY_BYTES, MAX_RESPONSE_BYTES, register_research_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.controller.information_needs import NeedSpec
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError


def query_body():
    return {
        "category": "documentation",
        "operation": "query_source",
        "locator": "external-research",
        "search_text": "literal Error42 v1.2",
        "required_identifiers": ["Error42", "v1.2"],
        "expected_observation": "Source text",
        "decision_effect": "Choose an implementation",
        "evidence_requirement": "Quoted documentation",
        "stopping_condition": "Source agreement",
        "fallback": "Leave unresolved",
        "target_version": "a" * 64,
        "freshness_seconds": 0,
        "max_results": 3,
        "max_source_bytes": 16384,
    }


@pytest.fixture
def api():
    actor = WorkspaceContext(uuid4(), uuid4())
    active_actor = [actor]
    episode, state, need, action, run, evidence = (uuid4() for _ in range(6))
    calls, response, setup_calls = [], [None], []
    token = "research-operator-fixture-capability"

    class Service:
        def __getattr__(self, name):
            assert name in {
                "context",
                "create_need",
                "admit",
                "queue",
                "list_runs",
                "inspect",
                "events",
                "result",
                "source",
                "cancel",
                "citation",
                "compare",
                "comparison",
            }

            def invoke(scope, *args, **kwargs):
                assert scope == actor
                calls.append((name, args, kwargs))
                return response[0] or {
                    "schema": "potbot-research-workbench/v1",
                    "scope": {
                        "workspaceId": str(actor.workspace_id),
                        "principalId": str(actor.principal_id),
                    },
                }

            return invoke

    def authenticate(value):
        if value == "Bearer ordinary":
            return active_actor[0]
        if value == "Bearer foreign":
            return WorkspaceContext(uuid4(), actor.principal_id)
        raise ScopeDeniedError("private authentication reason")

    def authorize(value):
        setup_calls.append(value)
        if value != token:
            raise ScopeDeniedError("private setup reason")

    def client(*, setup=True):
        app = FastAPI()

        @app.exception_handler(DomainError)
        async def failed(request, error):
            result = error_response(
                error,
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=hasattr(request.state, "idempotency_key"),
                ),
            )
            return JSONResponse(result.body, status_code=result.status)

        register_research_routes(
            app,
            None,
            authenticate,
            ("https://operator.example",),
            actor.workspace_id,
            authorize if setup else None,
            service=Service(),
        )
        return TestClient(app)

    spec = NeedSpec(
        "Which value?",
        "Select a supported value",
        ("one", "two"),
        ("source quote",),
        ("cited agreement",),
        "current task",
    ).to_dict()
    cite = {
        "runId": str(run),
        "sourceId": "b" * 64,
        "startByte": 0,
        "endByte": 2,
        "quoteSha256": hashlib.sha256(b"42").hexdigest(),
    }
    headers = {
        "Authorization": "Bearer ordinary",
        "Origin": "https://operator.example",
        "X-Potbot-Expected-Principal": str(actor.principal_id),
        "X-Potbot-Setup-Token": token,
        "Idempotency-Key": "research-original-key",
    }
    return SimpleNamespace(**locals())


def bodies(api):
    return {
        "needs": {
            "episodeId": str(api.episode),
            "repositoryStateId": str(api.state),
            "spec": api.spec,
            "confirm": True,
        },
        "actions": {
            "episodeId": str(api.episode),
            "repositoryStateId": str(api.state),
            "needId": str(api.need),
            "contextSha256": "c" * 64,
            "query": query_body(),
            "confirm": True,
        },
        "runs": {
            "actionId": str(api.action),
            "policySha256": "a" * 64,
            "disclosureConfirmed": True,
            "confirm": True,
        },
        "comparisons": {
            "episodeId": str(api.episode),
            "needId": str(api.need),
            "contextSha256": "c" * 64,
            "claims": [{"key": "value", "label": "Reported value", "citations": [api.cite]}],
            "confirm": True,
        },
    }


@pytest.mark.parametrize(
    "name,method",
    [("needs", "create_need"), ("actions", "admit"), ("runs", "queue"), ("comparisons", "compare")],
)
def test_exact_reviewed_mutations_preserve_keys_and_capability(api, name, method):
    response = api.client().post(
        f"/v1/ui/research/{name}", headers=api.headers, json=bodies(api)[name]
    )
    assert response.status_code == (202 if name == "runs" else 200)
    assert api.calls[0][0] == method
    assert api.calls[0][1][-1] == "research-original-key"
    assert api.calls[0][2]["setup_token"] == api.token
    assert response.headers["cache-control"] == "private, no-store"


def test_history_source_and_result_reads_need_no_setup_capability(api):
    paths = [
        f"context?episodeId={api.episode}&repositoryStateId={api.state}&needId={api.need}",
        f"runs?episodeId={api.episode}&limit=25",
        f"runs/{api.run}",
        f"runs/{api.run}/events?after=0&limit=100",
        f"runs/{api.run}/result",
        f"runs/{api.run}/sources/{'b' * 64}",
        f"comparisons/{api.evidence}",
    ]
    for path in paths:
        response = api.client(setup=False).get(
            f"/v1/ui/research/{path}", headers={"Authorization": "Bearer ordinary"}
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
    assert not api.setup_calls


def test_citation_is_an_explicit_origin_checked_read_without_key_or_setup(api):
    response = api.client(setup=False).post(
        "/v1/ui/research/citations",
        headers={
            "Authorization": "Bearer ordinary",
            "Origin": "https://operator.example",
            "X-Potbot-Expected-Principal": str(api.actor.principal_id),
        },
        json=api.cite,
    )
    assert response.status_code == 200
    assert api.calls == [("citation", (api.cite,), {})]
    assert not api.setup_calls


def test_cancellation_uses_ordinary_workspace_authority(api):
    headers = {key: value for key, value in api.headers.items() if key != "X-Potbot-Setup-Token"}
    response = api.client(setup=False).post(
        f"/v1/ui/research/runs/{api.run}/cancel", headers=headers, json={"confirm": True}
    )
    assert response.status_code == 200
    assert api.calls[0][0] == "cancel"
    assert not api.setup_calls


@pytest.mark.parametrize(
    "header,value",
    [
        ("Authorization", None),
        ("Authorization", "Bearer foreign"),
        ("Origin", None),
        ("Origin", "https://evil.example"),
        ("X-Potbot-Expected-Principal", None),
        ("X-Potbot-Expected-Principal", "not-a-principal"),
        ("X-Potbot-Setup-Token", None),
        ("X-Potbot-Setup-Token", "wrong"),
        ("Idempotency-Key", None),
        ("Idempotency-Key", "unsafe key"),
    ],
)
def test_effectful_request_without_exact_authority_never_reaches_owner(api, header, value):
    headers = dict(api.headers)
    if value is None:
        headers.pop(header)
    else:
        headers[header] = value
    response = api.client().post("/v1/ui/research/runs", headers=headers, json=bodies(api)["runs"])
    assert response.status_code >= 400
    assert not api.calls
    assert "private" not in response.text


@pytest.mark.parametrize(
    "header",
    [
        "authorization",
        "origin",
        "x-potbot-setup-token",
        "idempotency-key",
        "x-potbot-expected-principal",
    ],
)
def test_duplicate_authority_headers_are_rejected(api, header):
    headers = list(api.headers.items()) + [(header, "ambiguous")]
    response = api.client().post("/v1/ui/research/runs", headers=headers, json=bodies(api)["runs"])
    assert response.status_code >= 400
    assert not api.calls


@pytest.mark.parametrize("name", ["needs", "actions", "runs", "comparisons", "cancel", "citations"])
def test_recovered_command_cannot_execute_under_rotated_bearer_principal(api, name):
    api.active_actor[0] = WorkspaceContext(api.actor.workspace_id, uuid4())
    headers = dict(api.headers)
    if name in {"cancel", "citations"}:
        headers.pop("X-Potbot-Setup-Token")
    if name == "citations":
        headers.pop("Idempotency-Key")
        body, path = api.cite, "citations"
    elif name == "cancel":
        body, path = {"confirm": True}, f"runs/{api.run}/cancel"
    else:
        body, path = bodies(api)[name], name
    response = api.client().post(f"/v1/ui/research/{path}", headers=headers, json=body)
    assert response.status_code == 403
    assert not api.calls and not api.setup_calls


@pytest.mark.parametrize(
    "changes",
    [
        {"confirm": False},
        {"confirm": 1},
        {"disclosureConfirmed": False},
        {"query": query_body()},
        {"url": "https://other.example/"},
        {"policySha256": "A" * 64},
    ],
)
def test_run_cannot_replace_admitted_query_or_policy(api, changes):
    body = {**bodies(api)["runs"], **changes}
    response = api.client().post("/v1/ui/research/runs", headers=api.headers, json=body)
    assert response.status_code >= 400
    assert not api.calls


@pytest.mark.parametrize(
    "raw",
    [
        '{"confirm":true,"confirm":true}',
        '{"confirm":true,"amount":NaN}',
        '"not an object"',
        '{"confirm":true,"extra":"' + ("x" * MAX_BODY_BYTES) + '"}',
    ],
)
def test_ambiguous_nonfinite_or_oversized_body_is_rejected(api, raw):
    response = api.client().post(
        "/v1/ui/research/runs",
        headers={**api.headers, "Content-Type": "application/json"},
        content=raw,
    )
    assert response.status_code >= 400
    assert not api.calls


@pytest.mark.parametrize(
    "suffix",
    [
        "runs?limit=0",
        "runs?limit=26",
        "runs?limit=1&limit=2",
        "runs?unknown=x",
        "runs?after=",
        "runs?after=" + "x" * 513,
        "context",
        "context?episodeId=not-a-uuid&repositoryStateId=not-a-uuid",
    ],
)
def test_invalid_or_ambiguous_query_is_rejected(api, suffix):
    response = api.client().get(
        f"/v1/ui/research/{suffix}", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code >= 400
    assert not api.calls


def test_citation_rejects_caller_text_and_originless_posts(api):
    for headers, body in [
        ({"Authorization": "Bearer ordinary"}, api.cite),
        (api.headers, {**api.cite, "quote": "invented"}),
    ]:
        response = api.client().post("/v1/ui/research/citations", headers=headers, json=body)
        assert response.status_code >= 400
    assert not api.calls


def test_response_has_explicit_byte_bound(api):
    api.response[0] = {"text": "x" * MAX_RESPONSE_BYTES}
    response = api.client().get(
        f"/v1/ui/research/runs/{api.run}/result", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code >= 400
