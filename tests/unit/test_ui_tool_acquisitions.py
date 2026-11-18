"""Admission boundaries for durable acquisition requests; no host owner in HTTP."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_tool_acquisitions import register_tool_acquisition_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError

BASE = "/v1/ui/tools/acquisitions"


@pytest.fixture
def api():
    context = WorkspaceContext(uuid4(), uuid4())
    other = WorkspaceContext(uuid4(), uuid4())
    calls = []
    token = "private-tool-setup-canary-" + "x" * 32

    class Service:
        def __getattr__(self, method):
            def call(actor, *args, **kwargs):
                assert actor == context
                calls.append((method, args, kwargs))
                return {
                    "schema": "potbot-tool-acquisitions/v1",
                    "workspaceId": str(context.workspace_id),
                    "principalId": str(context.principal_id),
                    "items": [],
                }

            return call

    def authenticate(value):
        if value == "Bearer ordinary":
            return context
        if value == "Bearer foreign":
            return other
        raise ScopeDeniedError("Private auth canary")

    def authorize(value):
        if value != token:
            raise ScopeDeniedError("Private setup canary")

    def client(*, setup=True):
        app = FastAPI()

        @app.exception_handler(DomainError)
        async def failed(request, error):
            response = error_response(
                error,
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=getattr(request.state, "idempotency_key", None) is not None,
                ),
            )
            return JSONResponse(response.body, status_code=response.status)

        register_tool_acquisition_routes(
            app,
            object(),
            authenticate,
            ("http://127.0.0.1:3100",),
            context.workspace_id,
            authorize if setup else None,
            service=Service(),
        )
        return TestClient(app)

    return SimpleNamespace(
        client=client,
        calls=calls,
        context=context,
        headers={
            "Authorization": "Bearer ordinary",
            "Origin": "http://127.0.0.1:3100",
            "X-Potbot-Setup-Token": token,
            "Idempotency-Key": "tool-request-001",
        },
    )


def test_readonly_scoped_bounded_no_setup(api):
    client = api.client(setup=False)
    headers = {"Authorization": "Bearer ordinary"}
    for path in (
        BASE,
        BASE + "/recipes",
        BASE + "/" + str(uuid4()),
        BASE + "/" + str(uuid4()) + "/events",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["workspaceId"] == str(api.context.workspace_id)
    assert [item[0] for item in api.calls] == ["list", "recipes", "inspect", "events"]


@pytest.mark.parametrize(
    "missing", ["Authorization", "Origin", "X-Potbot-Setup-Token", "Idempotency-Key"]
)
def test_request_requires_all_independent_capabilities(api, missing):
    headers = {key: value for key, value in api.headers.items() if key != missing}
    result = api.client().post(
        BASE, headers=headers, json={"recipeId": str(uuid4()), "commit": "a" * 40, "confirm": True}
    )
    assert result.status_code == (400 if missing == "Idempotency-Key" else 404)
    assert not api.calls


@pytest.mark.parametrize(
    "change",
    [
        {"Origin": "https://unapproved.invalid"},
        {"Authorization": "Bearer foreign"},
        {"X-Potbot-Setup-Token": "ordinary-bearer"},
        {"Idempotency-Key": "x" * 129},
    ],
)
def test_refuses_wrong_scope_origin_setup_and_key(api, change):
    result = api.client().post(
        BASE,
        headers={**api.headers, **change},
        json={"recipeId": str(uuid4()), "commit": "a" * 40, "confirm": True},
    )
    assert result.status_code == (400 if "Idempotency-Key" in change else 404)
    assert not api.calls


def test_request_accepts_only_recipe_id_and_exact_source(api):
    recipe = uuid4()
    response = api.client().post(
        BASE,
        headers=api.headers,
        json={"recipeId": str(recipe), "commit": "a" * 40, "confirm": True},
    )
    assert response.status_code == 200
    method, args, kwargs = api.calls[0]
    assert method == "request" and args == (recipe, "a" * 40, "tool-request-001")
    assert kwargs["setup_token"] == api.headers["X-Potbot-Setup-Token"]
    assert "private-tool-setup-canary" not in response.text


@pytest.mark.parametrize(
    "extra", ["argv", "repository", "sourcePath", "runtimePins", "recipe", "agentId"]
)
def test_browser_cannot_supply_execution_or_source_overrides(api, extra):
    response = api.client().post(
        BASE,
        headers=api.headers,
        json={"recipeId": str(uuid4()), "commit": "a" * 40, "confirm": True, extra: "untrusted"},
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "action,field,method",
    [
        ("approve-build", "reviewSha256", "approve_build"),
        ("approve", "evidenceSha256", "decide"),
        ("reject", "evidenceSha256", "decide"),
        ("rollback", "bindingSha256", "action"),
        ("reconcile", "bindingSha256", "action"),
        ("repin", "bindingSha256", "action"),
        ("postflight", "bindingSha256", "action"),
        ("cancel", "bindingSha256", "action"),
    ],
)
def test_control_queues_exact_reviewed_binding(api, action, field, method):
    operation = uuid4()
    response = api.client().post(
        f"{BASE}/{operation}/{action}",
        headers=api.headers,
        json={"revision": 7, field: "b" * 64, "confirm": True},
    )
    assert response.status_code == 200
    called, args, kwargs = api.calls[0]
    assert called == method and args[0] == operation
    assert 7 in args and "b" * 64 in args
    assert kwargs["setup_token"] == api.headers["X-Potbot-Setup-Token"]


@pytest.mark.parametrize("revision", [True, -1, 1.5, "1", 2**53])
def test_control_revision_is_exact_bounded_integer(api, revision):
    response = api.client().post(
        f"{BASE}/{uuid4()}/approve-build",
        headers=api.headers,
        json={"revision": revision, "reviewSha256": "b" * 64, "confirm": True},
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "raw",
    [
        '{"revision":0,"revision":1,"reviewSha256":"' + "a" * 64 + '","confirm":true}',
        '{"revision":NaN,"reviewSha256":"' + "a" * 64 + '","confirm":true}',
        "[1,2,3]",
        '{"confirm":1}',
        "x" * 8193,
    ],
)
def test_ambiguous_or_oversized_json_is_refused(api, raw):
    response = api.client().post(
        f"{BASE}/{uuid4()}/approve-build",
        content=raw,
        headers={**api.headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "query", ["limit=51", "limit=0", "limit=01", "limit=3&limit=4", "token=private", "after="]
)
def test_pagination_rejects_unbounded_or_ambiguous_queries(api, query):
    response = api.client().get(BASE + "?" + query, headers=api.headers)
    assert response.status_code == 400 and not api.calls


def test_non_operator_listener_cannot_mutate(api):
    response = api.client(setup=False).post(
        BASE,
        headers=api.headers,
        json={"recipeId": str(uuid4()), "commit": "a" * 40, "confirm": True},
    )
    assert response.status_code != 200 and not api.calls


def test_duplicate_idempotency_headers_fail_closed(api):
    headers = list(api.headers.items()) + [("Idempotency-Key", "other-key")]
    response = api.client().post(
        BASE, headers=headers, json={"recipeId": str(uuid4()), "commit": "a" * 40, "confirm": True}
    )
    assert response.status_code == 400 and not api.calls
