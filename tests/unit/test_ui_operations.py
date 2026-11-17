"""HTTP scope and read-only boundary for the aggregate operation catalogue."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_operations import register_operation_routes
from potbot.api.wire import error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, RevisionConflictError, ScopeDeniedError


@pytest.fixture
def route_rig():
    actor = WorkspaceContext(uuid4(), uuid4())
    foreign = WorkspaceContext(uuid4(), uuid4())
    calls = []
    scope = {
        "schema": "potbot-operation-catalogue/v1",
        "workspaceId": str(actor.workspace_id),
        "principalId": str(actor.principal_id),
        "snapshotSha256": "a" * 64,
    }

    class Reader:
        def list(self, context, **query):
            calls.append(("list", context, query))
            if query.get("after") == "stale":
                raise RevisionConflictError("private source revision detail")
            return {**scope, "items": [], "nextCursor": None}

        def inspect(self, context, operation_id):
            calls.append(("inspect", context, operation_id))
            if operation_id == "f" * 64:
                raise ScopeDeniedError("private unavailable record")
            return {**scope, "item": {"id": operation_id}}

    def authenticate(value):
        if value == "Bearer scoped":
            return actor
        if value == "Bearer foreign":
            return foreign
        raise ScopeDeniedError("private token detail")

    app = FastAPI()

    @app.exception_handler(DomainError)
    async def failure(_request, error):
        result = error_response(error)
        return JSONResponse(result.body, status_code=result.status)

    register_operation_routes(
        app,
        object(),
        authenticate,
        ("http://127.0.0.1:3100",),
        actor.workspace_id,
        reader=Reader(),
    )
    return SimpleNamespace(client=TestClient(app), actor=actor, calls=calls)


def test_listing_binds_authenticated_scope_and_bounded_query(route_rig):
    r = route_rig
    response = r.client.get(
        "/v1/ui/operations",
        params={"q": "profile", "kind": "cog", "state": "installed_unqualified", "limit": "7"},
        headers={"Authorization": "Bearer scoped", "Origin": "http://127.0.0.1:3100"},
    )
    assert response.status_code == 200
    assert response.json()["workspaceId"] == str(r.actor.workspace_id)
    assert response.json()["principalId"] == str(r.actor.principal_id)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert r.calls == [
        (
            "list",
            r.actor,
            {
                "query": "profile",
                "kind": "cog",
                "state": "installed_unqualified",
                "limit": 7,
                "after": None,
            },
        )
    ]


@pytest.mark.parametrize(
    "headers",
    (
        {},
        {"Authorization": "Bearer bad"},
        {"Authorization": "Bearer foreign"},
        {"Authorization": "Bearer scoped", "Origin": "https://unapproved.invalid"},
    ),
)
def test_untrusted_scope_never_reaches_catalogue(route_rig, headers):
    for path in ("/v1/ui/operations", "/v1/ui/operations/" + "a" * 64):
        response = route_rig.client.get(path, headers=headers)
        assert response.status_code == 404
        assert "private" not in response.text
    assert route_rig.calls == []


@pytest.mark.parametrize(
    "query",
    (
        "workspace_id=foreign",
        "principal_id=foreign",
        "q=one&q=two",
        "limit=1&limit=2",
        "limit=0",
        "limit=101",
        "limit=+1",
        "limit=1.0",
        "limit=" + "9" * 100,
        "limit=%EF%BC%91",
        "q=" + "x" * 161,
        "after=" + "x" * 4097,
    ),
)
def test_invalid_queries_are_rejected_before_read(route_rig, query):
    response = route_rig.client.get(
        "/v1/ui/operations?" + query, headers={"Authorization": "Bearer scoped"}
    )
    assert response.status_code == 400
    assert route_rig.calls == []


def test_detail_rejects_parameters_and_noncanonical_ids(route_rig):
    for path in (
        "/v1/ui/operations/" + "A" * 64,
        "/v1/ui/operations/" + "b" * 63,
        "/v1/ui/operations/" + "b" * 64 + "?workspace=foreign",
    ):
        response = route_rig.client.get(path, headers={"Authorization": "Bearer scoped"})
        assert response.status_code == 400
    assert route_rig.calls == []


def test_detail_and_stale_cursor_keep_existing_error_semantics(route_rig):
    r = route_rig
    headers = {"Authorization": "Bearer scoped"}
    item = r.client.get("/v1/ui/operations/" + "b" * 64, headers=headers)
    assert item.status_code == 200 and item.json()["item"]["id"] == "b" * 64
    missing = r.client.get("/v1/ui/operations/" + "f" * 64, headers=headers)
    assert missing.status_code == 404 and "private" not in missing.text
    stale = r.client.get("/v1/ui/operations?after=stale", headers=headers)
    assert stale.status_code == 409 and "private" not in stale.text


@pytest.mark.parametrize("method", ("post", "put", "patch", "delete"))
def test_catalogue_has_no_mutation_or_execution_route(route_rig, method):
    response = getattr(route_rig.client, method)(
        "/v1/ui/operations", headers={"Authorization": "Bearer scoped"}
    )
    assert response.status_code == 405
    assert route_rig.calls == []
