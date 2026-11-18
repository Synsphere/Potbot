"""Scope, metadata-only routing and exact decision boundaries for live MCP."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_mcp_discovery import register_mcp_discovery_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError

BASE = "/v1/ui/mcp-discovery"


@pytest.fixture
def api():
    context, foreign = WorkspaceContext(uuid4(), uuid4()), WorkspaceContext(uuid4(), uuid4())
    calls = []
    token = "native-mcp-discovery-canary-" + "x" * 40

    class Service:
        def __getattr__(self, name):
            def invoke(actor, *args, **kwargs):
                assert actor == context
                calls.append((name, args, kwargs))
                return {
                    "schema": "potbot-mcp-discovery/v1",
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
        raise ScopeDeniedError("private authentication canary")

    def authorize(value):
        if value != token:
            raise ScopeDeniedError("private setup canary")

    def client(setup=True):
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

        register_mcp_discovery_routes(
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
        token=token,
        headers={
            "Authorization": "Bearer ordinary",
            "Origin": "https://operator.example",
            "X-Potbot-Setup-Token": token,
            "Idempotency-Key": "exact-mcp-command",
        },
    )


@pytest.mark.parametrize(
    "path,method",
    [
        ("bindings", "bindings"),
        ("bindings/{id}", "binding"),
        ("bindings/{id}/entries", "entries"),
        ("bindings/{id}/entries/{entry}/reference", "reference"),
        ("attempts", "attempts"),
        ("attempts/{id}", "attempt"),
    ],
)
def test_retained_reads_need_no_setup_and_call_only_the_named_read_owner(api, path, method):
    response = api.client().get(
        BASE + "/" + path.format(id=uuid4(), entry=uuid4()),
        headers={"Authorization": "Bearer ordinary"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert [call[0] for call in api.calls] == [method]
    assert api.token not in response.text


@pytest.mark.parametrize(
    "action,owner",
    [
        ("enable", "control"),
        ("disable", "control"),
        ("revoke", "control"),
        ("reconcile", "request"),
        ("cancel", "cancel"),
    ],
)
def test_exact_mutations_record_decisions_without_a_native_worker(api, action, owner):
    identity = uuid4()
    collection = "attempts" if action == "cancel" else "bindings"
    response = api.client().post(
        f"{BASE}/{collection}/{identity}/{action}",
        json={"revision": 3, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 200
    assert len(api.calls) == 1
    name, args, kwargs = api.calls[0]
    assert name == owner and args[0] == identity
    assert args[-3:] == (3, "a" * 64, "exact-mcp-command")
    assert kwargs == {"setup_token": api.token}


@pytest.mark.parametrize(
    "name,value",
    [
        ("Authorization", "Bearer foreign"),
        ("Authorization", None),
        ("Origin", "https://attacker.example"),
        ("Origin", None),
        ("X-Potbot-Setup-Token", "ordinary"),
    ],
)
def test_mutations_require_matching_scope_origin_and_independent_authority(api, name, value):
    headers = dict(api.headers)
    if value is None:
        headers.pop(name)
    else:
        headers[name] = value
    response = api.client().post(
        f"{BASE}/bindings/{uuid4()}/reconcile",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=headers,
    )
    assert response.status_code == 404
    assert "canary" not in response.text and not api.calls


@pytest.mark.parametrize(
    "header", ["Authorization", "Origin", "X-Potbot-Setup-Token", "Idempotency-Key"]
)
def test_duplicated_authority_or_command_headers_are_rejected(api, header):
    headers = [*api.headers.items(), (header, api.headers[header])]
    response = api.client().post(
        f"{BASE}/bindings/{uuid4()}/reconcile",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=headers,
    )
    assert response.status_code in {400, 404}
    assert not api.calls


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"revision": True, "digest": "a" * 64, "confirm": True},
        {"revision": 0, "digest": "a" * 64, "confirm": True},
        {"revision": 2**53, "digest": "a" * 64, "confirm": True},
        {"revision": 1, "digest": "A" * 64, "confirm": True},
        {"revision": 1, "digest": "a" * 64, "confirm": False},
        {
            "revision": 1,
            "digest": "a" * 64,
            "confirm": True,
            "endpoint": "https://attacker.example",
        },
    ],
)
def test_mutation_body_is_an_exact_bounded_decision(api, body):
    response = api.client().post(
        f"{BASE}/attempts/{uuid4()}/cancel", json=body, headers=api.headers
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "raw",
    [
        '{"revision":1,"revision":2,"digest":"' + "a" * 64 + '","confirm":true}',
        '{"revision":NaN,"digest":"' + "a" * 64 + '","confirm":true}',
        '{"revision":1,"digest":"' + "a" * 64 + '","confirm":true,"padding":"' + "x" * 8192 + '"}',
        "[]",
    ],
)
def test_ambiguous_or_oversized_json_is_rejected(api, raw):
    response = api.client().post(
        f"{BASE}/attempts/{uuid4()}/cancel",
        content=raw,
        headers={**api.headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "limit=01",
        "limit=true",
        "limit=2&limit=3",
        "after=not-a-uuid",
        "kind=tools",
        "q=x&q=y",
        "endpoint=https://attacker.example",
        "q=" + "x" * 257,
    ],
)
def test_retained_search_rejects_unbounded_or_ambiguous_queries(api, query):
    response = api.client().get(
        f"{BASE}/bindings/{uuid4()}/entries?{query}", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code == 400 and not api.calls


def test_query_filters_reach_existing_retained_owner_without_permission_changes(api):
    identity, cursor = uuid4(), uuid4()
    response = api.client().get(
        f"{BASE}/bindings/{identity}/entries",
        params={"kind": "resource_template", "q": "review", "limit": "12", "after": str(cursor)},
        headers={"Authorization": "Bearer ordinary"},
    )
    assert response.status_code == 200
    assert api.calls == [
        (
            "entries",
            (identity,),
            {"kind": "resource_template", "query": "review", "limit": 12, "after": str(cursor)},
        )
    ]


@pytest.mark.parametrize("action", ["admit", "execute", "run", "read", "call", "install", "fetch"])
def test_native_or_provider_operations_are_not_http_controls(api, action):
    response = api.client().post(
        f"{BASE}/bindings/{uuid4()}/{action}",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 400 and not api.calls


def test_disabled_setup_has_no_mutating_route_to_an_owner(api):
    response = api.client(setup=False).post(
        f"{BASE}/bindings/{uuid4()}/reconcile",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 503 and not api.calls
