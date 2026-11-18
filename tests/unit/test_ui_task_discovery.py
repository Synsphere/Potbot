"""Exact authenticated browser commands cannot execute or upload native scores."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_task_discovery import MAX_RESPONSE_BYTES, register_task_discovery_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError

BASE = "/v1/ui/task-discovery"


def creation():
    return {
        "episodeId": str(uuid4()),
        "stateId": str(uuid4()),
        "contextSha256": "a" * 64,
        "query": "Find a database reader",
        "constraints": {},
        "mode": "lexical",
        "limit": 5,
        "embedding": None,
        "confirm": True,
    }


@pytest.fixture
def api():
    context, foreign = WorkspaceContext(uuid4(), uuid4()), WorkspaceContext(uuid4(), uuid4())
    calls, response_value = [], [None]
    token = "private-task-discovery-setup-canary-" + "x" * 40
    allowed = {
        "context",
        "searches",
        "search",
        "result",
        "selection",
        "inspect_candidate",
        "create",
        "select",
        "cancel",
    }

    class Service:
        def __getattr__(self, name):
            assert name in allowed, "Browser cannot address native execution or score publication"

            def invoke(actor, *args, **kwargs):
                assert actor == context
                calls.append((name, args, kwargs))
                return response_value[0] or {
                    "schema": "potbot-task-discovery/v1",
                    "workspaceId": str(actor.workspace_id),
                    "principalId": str(actor.principal_id),
                    "items": [],
                    "nextCursor": None,
                }

            return invoke

    def authenticate(value):
        if value == "Bearer ordinary":
            return context
        if value == "Bearer foreign":
            return foreign
        raise ScopeDeniedError("private authentication failure canary")

    def authorize(value):
        if value != token:
            raise ScopeDeniedError("private setup failure canary")

    def client(*, setup=True):
        app = FastAPI()

        @app.exception_handler(DomainError)
        async def failure(request, error):
            response = error_response(
                error,
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=hasattr(request.state, "idempotency_key"),
                ),
            )
            return JSONResponse(response.body, status_code=response.status)

        register_task_discovery_routes(
            app,
            None,
            authenticate,
            ("https://operator.example",),
            context.workspace_id,
            authorize if setup else None,
            service=Service(),
        )
        return TestClient(app)

    return SimpleNamespace(
        context=context,
        calls=calls,
        client=client,
        response_value=response_value,
        token=token,
        headers={
            "Authorization": "Bearer ordinary",
            "Origin": "https://operator.example",
            "X-Potbot-Setup-Token": token,
            "Idempotency-Key": "reviewed-task-discovery",
        },
    )


@pytest.mark.parametrize(
    "path,owner",
    [
        ("searches", "searches"),
        ("searches/{id}", "search"),
        ("results/{id}", "result"),
        ("selections/{id}", "selection"),
        ("searches/{id}/candidates/" + "b" * 64, "inspect_candidate"),
    ],
)
def test_reads_are_private_and_never_address_native_execution_or_setup(api, path, owner):
    response = api.client(setup=False).get(
        f"{BASE}/{path.format(id=uuid4())}", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code == 200
    assert [call[0] for call in api.calls] == [owner]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["vary"] == "Authorization, Origin"
    assert api.token not in response.text


def test_context_and_page_preserve_exact_task_state_and_cursor(api):
    episode, state, after = uuid4(), uuid4(), uuid4()
    client = api.client(setup=False)
    assert (
        client.get(
            f"{BASE}/context",
            params={"episodeId": str(episode), "stateId": str(state)},
            headers={"Authorization": "Bearer ordinary"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"{BASE}/searches",
            params={"episodeId": str(episode), "limit": "12", "after": str(after)},
            headers={"Authorization": "Bearer ordinary"},
        ).status_code
        == 200
    )
    assert api.calls == [
        ("context", (episode, state), {}),
        ("searches", (), {"episode_id": episode, "limit": 12, "after": str(after)}),
    ]


def test_creation_preserves_original_body_and_separate_command_authority(api):
    body = creation()
    response = api.client().post(f"{BASE}/searches", json=body, headers=api.headers)
    assert response.status_code == 200
    assert api.calls == [("create", (body, "reviewed-task-discovery"), {"setup_token": api.token})]
    assert body["constraints"] == {}  # Normalization must not replace the original command bytes.
    assert api.token not in response.text


@pytest.mark.parametrize("action", ["cancel", "select"])
def test_reviewed_commands_preserve_exact_revision_digest_and_candidate(api, action):
    identity = uuid4()
    body = {"revision": 3, "digest": "a" * 64, "confirm": True}
    values = (identity, 3, "a" * 64)
    if action == "select":
        body["candidateId"] = "b" * 64
        values += ("b" * 64,)
    response = api.client().post(
        f"{BASE}/searches/{identity}/{action}", json=body, headers=api.headers
    )
    assert response.status_code == 200
    assert api.calls == [(action, (*values, "reviewed-task-discovery"), {"setup_token": api.token})]


@pytest.mark.parametrize(
    "header,value,status",
    [
        ("Authorization", None, 404),
        ("Authorization", "Bearer foreign", 404),
        ("Origin", None, 404),
        ("Origin", "null", 404),
        ("Origin", "https://attacker.example", 404),
        ("X-Potbot-Setup-Token", None, 400),
        ("X-Potbot-Setup-Token", "ordinary", 404),
        ("Idempotency-Key", None, 400),
        ("Idempotency-Key", ".leading", 400),
        ("Idempotency-Key", "space is ambiguous", 400),
        ("Idempotency-Key", "x" * 129, 400),
    ],
)
def test_mutations_require_independent_matching_authorities(api, header, value, status):
    headers = dict(api.headers)
    if value is None:
        headers.pop(header)
    else:
        headers[header] = value
    response = api.client().post(f"{BASE}/searches", json=creation(), headers=headers)
    assert response.status_code == status and api.calls == []
    assert "canary" not in response.text


@pytest.mark.parametrize(
    "header", ["Authorization", "Origin", "X-Potbot-Setup-Token", "Idempotency-Key", "Content-Type"]
)
def test_duplicate_authority_and_content_headers_never_reach_an_owner(api, header):
    headers = {**api.headers, "Content-Type": "application/json"}
    response = api.client().post(
        f"{BASE}/searches", json=creation(), headers=[*headers.items(), (header, headers[header])]
    )
    assert response.status_code in {400, 404} and not api.calls


@pytest.mark.parametrize(
    "change",
    [
        {"scores": [{"id": "a" * 64, "scoreMicros": 1_000_000}]},
        {"modelPath": "/operator/private-model"},
        {"query": "q" * 501},
        {"limit": True},
        {"limit": 11},
        {"confirm": 1},
        {"mode": {}},
        {"mode": "semantic"},
        {"embedding": {"scores": []}},
        {"constraints": {"uploadedEvidence": True}},
    ],
)
def test_create_is_exact_and_cannot_upload_scores_or_private_native_paths(api, change):
    response = api.client().post(
        f"{BASE}/searches", json={**creation(), **change}, headers=api.headers
    )
    assert response.status_code == 400 and not api.calls
    assert "/operator/private-model" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"revision": True, "digest": "a" * 64, "confirm": True},
        {"revision": 0, "digest": "a" * 64, "confirm": True},
        {"revision": 2**53, "digest": "a" * 64, "confirm": True},
        {"revision": 1, "digest": "A" * 64, "confirm": True},
        {"revision": 1, "digest": "a" * 64, "confirm": True, "scores": []},
    ],
)
def test_control_envelopes_do_not_admit_extra_or_ambiguous_fields(api, body):
    response = api.client().post(
        f"{BASE}/searches/{uuid4()}/cancel", json=body, headers=api.headers
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "raw",
    [
        b'{"confirm":true,"confirm":true}',
        b'{"confirm":true,"query":NaN}',
        b" " * 8193,
        b"[1]",
        b"\xff",
    ],
)
def test_duplicate_nonfinite_and_oversized_json_never_reach_owner(api, raw):
    response = api.client().post(
        f"{BASE}/searches", content=raw, headers={**api.headers, "Content-Type": "application/json"}
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "query",
    [
        "limit=26",
        "limit=0",
        "limit=01",
        "limit=1&limit=2",
        "after=not-a-uuid",
        "episodeId=invalid",
        "provider=remote",
    ],
)
def test_read_queries_are_bounded_and_unambiguous(api, query):
    response = api.client(setup=False).get(
        f"{BASE}/searches?{query}", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize("action", ["refine", "run", "claim", "complete", "scores", "publish"])
def test_browser_has_no_native_execution_or_score_upload_route(api, action):
    response = api.client().post(
        f"{BASE}/searches/{uuid4()}/{action}",
        json={"scores": [], "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 404 and not api.calls


def test_read_response_has_a_byte_limit_and_no_partial_publication(api):
    api.response_value[0] = {"private": "x" * MAX_RESPONSE_BYTES}
    response = api.client(setup=False).get(
        f"{BASE}/searches", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code == 400 and len(response.content) < 4096


def test_mutation_without_setup_bridge_is_unavailable(api):
    response = api.client(setup=False).post(
        f"{BASE}/searches", json=creation(), headers=api.headers
    )
    assert response.status_code == 503 and not api.calls
