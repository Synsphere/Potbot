"""Qualification protocol fixtures never run a worker, claim a job or call a provider."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.operation_workbench import CHECK_SCHEMA, request_digest
from potbot.domain.serialization import canonical_json, content_hash

_PATH = Path(__file__).resolve().parents[2] / "scripts/qualify_operation_workbench.py"
_SPEC = importlib.util.spec_from_file_location("operation_workbench_qualification", _PATH)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def fixture(*, native=False, changed=None):
    actor = WorkspaceContext(uuid4(), uuid4())
    operation, episode, revision, check_id, run_id, artifact_id = (
        "a" * 64,
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
    )
    request = {
        "job_type": "cog.execute" if native else "model.generate",
        "episode_id": episode,
        "input": {
            "agent_id": str(uuid4()),
            "repository_state_id": str(uuid4()),
            "operation_ref": "syn://thing/fixture",
            "profile_sha256": "b" * 64,
            "type_ref": "syn://type/fixture",
            "value": {"private": "prompt-canary"},
        }
        if native
        else {"prompt": "prompt-canary", "max_output_tokens": 16},
        "selected_models": []
        if native
        else [{"provider": "openai", "model": "fixture", "role": "reasoning"}],
    }
    scope = {
        "schema": gate.WORKBENCH_SCHEMA,
        "workspaceId": str(actor.workspace_id),
        "principalId": str(actor.principal_id),
        "operationId": operation,
    }
    now = datetime.now(UTC)
    bindings = {
        "requestSha256": request_digest(request),
        "contractRevisionId": revision,
        "policySha256": "c" * 64,
    }
    if native:
        bindings.update(
            profileSha256="b" * 64,
            installationSha256="d" * 64,
            repositorySha256="e" * 64,
            grantGeneration=1,
            grantSha256="f" * 64,
            runtimeSha256="0" * 64,
            nativeSha256="1" * 64,
        )
    report = {
        "schema": CHECK_SCHEMA,
        "operationId": operation,
        "eligible": True,
        "checks": [{"code": "fixture_admission", "status": "passed"}],
        "bindings": bindings,
        "bindingSha256": content_hash(bindings),
        "checkedAt": now.isoformat(),
        "expiresAt": (now + timedelta(seconds=120)).isoformat(),
    }
    check = {
        "id": check_id,
        "job_type": "operation.check",
        "state": "queued",
        "owner_principal_id": str(actor.principal_id),
        "episode_id": episode,
        "contract_revision_id": revision,
        "operation_check_id": None,
        "selected_models": [],
        "dispatched_at": None,
        "result": None,
    }
    run = {
        **check,
        "id": run_id,
        "job_type": request["job_type"],
        "selected_models": request["selected_models"],
        "operation_check_id": check_id,
    }
    artifact_text = canonical_json(
        {"type_ref": "syn://type/result", "value": {"fixture": "output-canary"}}
    )
    calls, journal = [], []

    def handler(call):
        calls.append(call)
        path = call.url.path
        if path.endswith("/contract"):
            return httpx.Response(
                200, json={**scope, "supported": True, "jobType": request["job_type"]}
            )
        if call.method == "POST" and path.endswith("/checks"):
            assert journal[-1]["phase"] == "submitting_check"
            assert json.loads(call.content) == request
            return httpx.Response(202, json={**scope, "job": check})
        if call.method == "POST" and path.endswith("/runs"):
            assert (
                journal[-1]["phase"] == "submitting_run" and journal[-1]["checkJobId"] == check_id
            )
            assert json.loads(call.content) == {
                "checkJobId": check_id,
                "request": request,
                "confirm": True,
            }
            if changed == "lost_response":
                raise httpx.ReadTimeout("secret-canary in an untrusted response")
            return httpx.Response(202, json={**scope, "job": run})
        if path.endswith("/" + check_id):
            selected_scope = (
                {**scope, "principalId": str(uuid4())} if changed == "principal" else scope
            )
            selected_report = dict(report)
            if changed == "blocked":
                selected_report.update(
                    eligible=False, checks=[{"code": "task_budget", "status": "blocked"}]
                )
            if changed == "expired":
                selected_report.update(
                    checkedAt=(now - timedelta(seconds=121)).isoformat(),
                    expiresAt=(now - timedelta(seconds=1)).isoformat(),
                )
            if changed == "bindings":
                selected_report["bindings"] = {**bindings, "requestSha256": "9" * 64}
                selected_report["bindingSha256"] = content_hash(selected_report["bindings"])
            return httpx.Response(
                200,
                json={
                    **selected_scope,
                    "job": {
                        **check,
                        "state": "succeeded",
                        "result": {"operation_check": selected_report},
                    },
                },
            )
        if path.endswith("/" + run_id):
            state = (
                "running"
                if changed == "timeout"
                else "cancelled"
                if changed == "cancelled"
                else "succeeded"
            )
            return httpx.Response(
                200,
                json={**scope, "job": {**run, "state": state, "dispatched_at": now.isoformat()}},
            )
        if path.endswith("/result"):
            assert path.endswith(f"/jobs/{run_id}/result")
            value = {
                **scope,
                "job": {
                    **run,
                    "state": "succeeded",
                    "dispatched_at": now.isoformat(),
                    "result": {"artifact_id": artifact_id} if native else {"completed_models": 1},
                },
                "output": "" if native else "output-canary",
                "truncated": False,
                "artifact": {
                    "id": artifact_id,
                    "sha256": hashlib.sha256(artifact_text.encode()).hexdigest(),
                    "bytes": len(artifact_text.encode()),
                }
                if native
                else None,
                "artifactText": artifact_text if native else None,
                "artifactTruncated": False,
            }
            if changed == "artifact":
                value["artifactText"] += "tampered"
            return httpx.Response(200, json=value)
        if path.endswith("/events"):
            assert path.endswith(f"/jobs/{run_id}/events")
            rows = [
                {"job_id": run_id, "sequence": index, "event_type": kind}
                for index, kind in enumerate(("job.queued", "job.running", "job.succeeded"), 1)
            ]
            return httpx.Response(
                200, json={**scope, "items": rows, "next_cursor": 3, "has_more": False}
            )
        pytest.fail("The qualification attempted an unrelated route: " + path)

    return SimpleNamespace(
        actor=actor,
        operation=operation,
        request=request,
        calls=calls,
        journal=journal,
        handler=handler,
        check_id=check_id,
        run_id=run_id,
    )


def exercise(value, **options):
    with httpx.Client(
        base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(value.handler)
    ) as client:
        return gate.qualify(
            client,
            value.actor,
            value.operation,
            value.request,
            key="operator-selected-qualification",
            source={"fixture": True},
            journal=value.journal.append,
            confirm_run=True,
            allow_paid=True,
            confirm_disclosure=True,
            sleep=options.pop("sleep", lambda _: None),
            **options,
        )


@pytest.mark.parametrize("native", [False, True])
def test_exact_check_and_run_have_owned_results_and_no_queue_claims(native):
    value = fixture(native=native)
    result = exercise(value)
    assert result["qualifiedSmoke"] is True and result["qualifiedForProduction"] is False
    assert result["checkJobId"] == value.check_id and result["runJobId"] == value.run_id
    posts = [call for call in value.calls if call.method == "POST"]
    assert len(posts) == 2
    assert [call.headers["Idempotency-Key"] for call in posts] == [
        result["checkKey"],
        result["runKey"],
    ]
    serialized = json.dumps(value.journal)
    assert "prompt-canary" not in serialized and "output-canary" not in serialized
    assert value.journal[0]["phase"] == "intent_recorded"
    assert value.journal[-1] == result


@pytest.mark.parametrize("missing", ["confirm_run", "allow_paid", "confirm_disclosure"])
def test_missing_explicit_approval_never_contacts_the_api(missing):
    value = fixture()
    options = {"confirm_run": True, "allow_paid": True, "confirm_disclosure": True}
    options[missing] = False
    with httpx.Client(
        base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(value.handler)
    ) as client:
        with pytest.raises(ValueError, match="approval"):
            gate.qualify(
                client,
                value.actor,
                value.operation,
                value.request,
                key="operator-key",
                source={},
                journal=value.journal.append,
                **options,
            )
    assert value.calls == [] and value.journal == []


@pytest.mark.parametrize("changed", ["blocked", "expired", "principal", "bindings"])
def test_changed_or_blocked_check_never_submits_execution(changed):
    value = fixture(changed=changed)
    result = exercise(value)
    assert result["qualifiedSmoke"] is False and result["runJobId"] is None
    assert len([call for call in value.calls if call.method == "POST"]) == 1


def test_ambiguous_launch_response_retains_key_without_retry_or_sensitive_error():
    value = fixture(changed="lost_response")
    result = exercise(value)
    assert result["phase"] == "submission_uncertain" and result["qualifiedSmoke"] is False
    assert result["checkJobId"] == value.check_id and result["runJobId"] is None
    assert result["runKey"] == "operator-selected-qualification:run"
    assert len([call for call in value.calls if call.method == "POST"]) == 2
    assert "secret-canary" not in json.dumps(value.journal)


@pytest.mark.parametrize("changed", ["timeout", "cancelled"])
def test_lost_observation_or_cancellation_retains_exact_run_without_replay(changed):
    value = fixture(changed=changed)
    elapsed = [0]

    def sleep(_):
        elapsed[0] += 1

    result = exercise(value, clock=lambda: elapsed[0], sleep=sleep, timeout_seconds=5)
    assert result["qualifiedSmoke"] is False and result["runJobId"] == value.run_id
    assert len([call for call in value.calls if call.method == "POST"]) == 2


def test_tampered_native_result_cannot_pass_qualification():
    value = fixture(native=True, changed="artifact")
    assert exercise(value)["qualifiedSmoke"] is False


def test_report_journal_is_exclusive_private_and_retains_each_checkpoint(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "qualification.jsonl"
    journal = gate.Journal(path)
    try:
        journal({"phase": "intent"})
        journal({"phase": "known", "job": str(uuid4())})
        with pytest.raises(FileExistsError):
            gate.Journal(path)
    finally:
        journal.close()
    assert path.stat().st_mode & 0o777 == 0o600
    assert [json.loads(line)["phase"] for line in path.read_text().splitlines()] == [
        "intent",
        "known",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1",
        "http://user@localhost:80",
        "http://127.0.0.1:80/path",
        "http://127.0.0.1:80?token=secret",
    ],
)
def test_credentials_cannot_be_sent_to_nonlocal_or_redirecting_api_origins(url):
    with pytest.raises(ValueError):
        gate.loopback_url(url)
