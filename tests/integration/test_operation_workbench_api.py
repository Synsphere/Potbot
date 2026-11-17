"""HTTP-to-controller-to-worker acceptance using real PostgreSQL and mocked provider I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_operation_workbench import register_operation_workbench_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.controller.operation_catalogue import OperationCatalogue
from potbot.domain.errors import DomainError, ScopeDeniedError
from tests.integration.test_operation_checks import KEY, TOKEN, effects, hosted

pytestmark = pytest.mark.postgres


def test_http_checked_launch_worker_result_and_same_key_recovery(app_env):
    value = hosted(app_env)
    with app_env.db.transaction(value.actor) as connection:
        connection.execute(
            "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) "
            "VALUES(%s,%s,'fixture-model','reasoning')",
            (value.actor.workspace_id, uuid4()),
        )
    operation = next(
        item["id"]
        for item in OperationCatalogue(app_env.db).list(value.actor)["items"]
        if item["reference"] == value.reference
    )
    app = FastAPI()

    def authenticate(header):
        if header != "Bearer owned":
            raise ScopeDeniedError("untrusted test caller")
        return value.actor

    def setup(header):
        if header != TOKEN:
            raise ScopeDeniedError("missing test operator capability")

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
        app, app_env.db, authenticate, ("http://127.0.0.1:3100",), value.actor.workspace_id, setup
    )
    headers = {
        "Authorization": "Bearer owned",
        "Origin": "http://127.0.0.1:3100",
        "X-Potbot-Setup-Token": TOKEN,
        "Idempotency-Key": "http-check",
    }
    read_headers = {"Authorization": "Bearer owned"}
    path = f"/v1/ui/operations/{operation}"
    try:
        with TestClient(app) as client:
            contract = client.get(path + "/contract", headers=read_headers)
            assert contract.status_code == 200 and contract.json()["supported"]
            before = effects(value)
            check = client.post(path + "/checks", json=value.spec, headers=headers)
            assert check.status_code == 202
            check_id = check.json()["job"]["id"]
            assert effects(value) == before and value.requests == []
            checked = value.runner.run_once(value.actor, "http-check-worker")
            assert checked["id"] == check_id and checked["result"]["operation_check"]["eligible"]
            assert effects(value) == before and value.requests == []
            checked_response = client.get(path + f"/jobs/{check_id}", headers=read_headers)
            assert checked_response.json()["job"]["state"] == "succeeded"
            body = {"checkJobId": check_id, "request": value.spec, "confirm": True}
            run_headers = {**headers, "Idempotency-Key": "http-run"}
            run = client.post(path + "/runs", json=body, headers=run_headers)
            retry = client.post(path + "/runs", json=body, headers=run_headers)
            assert run.status_code == retry.status_code == 202
            assert run.json() == retry.json()
            assert value.requests == []
            job_id = run.json()["job"]["id"]
            finished = value.runner.run_once(value.actor, "http-run-worker")
            assert finished["id"] == job_id and finished["state"] == "succeeded"
            assert len(value.requests) == 1
            result = client.get(path + f"/jobs/{job_id}/result", headers=read_headers)
            events = client.get(path + f"/jobs/{job_id}/events", headers=read_headers)
            assert result.status_code == events.status_code == 200
            assert result.json()["output"] and result.json()["job"]["state"] == "succeeded"
            assert any(row["event_type"] == "job.output" for row in events.json()["items"])
            second = client.post(
                path + "/runs", json=body, headers={**headers, "Idempotency-Key": "another-run"}
            )
            assert second.status_code == 409
            assert value.runner.run_once(value.actor, "http-no-repeat") is None
            assert effects(value)["usage"] == 1
            fixture = {
                "contract": contract.json(),
                "check": check.json(),
                "checked": checked_response.json(),
                "run": run.json(),
                "result": result.json(),
                "events": events.json(),
            }
            public = json.dumps(fixture)
            assert TOKEN not in public and KEY not in public and "private task prompt" not in public
            assert "operation_authorization" not in public and '"mac"' not in public
            # Optional local contract handoff to the frontend projector tests.
            if target := os.environ.get("POTBOT_WORKBENCH_CONTRACT_FIXTURE"):
                Path(target).write_text(public + "\n", encoding="utf-8")
    finally:
        value.http.close()
