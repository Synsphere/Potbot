"""Operation HTTP mutations preserve explicit authority and retry identity."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_operation_workbench import MAX_BODY_BYTES, register_operation_workbench_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, RevisionConflictError, ScopeDeniedError


@pytest.fixture
def workbench_api():
    actor, foreign = WorkspaceContext(uuid4(), uuid4()), WorkspaceContext(uuid4(), uuid4())
    operation, job, token = "a" * 64, uuid4(), "operator-setup-canary-" + "x" * 32
    calls, setup_calls = [], []

    class Service:
        def __getattr__(self, method):
            def invoke(context, operation_id, *args, **kwargs):
                assert context == actor and operation_id == operation
                calls.append((method, args, kwargs))
                if "stale" in args:
                    raise RevisionConflictError("secret://private-operation-canary")
                return {
                    "schema": "potbot-operation-workbench/v1",
                    "workspaceId": str(context.workspace_id),
                    "principalId": str(context.principal_id),
                    "operationId": operation_id,
                    "job": {"id": str(job), "state": "queued"},
                }

            return invoke

    def authenticate(value):
        if value == "Bearer scoped":
            return actor
        if value == "Bearer foreign":
            return foreign
        raise ScopeDeniedError("private authentication canary")

    def authorize(value):
        setup_calls.append(value)
        if value != token:
            raise ScopeDeniedError("private setup canary")

    def application(*, setup=True):
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

        register_operation_workbench_routes(
            app,
            object(),
            authenticate,
            ("http://127.0.0.1:3100",),
            actor.workspace_id,
            authorize if setup else None,
            service=Service(),
        )
        return TestClient(app)

    headers = {
        "Authorization": "Bearer scoped",
        "Origin": "http://127.0.0.1:3100",
        "X-Potbot-Setup-Token": token,
        "Idempotency-Key": "operation-check-1",
    }
    return SimpleNamespace(
        actor=actor,
        operation=operation,
        job=job,
        token=token,
        calls=calls,
        setup_calls=setup_calls,
        headers=headers,
        client=application(),
        application=application,
        path=f"/v1/ui/operations/{operation}",
    )


def test_check_and_launch_forward_same_key_and_exact_request_after_setup(workbench_api):
    r = workbench_api
    request = {"job_type": "cog.execute", "input": {"value": {"text": "private request"}}}
    checked = r.client.post(r.path + "/checks", json=request, headers=r.headers)
    assert checked.status_code == 202
    assert checked.headers["cache-control"] == "private, no-store"
    assert checked.headers["x-content-type-options"] == "nosniff"
    assert r.calls == [("check", (request, "operation-check-1"), {"setup_token": r.token})]
    assert r.token not in checked.text and "private request" not in checked.text
    launched = r.client.post(
        r.path + "/runs",
        json={"checkJobId": str(r.job), "request": request, "confirm": True},
        headers={**r.headers, "Idempotency-Key": "operation-run-1"},
    )
    assert launched.status_code == 202
    assert r.calls[-1] == (
        "launch",
        (r.job, request, "operation-run-1"),
        {"setup_token": r.token},
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"Authorization": None},
        {"Authorization": "Bearer foreign"},
        {"Origin": None},
        {"Origin": "https://untrusted.invalid"},
        {"X-Potbot-Setup-Token": None},
        {"X-Potbot-Setup-Token": "wrong"},
    ),
)
def test_untrusted_mutation_never_reads_body_or_queues_work(workbench_api, changes):
    r = workbench_api
    headers = {**r.headers, **changes}
    headers = {key: value for key, value in headers.items() if value is not None}
    for suffix in ("/checks", "/runs"):
        response = r.client.post(r.path + suffix, content=b"invalid JSON", headers=headers)
        assert response.status_code == 404
        assert "canary" not in response.text
    assert not r.calls


def test_external_listener_has_no_setup_authority(workbench_api):
    r = workbench_api
    client = r.application(setup=False)
    assert client.get(r.path + "/contract", headers=r.headers).status_code == 200
    r.calls.clear()
    response = client.post(r.path + "/checks", json={}, headers=r.headers)
    assert response.status_code == 503
    assert not r.calls and not r.setup_calls


@pytest.mark.parametrize("key", (None, "", "with spaces", "x" * 201, "line\tbreak"))
def test_missing_or_ambiguous_retry_identity_never_submits(workbench_api, key):
    r = workbench_api
    headers = {name: value for name, value in r.headers.items() if name != "Idempotency-Key"}
    if key is not None:
        headers["Idempotency-Key"] = key
    response = r.client.post(r.path + "/checks", json={}, headers=headers)
    assert response.status_code == 400 and not r.calls


def test_duplicate_authorization_or_key_headers_are_rejected(workbench_api):
    r = workbench_api
    for name in ("Authorization", "Origin", "X-Potbot-Setup-Token", "Idempotency-Key"):
        headers = [*r.headers.items(), (name, r.headers[name])]
        response = r.client.post(r.path + "/checks", json={}, headers=headers)
        assert response.status_code in {400, 404}
    assert not r.calls


@pytest.mark.parametrize(
    "raw",
    (
        b'{"input":{},"input":{}}',
        b'{"input":{"value":1,"value":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b"[]",
        b'"text"',
        b"\xff",
        b'{"value":"' + b"x" * MAX_BODY_BYTES + b'"}',
        b"[" * 1500 + b"]" * 1500,
    ),
)
def test_strict_bounded_json_never_reaches_queue(workbench_api, raw):
    r = workbench_api
    response = r.client.post(
        r.path + "/checks", content=raw, headers={**r.headers, "Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert not r.calls


@pytest.mark.parametrize(
    "body",
    (
        {},
        {"confirm": 1},
        {"confirm": False},
        {"confirm": True, "unexpected": "value"},
    ),
)
def test_launch_and_cancel_require_exact_explicit_confirmation(workbench_api, body):
    r = workbench_api
    for suffix in ("/runs", f"/jobs/{r.job}/cancel"):
        response = r.client.post(r.path + suffix, json=body, headers=r.headers)
        assert response.status_code == 400
    assert not r.calls


def test_owned_cancel_does_not_require_or_read_setup_capability(workbench_api):
    r = workbench_api
    headers = {key: value for key, value in r.headers.items() if key != "X-Potbot-Setup-Token"}
    response = r.client.post(
        r.path + f"/jobs/{r.job}/cancel", json={"confirm": True}, headers=headers
    )
    assert response.status_code == 200
    assert r.calls == [("cancel", (r.job, "operation-check-1"), {})]
    assert not r.setup_calls


def test_read_routes_are_scoped_and_do_not_use_setup_or_mutate(workbench_api):
    r = workbench_api
    paths = (
        ("/contract", "describe", (), {}),
        (f"/jobs/{r.job}", "inspect", (r.job,), {}),
        (f"/jobs/{r.job}/result", "result", (r.job,), {}),
        (f"/jobs/{r.job}/events?after=7&limit=5", "events", (r.job,), {"after": 7, "limit": 5}),
        (
            f"/jobs/{r.job}/artifact?offset=16&length=128",
            "artifact",
            (r.job,),
            {"offset": 16, "length": 128},
        ),
    )
    for path, method, args, kwargs in paths:
        response = r.client.get(r.path + path, headers={"Authorization": "Bearer scoped"})
        assert response.status_code == 200
        assert r.calls[-1] == (method, args, kwargs)
    assert not r.setup_calls


@pytest.mark.parametrize(
    "suffix",
    (
        "/contract?workspace=other",
        "/contract?setup_token=private",
        "/jobs/not-a-uuid",
        "/jobs/AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "/jobs/00000000-0000-0000-0000-000000000000/events?after=0&after=1",
        "/jobs/00000000-0000-0000-0000-000000000000/events?limit=101",
        "/jobs/00000000-0000-0000-0000-000000000000/events?limit=0",
        "/jobs/00000000-0000-0000-0000-000000000000/events?after=-1",
        "/jobs/00000000-0000-0000-0000-000000000000/events?after=9007199254740992",
        "/jobs/00000000-0000-0000-0000-000000000000/artifact?length=262145",
        "/jobs/00000000-0000-0000-0000-000000000000/artifact?length=0",
        "/jobs/00000000-0000-0000-0000-000000000000/artifact?offset=-1",
        "/jobs/00000000-0000-0000-0000-000000000000/artifact?offset=0&offset=1",
    ),
)
def test_read_queries_and_identities_are_bounded_before_service(workbench_api, suffix):
    r = workbench_api
    response = r.client.get(r.path + suffix, headers=r.headers)
    assert response.status_code == 400 and not r.calls


def test_stale_check_error_is_sanitized_without_retrying_mutation(workbench_api):
    r = workbench_api
    response = r.client.post(
        r.path + "/checks", json={}, headers={**r.headers, "Idempotency-Key": "stale"}
    )
    assert response.status_code == 409
    assert "private-operation-canary" not in response.text
    assert len(r.calls) == 1 and response.json()["error"]["retryable"] is False
