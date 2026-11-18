"""Complete selected hosted results with real SQL, queue and gateway receipts.

The provider uses an explicit HTTP fixture. Assertions verify retained fixture
text and receipt integrity; no model quality or live host qualification is claimed.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from potbot.api.ui_task_execution import register_task_execution_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.controller.task_execution_outcomes import MAX_OUTPUT_EVENTS
from potbot.domain.errors import DomainError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from tests.integration.test_hosted_gateway import wire
from tests.integration.test_task_execution import (
    checked,
    launch_checked,
    selected_hosted,
    selected_native,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def outcome_rig(app_env, tmp_path, monkeypatch):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    value.returned = ["model output"]
    value.finish = [True]
    value.reported_usage = [(12, 3)]
    value.http.close()

    def handler(request):
        value.requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=wire(value.returned, usage=value.reported_usage[0], complete=value.finish[0]),
        )

    value.http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    value.gateway.http_client = value.http
    try:
        yield value
    finally:
        value.http.close()


def completed(value):
    check = checked(value)
    launched = launch_checked(value, check)
    done = value.runner.run_once(value.actor, "result-fixture-worker")
    assert done["state"] == "succeeded", done
    assert done["id"] == launched["job"]["id"]
    return check, launched, done


def result(value, job):
    return value.service.result(value.actor, value.selection_id, UUID(job["id"]))


def test_real_owned_gateway_receipts_produce_repeatable_assertion_pass(outcome_rig):
    value = outcome_rig
    check, launched, done = completed(value)
    report = result(value, done)
    verification = report["verification"]
    assert verification["status"] == "passed", verification
    assert verification["bindingSha256"] == check["binding"]["sha256"]
    assert verification["acceptanceSha256"] == check["binding"]["acceptanceSha256"]
    assert verification["resultKind"] == "hosted_text"
    assert verification["checks"] == [{"index": 0, "passed": True, "reasonCode": None}]
    assert len(verification["outputSha256"]) == len(verification["evidenceSha256"]) == 64
    assert result(value, done)["verification"] == verification
    assert len(value.requests) == 1
    assert report["result"]["output"] == "model output"
    assert (
        value.service.launch(
            value.actor,
            value.selection_id,
            UUID(check["job"]["id"]),
            check["binding"]["sha256"],
            "selected-launch",
            setup_token=value.token[0],
        )
        == launched
    )
    assert len(value.requests) == 1


def test_acceptance_uses_complete_output_beyond_preview_limit(outcome_rig):
    value = outcome_rig
    value.returned[:] = ["a" * 70_000 + " reviewed tail"]
    value.acceptance["assertions"] = [
        {"op": "contains", "pointer": "/text", "value": "reviewed tail"},
        {"op": "length_equals", "pointer": "/text", "value": 70_014},
    ]
    _, _, done = completed(value)
    report = result(value, done)
    assert report["verification"]["status"] == "passed", report["verification"]
    assert report["result"]["truncated"] is True
    assert "reviewed tail" not in report["result"]["output"]
    assert len(value.requests) == 1


def test_successful_execution_can_fail_predeclared_acceptance(outcome_rig):
    value = outcome_rig
    value.returned[:] = ["The task succeeded according to this model, but the answer is wrong."]
    _, _, done = completed(value)
    report = result(value, done)
    assert report["result"]["job"]["state"] == "succeeded"
    assert report["verification"]["status"] == "failed"
    assert report["verification"]["reasonCodes"] == ["assertion_mismatch"]
    assert report["verification"]["checks"] == [
        {"index": 0, "passed": False, "reasonCode": "value_mismatch"}
    ]
    assert "according to this model" not in canonical_json(report["verification"])
    assert len(value.requests) == 1


def test_queued_and_check_jobs_cannot_become_positive_outcome_evidence(outcome_rig):
    value = outcome_rig
    check = checked(value)
    assert result(value, check["job"])["verification"]["status"] == "unavailable"
    launched = launch_checked(value, check)
    report = result(value, launched["job"])
    assert report["verification"]["status"] == "pending"
    assert report["verification"]["checks"] == []
    assert value.requests == []
    cancelled = value.service.cancel(
        value.actor, value.selection_id, UUID(launched["job"]["id"]), "cancel-outcome"
    )
    report = result(value, cancelled["job"])
    assert report["verification"]["status"] == "unavailable"
    assert report["verification"]["reasonCodes"] == ["execution_cancelled"]


def test_partial_provider_output_and_unsettled_usage_never_pass(outcome_rig):
    value = outcome_rig
    check = checked(value)
    launch_checked(value, check)
    value.finish[0] = False
    value.reported_usage[0] = None
    done = value.runner.run_once(value.actor, "partial-result-worker")
    assert done["state"] == "indeterminate"
    report = result(value, done)
    assert report["result"]["output"] == "model output"
    assert report["verification"]["status"] == "unavailable"
    assert report["verification"]["checks"] == []
    assert len(value.requests) == 1


@pytest.mark.parametrize("change", ["job", "gateway", "budget", "output_count", "owner_key"])
def test_modified_coordination_rows_cannot_replace_immutable_result_receipts(outcome_rig, change):
    value = outcome_rig
    _, _, done = completed(value)
    assert result(value, done)["verification"]["status"] == "passed"
    with value.service.database.transaction(value.actor) as connection:
        if change == "job":
            modified = deepcopy(done["result"])
            modified["completed_models"] = 0
            connection.execute(
                "UPDATE jobs SET result=%s WHERE id=%s", (Jsonb(modified), UUID(done["id"]))
            )
        elif change == "gateway":
            connection.execute(
                "UPDATE hosted_invocations SET result=jsonb_set(result,'{finish_reason}','\"length\"'::jsonb) WHERE id=%s",
                (UUID(done["result"]["invocation_ids"][0]),),
            )
        elif change == "budget":
            connection.execute(
                "UPDATE budget_reservations SET actual=jsonb_set(actual,'{calls}','0'::jsonb) "
                "WHERE reservation_key=%s",
                ("hosted:" + done["result"]["invocation_ids"][0],),
            )
        elif change == "output_count":
            connection.execute(
                "UPDATE jobs SET output_bytes=output_bytes+1 WHERE id=%s", (UUID(done["id"]),)
            )
        else:
            value.token[0] += "rotated"
    report = result(value, done)
    assert report["verification"]["status"] == "unavailable", report["verification"]
    assert report["verification"]["checks"] == []
    assert len(value.requests) == 1


def test_output_event_limit_never_silently_checks_a_partial_page(outcome_rig, monkeypatch):
    value = outcome_rig
    value.returned[:] = ["a" * 20_000]
    value.acceptance["assertions"][0].update(op="contains", value="a")
    _, _, done = completed(value)
    assert MAX_OUTPUT_EVENTS > 1
    monkeypatch.setattr("potbot.controller.task_execution_outcomes.MAX_OUTPUT_EVENTS", 1)
    report = result(value, done)
    assert report["verification"]["status"] == "unavailable"
    assert report["verification"]["reasonCodes"] == ["output_bound_or_type_unavailable"]
    assert report["verification"]["checks"] == []


def test_result_route_requires_exact_selection_job_join(outcome_rig):
    value = outcome_rig
    _, _, done = completed(value)
    with pytest.raises(ScopeDeniedError):
        value.service.result(value.actor, uuid4(), UUID(done["id"]))


@pytest.mark.parametrize("change", ["missing", "tampered"])
def test_native_artifact_loss_keeps_history_visible_without_a_positive_verdict(
    app_env, tmp_path, monkeypatch, change
):
    value = selected_native(app_env, tmp_path, monkeypatch)
    check = checked(value)
    launch_checked(value, check)
    done = value.runner.run_once(value.actor, "native-outcome-integrity")
    assert done["state"] == "succeeded", done
    before = result(value, done)
    assert before["verification"]["status"] == "passed", before["verification"]
    artifact = before["result"]["artifact"]
    from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
    from potbot.workspace_sandbox import load_workspace_sandbox

    root = load_workspace_sandbox(app_env.db, value.actor).path("artifacts")
    metadata = ArtifactStore(app_env.db, LocalArtifactBackend(root)).metadata(
        value.actor, UUID(artifact["id"])
    )
    path = root / metadata["locator"]
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed result data")
    after = result(value, done)
    assert after["verification"]["status"] == "unavailable"
    assert after["verification"]["checks"] == []
    assert after["result"]["job"]["state"] == "succeeded"
    assert after["result"]["artifact"] is None
    assert value.executions == ["syn://thing/fixture"]


def test_historical_result_cannot_be_reinterpreted_after_consumer_upgrade(outcome_rig, monkeypatch):
    value = outcome_rig
    _, _, done = completed(value)
    assert result(value, done)["verification"]["status"] == "passed"
    monkeypatch.setattr(
        "potbot.controller.task_execution.execution_consumer_sha256", lambda: "0" * 64
    )
    report = result(value, done)
    assert report["verification"]["status"] == "unavailable"
    assert report["verification"]["checks"] == []
    assert report["result"]["job"]["state"] == "succeeded"
    assert len(value.requests) == 1


def test_actual_http_lifecycle_exports_frontend_contract_fixture(outcome_rig):
    value = outcome_rig
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def error(request, failure):
        response = error_response(
            failure,
            semantics=OperationSemantics(
                read_only=request.method == "GET",
                idempotency_key=hasattr(request.state, "idempotency_key"),
            ),
        )
        return JSONResponse(response.body, status_code=response.status)

    def authenticate(token):
        assert token == "Bearer fixture-task-token"
        return value.actor

    def authorize(token):
        assert token == value.token[0]

    register_task_execution_routes(
        app,
        value.service.database,
        authenticate,
        ("https://fixture.example",),
        value.actor.workspace_id,
        authorize,
        service=value.service,
    )
    headers = {
        "Authorization": "Bearer fixture-task-token",
        "Origin": "https://fixture.example",
        "X-Potbot-Setup-Token": value.token[0],
        "Idempotency-Key": "http-selected-check",
    }
    base = f"/v1/ui/task-execution/selections/{value.selection_id}"
    with TestClient(app) as client:
        context = client.get(base + "/context", headers=headers)
        assert context.status_code == 200
        checked_response = client.post(
            base + "/checks",
            headers=headers,
            json={
                "selectionSha256": value.selection["sha256"],
                "request": value.spec,
                "acceptance": value.acceptance,
                "confirm": True,
            },
        )
        assert checked_response.status_code == 202, checked_response.text
        check = checked_response.json()
        worker_check = value.runner.run_once(value.actor, "http-selected-check")
        assert worker_check["result"]["operation_check"]["eligible"], worker_check
        checked_job = client.get(base + f"/jobs/{check['job']['id']}", headers=headers)
        launch = client.post(
            base + "/runs",
            headers={**headers, "Idempotency-Key": "http-selected-run"},
            json={
                "checkJobId": check["job"]["id"],
                "bindingSha256": check["binding"]["sha256"],
                "confirm": True,
            },
        )
        assert launch.status_code == 202, launch.text
        launched = launch.json()
        pending = client.get(base + f"/jobs/{launched['job']['id']}/result", headers=headers)
        done = value.runner.run_once(value.actor, "http-selected-worker")
        assert done["state"] == "succeeded", done
        final = client.get(base + f"/jobs/{done['id']}/result", headers=headers)
        assert final.status_code == 200, final.text
        assert final.json()["verification"]["status"] == "passed", final.json()["verification"]
        events = client.get(base + f"/jobs/{done['id']}/events", headers=headers)
        history = client.get(base + "/jobs", headers=headers)
        assert events.status_code == history.status_code == 200
        fixture = {
            "description": "Actual PostgreSQL/HTTP selected-task lifecycle; explicit synthetic provider transport.",
            "workspaceId": str(value.actor.workspace_id),
            "principalId": str(value.actor.principal_id),
            "selectionId": str(value.selection_id),
            "operationId": check["operationId"],
            "context": context.json(),
            "check": check,
            "checkedJob": checked_job.json(),
            "launch": launched,
            "pending": pending.json(),
            "result": final.json(),
            "events": events.json(),
            "jobs": history.json(),
        }
        assert len(value.requests) == 1
        destination = os.environ.get("POTBOT_TASK_EXECUTION_HTTP_FIXTURE")
        if destination:
            Path(destination).write_text(
                json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
