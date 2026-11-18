"""Selected task HTTP controls reject ambiguous authority and changed launch input."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_task_execution import (
    MAX_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    register_task_execution_routes,
)
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError


def check_body():
    return {
        "selectionSha256": "a" * 64,
        "request": {
            "job_type": "model.generate",
            "episode_id": str(uuid4()),
            "input": {"prompt": "produce the reviewed result", "max_output_tokens": 16},
            "selected_models": [
                {"provider": "openai", "model": "fixture-model", "role": "reasoning"}
            ],
        },
        "acceptance": {
            "schema": "potbot-task-result-oracle/v1",
            "assertions": [{"op": "equals", "pointer": "/text", "value": "expected result"}],
        },
        "confirm": True,
    }


@pytest.fixture
def api():
    actor = WorkspaceContext(uuid4(), uuid4())
    selection, job = uuid4(), uuid4()
    calls, response = [], [None]
    token = "selected-task-operator-capability-" + "x" * 40

    class Service:
        def __getattr__(self, name):
            assert name in {
                "context",
                "check",
                "launch",
                "jobs",
                "inspect",
                "events",
                "result",
                "artifact",
                "cancel",
            }

            def invoke(scope, *args, **kwargs):
                assert scope == actor
                calls.append((name, args, kwargs))
                return response[0] or {
                    "schema": "potbot-task-execution/v1",
                    "workspaceId": str(actor.workspace_id),
                    "principalId": str(actor.principal_id),
                    "selectionId": str(selection),
                }

            return invoke

    def authenticate(value):
        if value == "Bearer ordinary":
            return actor
        if value == "Bearer foreign":
            return WorkspaceContext(uuid4(), actor.principal_id)
        raise ScopeDeniedError("private auth detail")

    def authorize(value):
        if value != token:
            raise ScopeDeniedError("private setup detail")

    def client(*, setup=True):
        app = FastAPI()

        @app.exception_handler(DomainError)
        async def failed(request, error):
            reply = error_response(
                error,
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=hasattr(request.state, "idempotency_key"),
                ),
            )
            return JSONResponse(reply.body, status_code=reply.status)

        register_task_execution_routes(
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
        selection=selection,
        job=job,
        base=f"/v1/ui/task-execution/selections/{selection}",
        calls=calls,
        response=response,
        client=client,
        headers={
            "Authorization": "Bearer ordinary",
            "Origin": "https://operator.example",
            "X-Potbot-Setup-Token": token,
            "Idempotency-Key": "selected-task-stable-key",
        },
    )


def test_check_and_launch_preserve_exact_reviewed_values_and_recovery_key(api):
    body = check_body()
    reply = api.client().post(api.base + "/checks", headers=api.headers, json=body)
    assert reply.status_code == 202
    assert reply.headers["cache-control"] == "private, no-store"
    name, args, kwargs = api.calls[-1]
    assert name == "check"
    assert args == (
        api.selection,
        body["selectionSha256"],
        body["request"],
        body["acceptance"],
        "selected-task-stable-key",
    )
    assert kwargs["setup_token"] == api.headers["X-Potbot-Setup-Token"]
    run = {"checkJobId": str(api.job), "bindingSha256": "b" * 64, "confirm": True}
    for _ in range(2):
        reply = api.client().post(api.base + "/runs", headers=api.headers, json=run)
        assert reply.status_code == 202
        assert api.calls[-1][1] == (api.selection, api.job, "b" * 64, "selected-task-stable-key")


@pytest.mark.parametrize(
    "suffix,name",
    [
        ("context", "context"),
        ("jobs", "jobs"),
        ("jobs/{job}", "inspect"),
        ("jobs/{job}/events", "events"),
        ("jobs/{job}/result", "result"),
        ("jobs/{job}/artifact", "artifact"),
    ],
)
def test_owned_read_routes_have_no_setup_requirement_or_execution_side_effect(api, suffix, name):
    reply = api.client(setup=False).get(
        api.base + "/" + suffix.format(job=api.job), headers={"Authorization": "Bearer ordinary"}
    )
    assert reply.status_code == 200
    assert api.calls[-1][0] == name
    assert "setup_token" not in api.calls[-1][2]
    assert reply.headers["x-content-type-options"] == "nosniff"


def test_cancel_remains_available_without_operator_key(api):
    headers = {key: value for key, value in api.headers.items() if key != "X-Potbot-Setup-Token"}
    reply = api.client(setup=False).post(
        api.base + f"/jobs/{api.job}/cancel", headers=headers, json={"confirm": True}
    )
    assert reply.status_code == 200
    assert api.calls[-1] == ("cancel", (api.selection, api.job, "selected-task-stable-key"), {})


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign",
        "no_auth",
        "no_origin",
        "bad_origin",
        "no_setup",
        "wrong_setup",
        "duplicate_auth",
        "duplicate_origin",
        "duplicate_setup",
        "duplicate_key",
        "no_key",
        "bad_key",
    ],
)
def test_ambiguous_or_missing_mutation_authority_never_reaches_owner(api, mutation):
    headers = list(api.headers.items())
    remove = {
        "foreign": "Authorization",
        "no_auth": "Authorization",
        "no_origin": "Origin",
        "bad_origin": "Origin",
        "no_setup": "X-Potbot-Setup-Token",
        "wrong_setup": "X-Potbot-Setup-Token",
        "no_key": "Idempotency-Key",
        "bad_key": "Idempotency-Key",
    }
    if mutation in remove:
        headers = [(key, value) for key, value in headers if key != remove[mutation]]
    extra = {
        "foreign": ("Authorization", "Bearer foreign"),
        "bad_origin": ("Origin", "https://evil.example"),
        "wrong_setup": ("X-Potbot-Setup-Token", "incorrect"),
        "bad_key": ("Idempotency-Key", "bad key"),
        "duplicate_auth": ("Authorization", "Bearer ordinary"),
        "duplicate_origin": ("Origin", "https://operator.example"),
        "duplicate_setup": ("X-Potbot-Setup-Token", api.headers["X-Potbot-Setup-Token"]),
        "duplicate_key": ("Idempotency-Key", "second"),
    }
    if mutation in extra:
        headers.append(extra[mutation])
    response = api.client().post(api.base + "/checks", headers=headers, json=check_body())
    assert response.status_code >= 400
    assert api.calls == []
    assert "private" not in response.text


@pytest.mark.parametrize(
    "mutation",
    [
        "confirmation",
        "extra",
        "request_too_large",
        "assertion_empty",
        "assertion_operation",
        "assertion_extra",
        "assertion_limit",
        "digest",
        "scope",
        "input_type",
    ],
)
def test_exact_check_envelope_and_predeclared_assertions_are_required(api, mutation):
    body = check_body()
    if mutation == "confirmation":
        body["confirm"] = 1
    elif mutation == "extra":
        body["execute"] = True
    elif mutation == "request_too_large":
        body["request"]["input"]["prompt"] = "x" * 16_384
    elif mutation == "assertion_empty":
        body["acceptance"]["assertions"] = []
    elif mutation == "assertion_operation":
        body["acceptance"]["assertions"][0]["op"] = "eval"
    elif mutation == "assertion_extra":
        body["acceptance"]["assertions"][0]["regex"] = ".*"
    elif mutation == "assertion_limit":
        body["acceptance"]["assertions"] *= 17
    elif mutation == "digest":
        body["selectionSha256"] = "A" * 64
    elif mutation == "scope":
        body["principalId"] = str(uuid4())
    else:
        body["request"] = []
    response = api.client().post(api.base + "/checks", headers=api.headers, json=body)
    assert response.status_code >= 400
    assert api.calls == []


@pytest.mark.parametrize("extra", ["request", "acceptance", "result", "verified", "principalId"])
def test_launch_cannot_replace_checked_data_or_upload_a_verdict(api, extra):
    body = {"checkJobId": str(api.job), "bindingSha256": "b" * 64, "confirm": True, extra: {}}
    response = api.client().post(api.base + "/runs", headers=api.headers, json=body)
    assert response.status_code == 400 and api.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"confirm":true,"confirm":true}',
        '{"confirm":true,"value":NaN}',
        '{"confirm":true,"value":1e999}',
        '{"confirm":true,"value":"\\ud800"}',
        "[]",
        '"text"',
        '{"confirm":true,"nested":{"a":1,"a":2}}',
    ],
)
def test_ambiguous_or_invalid_json_is_rejected(api, raw):
    response = api.client().post(
        api.base + "/checks",
        headers={**api.headers, "Content-Type": "application/json"},
        content=raw,
    )
    assert response.status_code == 400 and api.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/jobs?limit=26",
        "/jobs?limit=01",
        "/jobs?limit=1&limit=2",
        "/jobs?after=",
        "/jobs?workspaceId=foreign",
        "/context?ignored=1",
        "/jobs/{job}/events?after=-1",
        "/jobs/{job}/events?limit=101",
        "/jobs/{job}/artifact?length=262145",
        "/jobs/{job}/artifact?offset=01",
        "/jobs/not-a-uuid",
        "/jobs/{job}/events?after=9007199254740992",
    ],
)
def test_bounded_queries_and_canonical_identities(api, path):
    response = api.client().get(api.base + path.format(job=api.job), headers=api.headers)
    assert response.status_code == 400 and api.calls == []


def test_body_response_and_disabled_setup_bounds(api):
    response = api.client().post(
        api.base + "/checks",
        headers={**api.headers, "Content-Type": "application/json"},
        content=b" " * (MAX_BODY_BYTES + 1),
    )
    assert response.status_code == 400 and api.calls == []
    response = api.client(setup=False).post(
        api.base + "/checks", headers=api.headers, json=check_body()
    )
    assert response.status_code >= 400 and api.calls == []
    api.response[0] = {"tooLarge": "x" * MAX_RESPONSE_BYTES}
    response = api.client().get(api.base + "/context", headers=api.headers)
    assert response.status_code == 400


def test_duplicate_content_type_is_rejected(api):
    response = api.client().post(
        api.base + "/checks",
        content=json.dumps(check_body()),
        headers=[
            *api.headers.items(),
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ],
    )
    assert response.status_code == 400 and api.calls == []
