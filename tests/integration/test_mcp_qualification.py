"""Durable MCP task qualification through real owners and a marked native peer.

PostgreSQL, suite admission, native host, gateway, task/candidate/budget owners,
result artifacts, stopped receipts and HTTP projections are production code.
Only the MCP process/peer and credential-store source are explicit fixtures;
these checks do not qualify a deployed server or native isolation installation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.errors import DomainError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.tasks import TaskStatus
from potbot.persistence.database import (
    Database,
    admin_connection,
    grant_application_role,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.gateway import ToolGateway
from potbot.tools.mcp_discovery_host import read_mcp_profile
from potbot.tools.mcp_protocol import McpCallResult
from potbot.tools.mcp_qualification_host import (
    McpQualificationHost,
    read_mcp_qualification_suite,
    read_mcp_task_binding,
)
from potbot.workspace_sandbox import WorkspaceSandbox
from tests.integration import test_mcp_discovery as discovery
from tests.integration.test_execution_gateway import setup, specification
from tests.unit.test_mcp_discovery_host import profile_document, write_profile

pytestmark = pytest.mark.postgres
rig = discovery.rig
runtime_rig = discovery.runtime_rig
__all__ = ["rig", "runtime_rig"]
QUERY_CANARY = "private-qualification-query-canary"
RESULT_CANARY = "private-qualification-result-canary"


def suite_document(*, expected=1, count=2):
    return {
        "schema": "potbot-mcp-qualification-suite/v1",
        "name": "Marked native fixture cases",
        "description": "Exercises actual task owners against explicit local peer outcomes.",
        "cases": [
            {
                "id": f"search-{number}",
                "title": f"Fixture search case {number}",
                "toolName": "search",
                "arguments": {"query": f"{QUERY_CANARY}-{number}"},
                "oracle": {
                    "schema": "potbot-mcp-result-oracle/v1",
                    "assertions": [
                        {
                            "op": "equals",
                            "pointer": "/structuredContent/matches",
                            "value": expected,
                        },
                        {"op": "length_equals", "pointer": "/structuredContent/items", "value": 1},
                        {"op": "contains", "pointer": "/content/0/text", "value": RESULT_CANARY},
                    ],
                },
            }
            for number in range(1, count + 1)
        ],
    }


@pytest.fixture
def qualification(rig, tmp_path, monkeypatch):
    # Imported here while the cooperating service implementation is installed;
    # collection itself never accesses native credentials or a provider.
    from potbot.controller.mcp_qualification import McpQualificationService

    d = rig
    private = tmp_path / "qualification-private"
    private.mkdir(mode=0o700)
    profile_path = write_profile(private / "profile.json", profile_document(d.installed, d.agent))
    profile = read_mcp_profile(d.actor, profile_path)
    d.configuration, d.installed = profile.configuration_sha256, profile.installed
    d.binding = discovery.admit(d, key="qualification-profile-binding")
    profile = read_mcp_profile(d.actor, profile_path, binding_id=UUID(d.binding["id"]))
    d.installed = profile.installed
    discovery.complete(d)
    selected_reference = discovery.reference(d)
    home = d.runtime.homes.lookup(d.actor.workspace_id, d.agent)
    WorkspaceSandbox.prepare(d.runtime.runtime.home / home.directory)
    execution = setup(d.env, tmp_path, tools=(profile.installed.definition,))
    d.env.artifact_root.chmod(0o700)
    receipts_root = private / "receipts"
    receipts_root.mkdir(mode=0o700)
    suite_path = write_profile(private / "suite.json", suite_document())
    suite = read_mcp_qualification_suite(suite_path)
    service = McpQualificationService(
        d.env.db, setup_token=lambda: d.token[0], clock=lambda: d.clock[0]
    )
    native = McpQualificationHost(
        d.env.db,
        d.host,
        artifacts_root=d.env.artifact_root,
        receipts_root=receipts_root,
        clock=lambda: d.clock[0],
    )
    result = SimpleNamespace(
        discovery=d,
        env=d.env,
        actor=d.actor,
        token=d.token,
        clock=d.clock,
        profile=profile,
        suite=suite,
        service=service,
        native=native,
        execution=execution,
        reference=selected_reference,
        task_binding=read_mcp_task_binding(
            d.env.db,
            d.actor,
            episode_id=execution.episode_id,
            repository_state_id=execution.state["id"],
        ),
        starts=[],
        calls=[],
        returned_matches=1,
        tool_error=False,
        before_approve_hook=lambda: None,
        call_hook=lambda: None,
        record_hook=lambda _event: None,
    )
    monkeypatch.setattr(
        "potbot.service_credentials.ServiceCredentials",
        lambda: SimpleNamespace(store=SimpleNamespace(get=lambda _name: d.token[0])),
    )

    class ExplicitNativeExecutionClient:
        """Marked process/peer seam; actual gateway creates every retained receipt."""

        def __init__(self, **config):
            self.config = config

        async def __aenter__(self):
            result.starts.append(self.config["managed_scope"].run_id)
            return self

        async def __aexit__(self, *_):
            return None

        async def call(self, name, arguments):
            assert name == profile.installed.tool.name
            assert arguments in [case.arguments for case in result.suite.cases]
            result.before_approve_hook()
            self.config["approve_call"](profile.installed.tool, arguments)
            result.call_hook()
            result.calls.append((name, arguments))
            return McpCallResult.from_wire(
                name,
                {
                    "content": [{"type": "text", "text": RESULT_CANARY}],
                    "structuredContent": {
                        "matches": result.returned_matches,
                        "items": [{"title": "Retained fixture search result"}],
                    },
                    "isError": result.tool_error,
                },
            )

        async def close(self):
            return d.exit

    monkeypatch.setattr(
        "potbot.tools.mcp_gateway.SandboxedMcpClient", ExplicitNativeExecutionClient
    )
    result.admitted = admit_suite(result)
    return result


def admit_suite(r, *, key="admit-suite"):
    return r.service.admit(
        r.actor,
        r.profile,
        r.suite,
        r.execution.episode_id,
        r.execution.state["id"],
        key,
        setup_token=r.token[0],
    )["suite"]


def current(r):
    return r.service.suite(r.actor, UUID(r.admitted["id"]))["suite"]


def request(r, *, key="request"):
    selected = current(r)
    return r.service.request(
        r.actor,
        UUID(selected["id"]),
        selected["revision"],
        selected["bindingSha256"],
        key,
        setup_token=r.token[0],
    )["attempt"]


def claim(r, attempt, *, key="claim", recovery=False, fresh_only=False):
    return r.service.claim(
        r.actor,
        UUID(attempt["id"]),
        attempt["revision"],
        attempt["bindingSha256"],
        key,
        setup_token=r.token[0],
        recovery=recovery,
        fresh_only=fresh_only,
    )


def control(r, action, *, key=None):
    selected = current(r)
    return r.service.control(
        r.actor,
        UUID(selected["id"]),
        action,
        selected["revision"],
        selected["bindingSha256"],
        key or action,
        setup_token=r.token[0],
    )["suite"]


def native_report(r, attempt, claimed, *, recover=False):
    identity, worker = UUID(attempt["id"]), UUID(claimed["workerToken"])

    def record(event):
        r.service.record_case(r.actor, identity, worker, event)
        r.record_hook(event)

    return asyncio.run(
        (r.native.recover if recover else r.native.run)(
            r.actor,
            attempt_id=identity,
            profile=r.profile,
            suite=r.suite,
            reference=claimed["reference"],
            task_binding=r.task_binding,
            authorize_attempt=lambda: r.service.validate_claim(r.actor, identity, worker) and None,
            record_case=record,
            load_case=lambda case_id: r.service.case(r.actor, identity, worker, case_id),
        )
    )


def complete(r, *, key="run", attempt=None, claimed=None, recover=False):
    attempt = attempt or request(r, key=key + "-request")
    claimed = claimed or claim(r, attempt, key=key + "-claim", recovery=recover)
    report = native_report(r, attempt, claimed, recover=recover)
    return r.service.complete(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), report)[
        "attempt"
    ]


def test_admission_exposes_hashes_and_task_binding_without_native_execution(qualification):
    r = qualification
    admitted = current(r)
    assert admitted["approvalValid"] and admitted["authorityCurrent"]
    assert admitted["state"] == "enabled"
    assert admitted["taskBinding"] == r.task_binding
    assert admitted["suiteSha256"] == r.suite.sha256
    assert admitted["suiteFileSha256"] == r.suite.file_sha256
    assert admitted["cases"] == [case.summary() for case in r.suite.cases]
    assert admitted["lastReportId"] is None and admitted["activeAttemptId"] is None
    assert admit_suite(r) == r.admitted
    public = canonical_json(r.service.suites(r.actor))
    for forbidden in (
        QUERY_CANARY,
        RESULT_CANARY,
        str(r.suite.path),
        str(r.profile.private_paths[0]),
    ):
        assert forbidden not in public
    assert r.starts == [] and r.calls == []


@pytest.mark.parametrize("change", ["task", "catalogue"])
def test_exact_admission_retry_returns_original_receipt_after_authority_changes(
    qualification, change
):
    r = qualification
    if change == "task":
        r.execution.tasks.transition(
            r.actor,
            r.execution.episode_id,
            r.execution.task["update_token"],
            TaskStatus.PAUSED,
            "Pause after durable suite admission",
            idempotency_key="pause-after-suite-admission",
        )
    else:
        discovery.complete(
            r.discovery, key="replace-catalogue-after-admission", variant="changed_schema"
        )
    assert admit_suite(r) == r.admitted
    assert not current(r)["authorityCurrent"]
    with pytest.raises(DomainError):
        admit_suite(r, key="new-admission-requires-current-authority")
    assert r.starts == [] and r.calls == []


def test_actual_native_host_finishes_each_case_with_gateway_artifacts_and_stopped_receipts(
    qualification,
):
    r = qualification
    completed = complete(r)
    assert completed["status"] == "succeeded"
    assert completed["completedCases"] == completed["passedCases"] == 2
    assert completed["failedCases"] == 0
    assert len(r.starts) == len(r.calls) == 2
    report = r.service.report(r.actor, UUID(completed["reportId"]))["report"]
    assert report["approvalValid"] and report["complete"] and report["passed"]
    assert report["authorityCurrent"]
    assert report["evidence"]["taskBinding"] == r.task_binding
    for case in completed["cases"]:
        assert case["status"] == "passed" and all(check["passed"] for check in case["checks"])
        selected = r.execution.store.inspect(r.actor, UUID(case["actionId"]))
        assert selected["run"]["id"] == case["runId"]
        receipt = r.native.receipts.read(r.actor.workspace_id, UUID(case["runId"]))
        assert receipt["processes_stopped"] is True
        assert content_hash(receipt) == case["receiptSha256"]
        raw = r.native.artifacts.read(r.actor, UUID(case["resultArtifactId"]))
        assert hashlib.sha256(raw).hexdigest() == case["resultSha256"]
        assert RESULT_CANARY in raw.decode()
    assert QUERY_CANARY not in canonical_json(report)
    assert RESULT_CANARY not in canonical_json(report)


def test_completed_assertion_failure_is_retained_without_publishing_success(qualification):
    r = qualification
    r.returned_matches = 2
    completed = complete(r)
    assert completed["status"] == "failed"
    assert completed["completedCases"] == completed["failedCases"] == 2
    assert completed["passedCases"] == 0 and len(r.calls) == 2
    report = r.service.report(r.actor, UUID(completed["reportId"]))["report"]
    assert report["complete"] and not report["passed"] and report["approvalValid"]
    for case in report["cases"]:
        assert case["status"] == "failed" and case["reasonCode"] == "assertion_mismatch"
        assert case["checks"][0] == {"index": 0, "passed": False, "reasonCode": "value_mismatch"}


def test_peer_error_is_stopped_failed_evidence_and_never_a_passed_oracle(qualification):
    r = qualification
    r.tool_error = True
    completed = complete(r)
    assert completed["status"] == "failed" and completed["passedCases"] == 0
    assert completed["reasonCode"] == "tool_failed"
    assert len(r.calls) == 2
    report = r.service.report(r.actor, UUID(completed["reportId"]))["report"]
    assert report["complete"] and not report["passed"]
    assert all(
        case["checks"] is None and case["reasonCode"] == "tool_failed" for case in report["cases"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "suite-file",
        "case-digest",
        "stale-clock",
        "runtime",
        "image",
        "qualification",
        "home",
    ],
)
def test_native_report_publication_requires_the_exact_retained_lifetime(qualification, change):
    r = qualification
    attempt = request(r)
    claimed = claim(r, attempt)
    observed = native_report(r, attempt, claimed)
    altered = dict(observed)
    if change == "stale-clock":
        r.clock[0] += timedelta(seconds=31)
    else:
        key = {
            "attempt": "attemptId",
            "suite-file": "suiteFileSha256",
            "case-digest": "casesSha256",
            "runtime": "runtimeSha256",
            "image": "imageSha256",
            "qualification": "qualificationSha256",
            "home": "agentHomeRevision",
        }[change]
        altered[key] = str(uuid4()) if change == "attempt" else 99 if change == "home" else "0" * 64
    with pytest.raises(DomainError):
        r.service.complete(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), altered)
    assert len(r.calls) == 2
    with r.env.db.transaction(r.actor) as connection:
        assert connection.execute("SELECT * FROM mcp_qualification_reports").fetchall() == []


def test_claim_and_cancel_are_exact_fenced_commands_without_dispatch(qualification):
    r = qualification
    attempt = request(r)
    inputs = (r.actor, UUID(attempt["id"]), attempt["revision"], attempt["bindingSha256"], "claim")
    assert r.service.execution_replay(*inputs, setup_token=r.token[0]) is None
    claimed = claim(r, attempt, fresh_only=True)
    assert "workerToken" in claimed
    replay = r.service.execution_replay(*inputs, setup_token=r.token[0])
    assert replay["attempt"]["status"] == "running" and "workerToken" not in canonical_json(replay)
    assert claim(r, attempt, fresh_only=True) == {"replay": replay}
    with pytest.raises(RevisionConflictError):
        r.service.execution_replay(*(*inputs[:3], "0" * 64, "claim"), setup_token=r.token[0])
    running = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
    cancelled = r.service.cancel(
        r.actor,
        UUID(running["id"]),
        running["revision"],
        running["bindingSha256"],
        "cancel",
        setup_token=r.token[0],
    )["attempt"]
    assert cancelled["status"] == "cancelled" and cancelled["reportId"] is None
    with pytest.raises(DomainError):
        r.service.validate_claim(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]))
    assert r.starts == [] and r.calls == []


def assert_untrusted_attempt(value, *, active=False):
    assert (
        value["status"] in {"requested", "running", "recovering"}
        if active
        else value["status"] == "indeterminate"
    )
    assert value["completedCases"] == value["passedCases"] == value["failedCases"] == 0
    assert value["reasonCode"] == "admission_invalid"
    assert value["reportId"] is None and value["reportSha256"] is None
    assert not value["recoveryAvailable"]
    assert value["actions"] == (["cancel"] if active else [])
    for case in value["cases"]:
        assert case["status"] == "indeterminate" and case["reasonCode"] == "admission_invalid"
        assert all(
            case[field] is None
            for field in (
                "actionId",
                "candidateId",
                "reservationId",
                "runId",
                "resultArtifactId",
                "resultSha256",
                "receiptSha256",
                "checks",
                "elapsedMs",
            )
        )


def forge_passed_projection(r, attempt_id):
    """Attack an existing failed row; this never creates native qualification."""
    with r.env.db.transaction(r.actor) as connection:
        payload = connection.execute(
            "SELECT payload FROM mcp_qualification_attempts WHERE id=%s", (UUID(attempt_id),)
        ).fetchone()["payload"]
        for event in payload["cases"].values():
            assert event["phase"] == "finished" and not event["resultCheck"]["passed"]
            event["resultCheck"]["passed"] = True
            for check in event["resultCheck"]["checks"]:
                check.update(passed=True, reasonCode=None)
            event["reasonCode"] = None
        payload["reasonCode"] = None
        connection.execute(
            "UPDATE mcp_qualification_attempts SET state='succeeded',payload=%s WHERE id=%s",
            (Jsonb(payload), UUID(attempt_id)),
        )
    return payload


def test_concurrent_native_claims_have_exactly_one_fresh_winner(qualification):
    r = qualification
    attempt = request(r)

    def acquire(key):
        try:
            return claim(r, attempt, key=key, fresh_only=True)
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(acquire, ("worker-one", "worker-two")))
    winners = [value for value in results if isinstance(value, dict)]
    assert len(winners) == 1 and "workerToken" in winners[0]
    assert sum(isinstance(value, DomainError) for value in results) == 1
    assert r.starts == [] and r.calls == []


@pytest.mark.parametrize("action", ["disable", "revoke"])
def test_suite_controls_fence_claims_without_asserting_a_native_stop(qualification, action):
    r = qualification
    attempt = request(r)
    claimed = claim(r, attempt)
    controlled = control(r, action)
    assert controlled["state"] == ("disabled" if action == "disable" else "revoked")
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert r.starts == [] and r.calls == []
    retained = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
    assert retained["reportId"] is None
    if action == "disable":
        assert control(r, "enable")["state"] == "enabled"
    else:
        with pytest.raises(DomainError):
            control(r, "enable")


@pytest.mark.parametrize("action", ["cancel", "revoke"])
@pytest.mark.parametrize("phase", ["approve", "result"])
def test_claim_fence_at_actual_gateway_boundary_never_publishes_stopped_success(
    qualification, action, phase
):
    r = qualification
    attempt = request(r)
    claimed = claim(r, attempt)

    def fence():
        if action == "revoke":
            control(r, "revoke")
        else:
            selected = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
            r.service.cancel(
                r.actor,
                UUID(selected["id"]),
                selected["revision"],
                selected["bindingSha256"],
                "cancel-at-native-boundary",
                setup_token=r.token[0],
            )

    if phase == "approve":
        r.before_approve_hook = fence
    else:
        r.call_hook = fence
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert len(r.starts) == 1
    assert len(r.calls) == (0 if phase == "approve" else 1)
    retained = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
    assert retained["reportId"] is None and retained["completedCases"] == 0


def test_rotating_native_key_never_reapproves_prior_suite_evidence(qualification):
    r = qualification
    r.token[0] = "rotated-qualification-native-" + "b" * 48
    stale = current(r)
    assert not stale["approvalValid"] and not stale["authorityCurrent"]
    assert stale["actions"] == ["revoke"]
    with pytest.raises(DomainError):
        request(r)
    with pytest.raises(DomainError):
        control(r, "disable")
    assert control(r, "revoke")["state"] == "revoked"


def test_workspace_rls_and_principal_scope_hide_suites_attempts_and_reports(qualification):
    from potbot.controller.mcp_qualification import McpQualificationService

    r = qualification
    completed = complete(r)
    foreign = McpQualificationService(r.env.other_db, setup_token=lambda: r.token[0])
    assert foreign.suites(r.env.other_context)["items"] == []
    assert foreign.attempts(r.env.other_context)["items"] == []
    for function, identity in (
        (foreign.suite, r.admitted["id"]),
        (foreign.attempt, completed["id"]),
        (foreign.report, completed["reportId"]),
    ):
        with pytest.raises(ScopeDeniedError):
            function(r.env.other_context, UUID(identity))
    with r.env.other_db.transaction(r.env.other_context) as connection:
        for table in (
            "mcp_qualification_suites",
            "mcp_qualification_attempts",
            "mcp_qualification_reports",
            "mcp_qualification_events",
        ):
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == []


@pytest.mark.parametrize("target", ["suite", "attempt"])
def test_forged_rows_and_event_heads_cannot_create_native_qualification_authority(
    qualification, target
):
    from potbot.controller.mcp_qualification import _body

    r = qualification
    attempt = request(r)
    claimed = claim(r, attempt)
    table, column, identity = (
        ("mcp_qualification_suites", "config", r.admitted["id"])
        if target == "suite"
        else ("mcp_qualification_attempts", "payload", attempt["id"])
    )
    with r.env.db.transaction(r.actor) as connection:
        changed = connection.execute(
            f"UPDATE {table} SET revision=revision+1,"
            f"{column}=jsonb_set({column},'{{suiteSha256}}',%s) WHERE id=%s RETURNING *",
            (Jsonb("0" * 64), UUID(identity)),
        ).fetchone()
        connection.execute(
            "INSERT INTO mcp_qualification_events(workspace_id,id,principal_id,subject_kind,"
            "subject_id,revision,action,binding_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                r.actor.workspace_id,
                uuid4(),
                r.actor.principal_id,
                target,
                UUID(identity),
                changed["revision"],
                "unsigned_fixture_claim",
                content_hash(_body(target, changed)),
            ),
        )
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert r.starts == [] and r.calls == []
    if target == "suite":
        assert not current(r)["approvalValid"]
        assert current(r)["actions"] == ["revoke"]
    with r.env.db.transaction(r.actor) as connection:
        assert connection.execute("SELECT * FROM mcp_qualification_reports").fetchall() == []


@pytest.mark.parametrize("target", ["suite", "attempt"])
def test_untrusted_signed_owner_never_projects_forged_success_or_positive_case_evidence(
    qualification, target
):
    r = qualification
    if target == "attempt":
        r.returned_matches = 2
    completed = complete(r)
    if target == "suite":
        with r.env.db.transaction(r.actor) as connection:
            connection.execute(
                "UPDATE mcp_qualification_suites SET mac=%s WHERE id=%s",
                ("0" * 64, UUID(r.admitted["id"])),
            )
    else:
        forged = forge_passed_projection(r, completed["id"])
    projected = r.service.attempt(r.actor, UUID(completed["id"]))["attempt"]
    assert_untrusted_attempt(projected)
    assert projected["revision"] == completed["revision"]
    assert [(case["caseSha256"], case["oracleSha256"]) for case in projected["cases"]] == [
        (case.sha256, content_hash(case.oracle)) for case in r.suite.cases
    ]
    if target == "attempt":
        # The safe public projection must not rewrite or erase retained private
        # evidence, even when that private evidence requires native inspection.
        with r.env.db.transaction(r.actor) as connection:
            retained = connection.execute(
                "SELECT payload FROM mcp_qualification_attempts WHERE id=%s",
                (UUID(completed["id"]),),
            ).fetchone()["payload"]
        assert retained == forged


def test_restoring_signed_suite_bytes_does_not_restore_old_event_authority(qualification):
    r = qualification
    with r.env.db.transaction(r.actor) as connection:
        original = connection.execute(
            "SELECT * FROM mcp_qualification_suites WHERE id=%s", (UUID(r.admitted["id"]),)
        ).fetchone()
    control(r, "disable")
    with r.env.db.transaction(r.actor) as connection:
        connection.execute(
            "UPDATE mcp_qualification_suites SET revision=%s,state=%s,mac=%s WHERE id=%s",
            (original["revision"], original["state"], original["mac"], original["id"]),
        )
    assert not current(r)["approvalValid"]
    with pytest.raises(DomainError):
        request(r)


@pytest.mark.parametrize(
    "change",
    [
        "profile-bytes",
        "suite-bytes",
        "consumer",
        "task-paused",
        "task-revised",
        "repository",
        "discovery",
        "expired",
    ],
)
def test_current_native_and_task_authority_is_rechecked_before_any_case_dispatch(
    qualification, monkeypatch, change
):
    r = qualification
    attempt = request(r)
    claimed = claim(r, attempt)
    if change in {"profile-bytes", "suite-bytes"}:
        path = r.profile.private_paths[0] if change == "profile-bytes" else r.suite.path
        path.write_text(json.dumps(json.loads(path.read_text()), indent=2) + "\n", encoding="utf-8")
    elif change == "consumer":
        for module in (
            "potbot.controller.mcp_qualification",
            "potbot.tools.mcp_qualification_host",
        ):
            monkeypatch.setattr(module + ".qualification_consumer_sha256", lambda: "0" * 64)
    elif change == "task-paused":
        r.execution.tasks.transition(
            r.actor,
            r.execution.episode_id,
            r.execution.task["update_token"],
            TaskStatus.PAUSED,
            "Qualification fixture pauses its existing task",
            idempotency_key="pause-before-qualification",
        )
    elif change == "task-revised":
        r.execution.tasks.revise(
            r.actor,
            r.execution.episode_id,
            r.execution.contract_id,
            replace(
                specification(), objective="Review the changed task contract before qualification"
            ),
            "revise-before-qualification",
        )
    elif change == "repository":
        # Existing states are immutable even to this workspace's application
        # principal. A valid newly captured state still cannot replace the one
        # admitted by this qualification suite.
        with pytest.raises(ScopeDeniedError), r.env.db.transaction(r.actor) as connection:
            connection.execute(
                "UPDATE repository_states SET content_digest=%s WHERE id=%s",
                ("0" * 64, r.execution.state["id"]),
            )
        calculator = r.execution.fixture.path / "calculator.py"
        calculator.write_text(
            calculator.read_text() + "\n# Alternate qualification repository state\n"
        )
        other = r.execution.states.capture(
            r.actor,
            r.env.repository_id,
            r.execution.fixture.path,
            r.execution.state["environment_id"],
        )
        assert other["id"] != r.execution.state["id"]
        r.task_binding = read_mcp_task_binding(
            r.env.db, r.actor, episode_id=r.execution.episode_id, repository_state_id=other["id"]
        )
    elif change == "discovery":
        discovery.complete(r.discovery, key="changed-discovery", variant="changed_schema")
    else:
        r.clock[0] += timedelta(seconds=61)
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert r.starts == [] and r.calls == []
    retained = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
    assert retained["reportId"] is None


def test_task_budget_is_shared_and_exhaustion_cannot_start_native_cases(qualification):
    r = qualification
    r.execution.budgets.reserve(
        r.actor, r.execution.episode_id, {"calls": 30}, "fixture-consumes-existing-call-allowance"
    )
    attempt = request(r)
    claimed = claim(r, attempt)
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert r.starts == [] and r.calls == []
    totals = r.execution.budgets.inspect(r.actor, r.execution.episode_id)
    assert totals["held"]["calls"] == 30 and totals["used"].get("calls", 0) == 0
    with r.env.db.transaction(r.actor) as connection:
        assert connection.execute("SELECT * FROM mcp_qualification_reports").fetchall() == []


def test_stopped_gateway_cases_settle_usage_in_existing_task_budget(qualification):
    r = qualification
    complete(r)
    totals = r.execution.budgets.inspect(r.actor, r.execution.episode_id)
    assert totals["used"]["calls"] == 2
    assert totals["held"].get("calls", 0) == 0
    assert totals["held"].get("concurrency", 0) == 0
    assert totals["available"]["calls"] == 28


def test_substituted_frozen_input_bytes_never_reach_native_dispatch(qualification):
    r = qualification

    def corrupt(event):
        if event["phase"] == "prepared":
            locator = r.native.artifacts.backend.locator(r.actor.workspace_id, event["inputSha256"])
            (r.env.artifact_root / locator).write_bytes(b"{}")

    r.record_hook = corrupt
    attempt = request(r)
    claimed = claim(r, attempt)
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert r.starts == [] and r.calls == []


@pytest.mark.parametrize("change", ["artifact", "receipt-stdout", "receipt-stop", "receipt-intent"])
def test_changed_result_or_stop_receipt_cannot_become_a_qualification_report(
    qualification, monkeypatch, change
):
    r = qualification
    original = r.native._case_result

    def corrupt(gateway, context, case, prepared, profile, reference, task_binding):
        selected = gateway.store.inspect(context, UUID(prepared["actionId"]))
        completion, run = selected["completion"], selected["run"]
        if change == "artifact":
            raw = r.native.artifacts.read(context, UUID(completion["stdout_artifact_id"]))
            locator = r.native.artifacts.backend.locator(
                context.workspace_id, hashlib.sha256(raw).hexdigest()
            )
            (r.env.artifact_root / locator).write_bytes(b"{}")
        else:
            identity = UUID(run["id"])
            receipt = r.native.receipts.read(context.workspace_id, identity)
            key, value = {
                "receipt-stdout": ("stdout", "e30="),
                "receipt-stop": ("processes_stopped", False),
                "receipt-intent": ("mcp_intent_hash", "0" * 64),
            }[change]
            receipt[key] = value
            path = r.native.receipts.root / str(context.workspace_id) / f"{identity}.json"
            path.write_text(canonical_json(receipt), encoding="utf-8")
        return original(gateway, context, case, prepared, profile, reference, task_binding)

    monkeypatch.setattr(r.native, "_case_result", corrupt)
    attempt = request(r)
    claimed = claim(r, attempt)
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert len(r.calls) == 1
    with r.env.db.transaction(r.actor) as connection:
        assert connection.execute("SELECT * FROM mcp_qualification_reports").fetchall() == []


def test_missing_stop_evidence_keeps_execution_and_budget_indeterminate(qualification):
    r = qualification
    r.discovery.exit = replace(r.discovery.exit, processes_stopped=False)
    attempt = request(r)
    claimed = claim(r, attempt)
    with pytest.raises(DomainError):
        native_report(r, attempt, claimed)
    assert len(r.calls) == 1
    case = r.service.case(
        r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), r.suite.cases[0].id
    )
    selected = r.execution.store.inspect(r.actor, UUID(case["actionId"]))
    assert selected["run"]["status"] == "indeterminate"
    assert r.native.receipts.read(r.actor.workspace_id, UUID(selected["run"]["id"])) is None
    totals = r.execution.budgets.inspect(r.actor, r.execution.episode_id)
    assert totals["held"]["calls"] == 1 and totals["used"].get("calls", 0) == 0


def interrupt_after_stopped_receipt(r, monkeypatch):
    original = ToolGateway.execute_mcp

    def lost_acknowledgment():
        raise OSError("Explicit fixture loss after a real stopped receipt was persisted")

    async def lose_acknowledgment(gateway, *args, **kwargs):
        return await original(gateway, *args, **kwargs, after_receipt=lost_acknowledgment)

    monkeypatch.setattr(ToolGateway, "execute_mcp", lose_acknowledgment)
    attempt = request(r)
    claimed = claim(r, attempt)
    with pytest.raises(OSError, match="real stopped receipt"):
        native_report(r, attempt, claimed)
    assert len(r.calls) == 1
    return attempt, claimed


def test_lost_receipt_acknowledgment_recovers_existing_result_without_redispatch(
    qualification, monkeypatch
):
    r = qualification
    attempt, claimed = interrupt_after_stopped_receipt(r, monkeypatch)
    failed = r.service.fail(
        r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), "qualification_indeterminate"
    )["attempt"]
    assert failed["status"] == "indeterminate" and failed["recoveryAvailable"]
    recovering = claim(r, failed, key="recover-after-receipt", recovery=True)
    completed = complete(r, attempt=failed, claimed=recovering, recover=True)
    assert len(r.calls) == len(r.starts) == 1
    assert completed["passedCases"] == 1 and completed["completedCases"] == 1
    assert completed["status"] != "succeeded"
    report = r.service.report(r.actor, UUID(completed["reportId"]))["report"]
    assert not report["complete"] and not report["passed"]
    assert report["cases"][0]["status"] == "passed"
    assert report["cases"][1]["status"] == "not_run"
    totals = r.execution.budgets.inspect(r.actor, r.execution.episode_id)
    assert totals["used"]["calls"] == 1 and totals["held"].get("calls", 0) == 0
    assert (
        claim(r, failed, key="recover-after-receipt", recovery=True, fresh_only=True)["replay"][
            "attempt"
        ]["id"]
        == completed["id"]
    )


def test_recovery_cannot_relabel_an_old_receipt_with_a_changed_native_runtime(
    qualification, monkeypatch
):
    r = qualification
    attempt, claimed = interrupt_after_stopped_receipt(r, monkeypatch)
    failed = r.service.fail(
        r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), "qualification_indeterminate"
    )["attempt"]
    # This marked operator fixture changes the native identity without replacing
    # the actual gateway receipt or its old run and reservation owners.
    monkeypatch.setattr(
        r.discovery.host.backend,
        "operator_attachment",
        SimpleNamespace(
            require_healthy=lambda: {"epoch": "new-fixture-operator-epoch"}, healthy=lambda: True
        ),
    )
    recovering = claim(r, failed, key="recover-after-runtime-change", recovery=True)
    with pytest.raises(DomainError):
        native_report(r, failed, recovering, recover=True)
    assert len(r.calls) == len(r.starts) == 1
    assert r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]["reportId"] is None


@pytest.mark.parametrize("target", ["suite", "attempt"])
def test_retiring_invalid_admission_never_launders_prepared_work_into_recovery(
    qualification, monkeypatch, target
):
    r = qualification
    attempt, _claimed = interrupt_after_stopped_receipt(r, monkeypatch)
    table, identity = (
        ("mcp_qualification_suites", r.admitted["id"])
        if target == "suite"
        else ("mcp_qualification_attempts", attempt["id"])
    )
    with r.env.db.transaction(r.actor) as connection:
        connection.execute(f"UPDATE {table} SET mac=%s WHERE id=%s", ("0" * 64, UUID(identity)))
    assert_untrusted_attempt(
        r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"], active=True
    )
    if target == "suite":
        assert control(r, "revoke")["state"] == "revoked"
    else:
        selected = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
        r.service.cancel(
            r.actor,
            UUID(selected["id"]),
            selected["revision"],
            selected["bindingSha256"],
            "retire-untrusted-attempt",
            setup_token=r.token[0],
        )
    retained = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
    assert_untrusted_attempt(retained)
    assert not retained["recoveryAvailable"] and retained["reportId"] is None
    with pytest.raises(DomainError):
        claim(r, retained, key="forbidden-laundered-recovery", recovery=True)
    assert len(r.calls) == 1


def test_recovering_older_finished_work_cannot_displace_newer_failed_evidence(qualification):
    r = qualification
    older = request(r, key="older-pass-request")
    claimed = claim(r, older, key="older-pass-claim")
    native_report(r, older, claimed)
    # Real gateway cases completed, but publication of their native report was
    # interrupted. A later operator request obtains newer failed observations.
    interrupted = r.service.fail(
        r.actor, UUID(older["id"]), UUID(claimed["workerToken"]), "qualification_failed"
    )["attempt"]
    assert interrupted["passedCases"] == 2 and interrupted["reportId"] is None
    r.returned_matches = 2
    newer = complete(r, key="newer-failure")
    assert newer["status"] == "failed" and len(r.calls) == 4
    recovering = claim(r, interrupted, key="recover-historical-pass", recovery=True)
    assert current(r)["lastAttemptId"] == newer["id"]
    recovered = complete(r, attempt=interrupted, claimed=recovering, recover=True)
    assert recovered["status"] == "succeeded" and len(r.calls) == 4
    selected = current(r)
    assert selected["lastAttemptId"] == newer["id"]
    assert selected["lastReportId"] == newer["reportId"]
    historical = r.service.report(r.actor, UUID(recovered["reportId"]))["report"]
    assert historical["passed"] and historical["approvalValid"]
    assert not historical["authorityCurrent"] and "report_superseded" in historical["reasonCodes"]
    latest = r.service.report(r.actor, UUID(newer["reportId"]))["report"]
    assert not latest["passed"] and latest["authorityCurrent"]


def test_recovery_with_no_prepared_case_never_dispatches_missing_work(qualification):
    r = qualification
    attempt = request(r)
    claimed = claim(r, attempt)
    failed = r.service.fail(
        r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), "qualification_indeterminate"
    )["attempt"]
    assert not failed["recoveryAvailable"] and failed["completedCases"] == 0
    with pytest.raises(DomainError):
        claim(r, failed, key="recover-no-cases", recovery=True)
    assert r.starts == [] and r.calls == []
    assert failed["reportId"] is None


def test_actual_http_lifecycle_exports_only_retained_public_qualification_contracts(qualification):
    from potbot.api.ui_mcp_qualification import register_mcp_qualification_routes

    r = qualification
    app, origin = FastAPI(), "http://127.0.0.1:3100"
    reads = {"Authorization": "Bearer qualification-fixture"}
    writes = {**reads, "Origin": origin, "X-Potbot-Setup-Token": r.token[0]}

    def authenticate(value):
        if value != reads["Authorization"]:
            raise ScopeDeniedError("Unknown fixture identity")
        return r.actor

    def authorize(value):
        if value != r.token[0]:
            raise ScopeDeniedError("Missing fixture setup authority")

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

    register_mcp_qualification_routes(
        app, r.env.db, authenticate, (origin,), r.actor.workspace_id, authorize, service=r.service
    )
    fixture = {"requests": {}}
    prefix = "/v1/ui/mcp-qualification"
    with TestClient(app) as client:

        def get(suffix):
            response = client.get(prefix + suffix, headers=reads)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            return response.json()

        def post(suffix, body, key):
            response = client.post(
                prefix + suffix, headers={**writes, "Idempotency-Key": key}, json=body
            )
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            return response.json()

        def change(collection, row, action, key, *, save=False):
            body = {"revision": row["revision"], "digest": row["bindingSha256"], "confirm": True}
            if save:
                fixture["requests"][action] = body
            suffix = f"/{collection}/{row['id']}/{action}"
            result = post(suffix, body, key)
            assert post(suffix, body, key) == result
            return result

        identity = r.admitted["id"]
        fixture["suites"] = get("/suites")
        fixture["suite"] = get("/suites/" + identity)
        fixture["emptyAttempts"] = get("/attempts?suiteId=" + identity)
        fixture["requestedAttempt"] = change(
            "suites", r.admitted, "qualify", "http-qualify", save=True
        )
        attempt = fixture["requestedAttempt"]["attempt"]
        claimed = claim(r, attempt, key="http-native-claim")
        fixture["runningAttempt"] = get("/attempts/" + attempt["id"])
        completed = complete(r, attempt=attempt, claimed=claimed)
        fixture["succeededAttempt"] = get("/attempts/" + completed["id"])
        fixture["report"] = get("/reports/" + completed["reportId"])
        fixture["freshSuite"] = get("/suites/" + identity)
        r.returned_matches = 2
        attempt = change("suites", current(r), "qualify", "http-failure-request")["attempt"]
        claimed = claim(r, attempt, key="http-failure-claim")
        failed_attempt = complete(r, attempt=attempt, claimed=claimed)
        fixture["failedAttempt"] = get("/attempts/" + failed_attempt["id"])
        fixture["failedReport"] = get("/reports/" + failed_attempt["reportId"])
        fixture["attempts"] = get("/attempts?suiteId=" + identity)
        fixture["firstPage"] = get("/attempts?suiteId=" + identity + "&limit=1")
        fixture["secondPage"] = get(
            "/attempts?suiteId=" + identity + "&limit=1&after=" + fixture["firstPage"]["nextCursor"]
        )
        pending = change("suites", current(r), "qualify", "http-cancel-request")["attempt"]
        fixture["cancelledAttempt"] = change(
            "attempts", pending, "cancel", "http-cancel", save=True
        )
        fixture["disabledSuite"] = change(
            "suites", current(r), "disable", "http-disable", save=True
        )
        fixture["enabledSuite"] = change(
            "suites", fixture["disabledSuite"]["suite"], "enable", "http-enable", save=True
        )
        r.clock[0] += timedelta(seconds=61)
        fixture["staleSuite"] = get("/suites/" + identity)
        fixture["staleReport"] = get("/reports/" + completed["reportId"])
        fixture["revokedSuite"] = change("suites", current(r), "revoke", "http-revoke", save=True)
        forge_passed_projection(r, failed_attempt["id"])
        fixture["untrustedAttempt"] = get("/attempts/" + failed_attempt["id"])
        assert_untrusted_attempt(fixture["untrustedAttempt"]["attempt"])
        assert len(r.calls) == len(r.starts) == 4
        assert client.get(prefix + "/suites").status_code != 200
        assert client.get(prefix + "/suites?limit=51", headers=reads).status_code != 200
        assert (
            client.post(
                prefix + f"/suites/{identity}/qualify",
                headers={**reads, "Idempotency-Key": "missing-origin-and-setup"},
                json=fixture["requests"]["qualify"],
            ).status_code
            != 200
        )
        assert (
            client.post(
                prefix + f"/suites/{identity}/qualify",
                headers={**writes, "Idempotency-Key": "forged-native-payload"},
                json={**fixture["requests"]["qualify"], "nativeReport": {"passed": True}},
            ).status_code
            != 200
        )
        assert len(r.calls) == 4

    encoded = json.dumps(fixture, sort_keys=True, indent=2)
    for forbidden in (
        r.token[0],
        QUERY_CANARY,
        RESULT_CANARY,
        "workerToken",
        str(r.suite.path),
        str(r.profile.private_paths[0]),
        str(r.native.receipts.root),
        str(r.discovery.runtime.rootfs),
    ):
        assert forbidden not in encoded
    if destination := os.environ.get("POTBOT_MCP_QUALIFICATION_CONTRACT_OUTPUT"):
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")


def test_populated_v43_upgrade_preserves_signed_discovery_and_migration_checksums(
    app_env, tmp_path
):
    from potbot.controller.mcp_discovery import McpDiscoveryService
    from tests.unit.test_mcp_discovery_gateway import installed_fixture

    schema, migrations = "mcp_qualification_upgrade", application_migrations()
    assert migrate(app_env.admin_dsn, schema, migrations[:43]) == tuple(range(1, 44))
    workspace = provision_workspace(
        app_env.admin_dsn, schema, "Qualification upgrade", sandbox_root=tmp_path / "legacy"
    )
    role = urlsplit(app_env.db.dsn).username
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    database = Database(app_env.db.dsn, schema)
    retained_service = McpDiscoveryService(database, setup_token=lambda: discovery.TOKEN)
    binding = retained_service.admit(
        actor,
        installed_fixture(actor),
        agent_id=uuid4(),
        configuration_sha256="b" * 64,
        name="Retained installed discovery fixture",
        ttl_seconds=300,
        watch_ms=0,
        key="legacy-binding",
        setup_token=discovery.TOKEN,
    )["binding"]
    retained_service.request(
        actor,
        UUID(binding["id"]),
        binding["revision"],
        binding["bindingSha256"],
        "legacy-request",
        setup_token=discovery.TOKEN,
    )
    with database.transaction(actor) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in (
                "mcp_discovery_bindings",
                "mcp_discovery_attempts",
                "mcp_discovery_events",
            )
        }
    with admin_connection(app_env.admin_dsn, schema) as connection:
        checksums = connection.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert migrate(app_env.admin_dsn, schema, migrations[:44]) == tuple(range(1, 45))
    grant_application_role(app_env.admin_dsn, schema, role)
    assert migrate(app_env.admin_dsn, schema, migrations[:44]) == tuple(range(1, 45))
    with database.transaction(actor) as connection:
        for table, previous in before.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == previous
        for table in (
            "mcp_qualification_suites",
            "mcp_qualification_attempts",
            "mcp_qualification_reports",
            "mcp_qualification_events",
        ):
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == []
    with admin_connection(app_env.admin_dsn, schema) as connection:
        assert (
            connection.execute(
                "SELECT * FROM schema_migrations WHERE version<=43 ORDER BY version"
            ).fetchall()
            == checksums
        )
    retained = retained_service.binding(actor, UUID(binding["id"]))["binding"]
    assert retained["id"] == binding["id"] and retained["approvalValid"]
