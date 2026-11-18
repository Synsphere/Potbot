"""HTTP authority and input boundaries for metadata-only discovery routes."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_discovery import register_discovery_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError

BASE = "/v1/ui/discovery"


@pytest.fixture
def api():
    actor = WorkspaceContext(uuid4(), uuid4())
    foreign = WorkspaceContext(uuid4(), uuid4())
    calls = []
    token = "native-discovery-secret-canary-" + "x" * 32

    class Service:
        def __getattr__(self, name):
            def invoke(context, *args, **kwargs):
                assert context == actor
                calls.append((name, args, kwargs))
                return {
                    "schema": "potbot-discovery/v1",
                    "workspaceId": str(actor.workspace_id),
                    "principalId": str(actor.principal_id),
                    "items": [],
                    "nextCursor": None,
                }

            return invoke

    def authenticate(value):
        if value == "Bearer ordinary":
            return actor
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
            reply = error_response(
                error,
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=hasattr(request.state, "idempotency_key"),
                ),
            )
            return JSONResponse(reply.body, status_code=reply.status)

        register_discovery_routes(
            app,
            None,
            authenticate,
            ("https://operator.example",),
            actor.workspace_id,
            authorize if setup else None,
            service=Service(),
        )
        return TestClient(app)

    return SimpleNamespace(
        actor=actor,
        calls=calls,
        token=token,
        client=client,
        headers={
            "Authorization": "Bearer ordinary",
            "Origin": "https://operator.example",
            "X-Potbot-Setup-Token": token,
            "Idempotency-Key": "stable-discovery-1",
        },
    )


@pytest.mark.parametrize(
    "path,method",
    [
        ("sources", "sources"),
        ("publishers", "publishers"),
        ("candidates", "candidates"),
        ("checks", "checks"),
    ],
)
def test_lists_are_scoped_metadata_reads_without_setup(api, path, method):
    response = api.client(False).get(
        BASE + "/" + path, headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code == 200
    assert api.calls[-1][0] == method
    assert "setup_token" not in api.calls[-1][2]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization, Origin"


def test_search_retains_empty_security_filters_and_opaque_cursor(api):
    response = api.client().get(
        BASE + "/candidates",
        params={
            "q": "inspect commit",
            "platform": "linux",
            "permittedEffects": "read_only",
            "permittedScopes": "",
            "requireLocal": "true",
            "after": "opaque_cursor",
            "limit": "50",
        },
        headers=api.headers,
    )
    assert response.status_code == 200
    assert api.calls[-1][2] == {
        "query": "inspect commit",
        "kind": None,
        "source_id": None,
        "limit": 50,
        "after": "opaque_cursor",
        "constraints": {
            "platform": "linux",
            "permittedEffects": ["read_only"],
            "permittedScopes": [],
            "requireLocal": True,
        },
    }


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "limit=true",
        "limit=1&limit=2",
        "after=",
        "latest=true",
        "platform=host",
        "permittedEffects=read_only,read_only",
        "permittedEffects=grant_all",
        "requireLocal=1",
        "classes=unknown",
        "inputTypes=https://attacker.example/type",
        "sourceId=not-uuid",
        "kind=command",
        "q=" + "x" * 501,
        "permittedScopes=" + "x" * 8200,
    ],
)
def test_invalid_search_stops_before_service(api, query):
    response = api.client().get(BASE + "/candidates?" + query, headers=api.headers)
    assert response.status_code == 400
    assert not api.calls


@pytest.mark.parametrize(
    "collection,action,method",
    [
        ("sources", "disable", "control"),
        ("publishers", "approve", "control"),
        ("checks", "handoff", "action"),
        ("checks", "cancel", "action"),
    ],
)
def test_controls_bind_exact_revision_digest_key_and_native_capability(
    api, collection, action, method
):
    identity = uuid4()
    response = api.client().post(
        f"{BASE}/{collection}/{identity}/{action}",
        json={"revision": 3, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 200
    name, args, kwargs = api.calls[-1]
    assert name == method and args[-5:] == (identity, action, 3, "a" * 64, "stable-discovery-1")
    assert kwargs == {"setup_token": api.token}
    assert api.token not in response.text


def test_prepare_only_accepts_exact_candidate_and_agent_identity(api):
    identity, snapshot, agent = uuid4(), uuid4(), uuid4()
    response = api.client().post(
        f"{BASE}/candidates/{identity}/checks",
        json={
            "snapshotId": str(snapshot),
            "candidateSha256": "b" * 64,
            "agentId": str(agent),
            "confirm": True,
        },
        headers=api.headers,
    )
    assert response.status_code == 200
    assert api.calls[-1][0:2] == (
        "create_check",
        (identity, snapshot, "b" * 64, agent, "stable-discovery-1"),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"confirm": False},
        {"revision": True},
        {"digest": "A" * 64},
        {"command": ["/bin/sh"]},
        {"manifest": "/host/private"},
        {"revision": -1},
    ],
)
def test_mutation_shape_never_supplies_commands_or_host_paths(api, change):
    body = {"revision": 1, "digest": "a" * 64, "confirm": True, **change}
    response = api.client().post(f"{BASE}/checks/{uuid4()}/handoff", json=body, headers=api.headers)
    assert response.status_code == 400
    assert not api.calls


@pytest.mark.parametrize(
    "raw",
    [
        b'{"confirm":true,"confirm":true}',
        b'{"confirm":true,"revision":NaN}',
        b"[]",
        b'{"confirm":true,"payload":"' + b"x" * 8192 + b'"}',
    ],
)
def test_duplicate_nonfinite_and_oversized_bodies_are_rejected(api, raw):
    response = api.client().post(
        f"{BASE}/checks/{uuid4()}/cancel",
        content=raw,
        headers={**api.headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert not api.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("Authorization", "Bearer foreign"),
        ("Origin", "https://attacker.example"),
        ("Origin", None),
        ("X-Potbot-Setup-Token", "ordinary"),
    ],
)
def test_scope_origin_and_independent_capability_are_required(api, field, value):
    headers = dict(api.headers)
    if value is None:
        headers.pop(field)
    else:
        headers[field] = value
    response = api.client().post(
        f"{BASE}/checks/{uuid4()}/cancel",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=headers,
    )
    # Scope failures use the existing anti-enumeration 404 wire contract.
    assert response.status_code == 404
    assert not api.calls
    assert "canary" not in response.text


@pytest.mark.parametrize("action", ["measure", "refresh", "install", "execute", "run-once"])
def test_host_actions_are_not_http_controls(api, action):
    response = api.client().post(
        f"{BASE}/checks/{uuid4()}/{action}",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 400 and not api.calls


def test_duplicate_command_keys_and_disabled_setup_are_rejected(api):
    path = f"{BASE}/checks/{uuid4()}/cancel"
    body = {"revision": 1, "digest": "a" * 64, "confirm": True}
    headers = list(api.headers.items()) + [("Idempotency-Key", "another")]
    assert api.client().post(path, json=body, headers=headers).status_code == 400
    assert api.client(False).post(path, json=body, headers=api.headers).status_code == 503
    assert not api.calls
