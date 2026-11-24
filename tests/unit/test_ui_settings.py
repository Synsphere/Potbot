"""HTTP admission, strict reviews and no authority from an inventory read."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_settings import register_settings_routes
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.domain.settings_catalogue import catalogue

ORIGIN = "http://localhost:3100"


def application(*, setup=True):
    context = WorkspaceContext(uuid4(), uuid4())
    calls = []

    class Service:
        def catalogue(self, actor):
            calls.append(("catalogue", actor))
            return {
                "schema": "potbot-settings/v1",
                "workspaceId": str(actor.workspace_id),
                "principalId": str(actor.principal_id),
                **catalogue(),
            }

        def effective(self, actor):
            calls.append(("effective", actor))
            return {"workspaceId": str(actor.workspace_id)}

        def inventory_view(self, actor, kind):
            calls.append((kind, actor))
            return {"workspaceId": str(actor.workspace_id), "items": []}

        def swarm(self, actor, *, request_available):
            calls.append(("swarm", actor))
            return {"workspaceId": str(actor.workspace_id), "requestAvailable": request_available}

        def request(self, actor, identity, value):
            calls.append(("request", actor, identity, value))
            return {"requestId": str(identity), "state": "requested"}

    def authenticate(value):
        if value == "Bearer wrong-scope":
            return WorkspaceContext(uuid4(), context.principal_id)
        if value != "Bearer scoped-token":
            raise ScopeDeniedError("Unavailable")
        return context

    def authorize(value):
        if value != "native-setup-token":
            raise ScopeDeniedError("Unavailable")

    app = FastAPI()

    @app.exception_handler(DomainError)
    async def errors(_request, error):
        status = (
            400
            if isinstance(error, InvalidInputError)
            else 409
            if isinstance(error, RevisionConflictError)
            else 503
            if isinstance(error, CapabilityUnavailableError)
            else 403
        )
        return JSONResponse({"error": error.code.value}, status_code=status)

    register_settings_routes(
        app,
        None,
        authenticate,
        (ORIGIN,),
        context.workspace_id,
        authorize if setup else None,
        service=Service(),
    )
    return TestClient(app), context, calls


def headers(**extra):
    return {"Authorization": "Bearer scoped-token", **extra}


def review():
    return {
        "requestId": str(uuid4()),
        "grantId": str(uuid4()),
        "expectedRevision": 1,
        "expectedGeneration": 1,
        "grantSha256": "a" * 64,
        "confirm": True,
    }


def test_catalogue_read_is_scoped_and_private_without_touching_native_credentials():
    client, context, calls = application()
    response = client.get("/v1/ui/settings/catalogue", headers=headers())
    assert response.status_code == 200
    assert response.json()["workspaceId"] == str(context.workspace_id)
    assert len(response.json()["descriptors"]) == 17
    assert response.headers["cache-control"] == "private, no-store"
    assert calls == [("catalogue", context)]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/ui/settings/catalogue",
        "/v1/ui/settings/effective",
        "/v1/ui/inventory/models",
        "/v1/ui/inventory/tools",
        "/v1/ui/swarm/nodes",
    ],
)
def test_each_read_rejects_foreign_scope_unknown_queries_and_bad_origin_before_service(path):
    client, _context, calls = application()
    assert client.get(path, headers=headers(Origin="https://foreign.invalid")).status_code == 403
    assert client.get(path + "?workspace=other", headers=headers()).status_code == 400
    assert client.get(path, headers=headers(Authorization="Bearer wrong-scope")).status_code == 403
    assert client.get(path).status_code == 403
    assert calls == []


def test_request_requires_setup_origin_exact_json_and_stable_identity():
    client, context, calls = application()
    path = "/v1/ui/swarm/revocations"
    body = review()
    assert client.post(path, headers=headers(Origin=ORIGIN), json=body).status_code == 403
    authorized = headers(Origin=ORIGIN, **{"X-Potbot-Setup-Token": "native-setup-token"})
    response = client.post(path, headers=authorized, json=body)
    assert response.status_code == 202 and response.json()["state"] == "requested"
    assert calls[-1][0] == "request" and calls[-1][1] == context
    assert str(calls[-1][2]) == body["requestId"]
    assert calls[-1][3]["schema"] == "potbot-swarm-revocation-request/v1"
    for patch in ({"confirm": False}, {"hostname": "foreign"}, {"requestId": 123}):
        assert client.post(path, headers=authorized, json=body | patch).status_code == 400
    assert (
        client.post(
            path, headers=authorized | {"Content-Type": "text/plain"}, content="{}"
        ).status_code
        == 400
    )
    assert (
        client.post(
            path,
            headers=authorized | {"Content-Type": "application/json"},
            content='{"confirm":true,"confirm":true}',
        ).status_code
        == 400
    )
    assert (
        client.post(
            path,
            headers=authorized | {"Content-Type": "application/json"},
            content='{"padding":"' + "x" * 5000 + '"}',
        ).status_code
        == 400
    )


def test_missing_native_management_adapter_does_not_disable_passive_views():
    client, _context, calls = application(setup=False)
    assert client.get("/v1/ui/swarm/nodes", headers=headers()).status_code == 200
    assert calls[-1][0] == "swarm"
    response = client.post(
        "/v1/ui/swarm/revocations", headers=headers(Origin=ORIGIN), json=review()
    )
    assert response.status_code == 503
