"""Actual PostgreSQL, HTTP, Git, leases and filesystem promotion in one flow.

Builder/test process outcomes are explicit fixtures. These tests do not claim
an installed Bubblewrap/Syn host or a live GitHub transport qualification.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_tool_acquisitions import register_tool_acquisition_routes
from potbot.api.ui_tool_upgrades import UiToolUpgradeStore
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.errors import DomainError, ScopeDeniedError, StateMismatchError
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_tool_release_markers import blocked_directories, read_hold
from potbot.tools.shared_tool_store import SharedToolStore
from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.tool_acquisition import ToolAcquisitionService, ToolRecipe
from potbot.tools.tool_acquisition_worker import ToolAcquisitionWorker, retain_recipe
from potbot.tools.tool_approval_worker_s11 import ToolApprovalWorker
from potbot.tools.tool_release_gate_s12 import ToolReleaseAuthority
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from tests.unit.test_tool_sources import REPOSITORY, git
from tests.unit.test_tool_sources import source as source

pytestmark = pytest.mark.postgres
TOKEN = "fixture-independent-tool-capability-" + "s" * 32
BASE = "/v1/ui/tools/acquisitions"


class TestProcesses:
    __test__ = False

    def __init__(self):
        self.calls = []

    def run(self, *, case, argv, cancelled, **kwargs):
        assert not cancelled()
        self.calls.append((case.name, argv))
        return dict(
            name=case.name,
            kind=case.kind,
            passed=True,
            outcome="completed",
            exit_code=0,
            elapsed_ms=2,
            dispatched=True,
            stopped=True,
            isolation_identity="explicit-acquisition-test-process-fixture",
            output_sha256="e" * 64,
        )


class BuildProcess:
    def __init__(self):
        self.calls = []

    def run(self, request, cancelled):
        assert not cancelled()
        self.calls.append(request)
        output = request.cwd / "static" / "fixture-tool"
        output.write_bytes(b"#!/usr/bin/python3\nprint('fixture')\n")
        output.chmod(0o755)
        return SimpleNamespace(
            dispatched=True,
            processes_stopped=True,
            outcome="completed",
            exit_code=0,
            isolation_identity="f" * 64,
        )


@pytest.fixture
def pipeline(app_env, source, tmp_path, monkeypatch):
    sources, remote, commit = source
    actor, agent = app_env.context, uuid4()
    rootfs, home, tools, private, config, history = [
        tmp_path / name for name in ("rootfs", "home", "tools", "private", "config", "history")
    ]
    for directory in (rootfs, home, tools, private, config, history):
        directory.mkdir(mode=0o700)
    recipe_dir, evidence_dir, proposals = [private / name for name in ("recipes", "stages", "pins")]
    for directory in (recipe_dir, evidence_dir, proposals):
        directory.mkdir(mode=0o700)
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(
        actor.workspace_id,
        digest="a" * 64,
        image_ref="ghcr.io/fixture/image@sha256:" + "a" * 64,
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256="b" * 64,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="acquisition-fixture")
    homes.provision(actor.workspace_id, agent)

    def authorized(workspace, candidate):
        return (workspace, candidate) == (actor.workspace_id, agent)

    store = SharedToolStore(ledger=ledger, shared_root=tools, authorize=authorized)
    runtime = SharedWorkspaceRuntime(
        "a" * 64,
        rootfs,
        home,
        "b" * 64,
        shared_tools=tools,
        ai_config_dir=config,
        ai_history_dir=history,
    )
    executor, builder = TestProcesses(), BuildProcess()
    manager = SharedToolUpgradeManager(
        store=store, runtime=runtime, homes=homes, authorize=authorized, executor=executor
    )
    operator = SimpleNamespace(
        workspace_id=actor.workspace_id,
        runtime=runtime,
        monitor=SimpleNamespace(require_active=lambda: None),
        supervisor=builder,
    )
    operator.bind_tool_release_authority = lambda authority: None
    service = ToolAcquisitionService(app_env.db, setup_token_resolver=lambda: TOKEN)
    promotion = ToolApprovalWorker(
        approvals=UiToolUpgradeStore(app_env.db),
        manager=manager,
        context=actor,
        proposal_dir=proposals,
        agent_id=agent,
        setup_token=TOKEN,
    )
    worker = ToolAcquisitionWorker(
        service=service,
        context=actor,
        agent_id=agent,
        setup_token=TOKEN,
        sources=sources,
        recipe_dir=recipe_dir,
        evidence_dir=evidence_dir,
        manager=manager,
        homes=homes,
        operator=operator,
        promotion=promotion,
    )
    recipe = ToolRecipe.from_dict(
        {
            "schema": "potbot-tool-acquisition-recipe/v1",
            "slug": "fixture-tool",
            "name": "Fixture executable",
            "description": "Explicit offline process fixture",
            "repository": REPOSITORY,
            "build_argv": ["/usr/bin/python3", "-I", "-B", "{checkout}/run.py"],
            "timeout_ms": 30000,
            "cleanup_paths": ["build-cache"],
            "lockfile_paths": [],
            "artifact_paths": ["static/fixture-tool"],
            "tests": [
                {
                    "name": name,
                    "kind": name,
                    "argv": ["/usr/bin/python3", "--version"],
                    "timeout_ms": 1000,
                }
                for name in ("smoke", "unit")
            ],
        }
    )
    retain_recipe(recipe, recipe_dir)
    published = service.publish_recipe(actor, recipe, agent, "publish-fixture", setup_token=TOKEN)
    recipe_id = published["recipe"]["id"]
    postflight = TestProcesses()
    real_postflight = ToolReleaseAuthority.postflight

    def fixture_postflight(self, *, operation, suite, **authorization):
        return real_postflight(
            self, operation=operation, suite=suite, executor=postflight, **authorization
        )

    monkeypatch.setattr(ToolReleaseAuthority, "postflight", fixture_postflight)
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

    def authenticate(value):
        if value == "Bearer fixture":
            return actor
        raise ScopeDeniedError("Fixture authentication required")

    def authorize(value):
        if value != TOKEN:
            raise ScopeDeniedError("Fixture independent capability required")

    register_tool_acquisition_routes(
        app,
        app_env.db,
        authenticate,
        ("http://127.0.0.1:3100",),
        actor.workspace_id,
        authorize,
        service=service,
    )
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer fixture",
        "Origin": "http://127.0.0.1:3100",
        "X-Potbot-Setup-Token": TOKEN,
    }
    responses = {"recipes": client.get(BASE + "/recipes", headers=headers).json()}

    def request_source(revision=commit, key=None):
        response = client.post(
            BASE,
            headers={**headers, "Idempotency-Key": key or uuid4().hex},
            json={"recipeId": recipe_id, "commit": revision, "confirm": True},
        )
        assert response.status_code == 200, response.text
        responses["requested"] = response.json()
        return UUID(response.json()["operation"]["id"])

    def current(operation):
        response = client.get(BASE + "/" + str(operation), headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["operation"]

    def control(operation, action, key=None):
        before = current(operation)
        field = (
            "reviewSha256"
            if action == "approve-build"
            else ("evidenceSha256" if action in {"approve", "reject"} else "bindingSha256")
        )
        digest = before["review"]["sha256"] if action == "approve-build" else before[field]
        response = client.post(
            f"{BASE}/{operation}/{action}",
            headers={**headers, "Idempotency-Key": key or uuid4().hex},
            json={"revision": before["revision"], field: digest, "confirm": True},
        )
        assert response.status_code == 200, response.text
        responses[action] = response.json()
        return response.json()["operation"]

    def update_runtime(pins):
        manager.runtime = replace(manager.runtime, tool_entrypoints=tuple(pins.items()))
        operator.runtime = manager.runtime

    return SimpleNamespace(**locals())


def build_tested(r, *, commit=None):
    operation = r.request_source(commit or r.commit)
    assert r.current(operation)["phase"] == "source_queued"
    sourced = r.worker.run_once("source")
    assert sourced["operation"]["phase"] == "review", sourced
    reviewed = r.current(operation)
    r.responses["reviewed"] = r.service.inspect(r.actor, operation)
    assert reviewed["review"]["newCommit"] == (commit or r.commit)
    assert reviewed["review"]["network"] == "denied"
    assert r.control(operation, "approve-build")["phase"] == "build_queued"
    built = r.worker.run_once("build")
    assert built["operation"]["phase"] == "awaiting_approval", built
    r.responses["tested"] = built
    assert len(r.current(operation)["tests"]) == 2
    return operation


def activate_release(r, operation):
    assert r.control(operation, "approve")["phase"] == "approved"
    assert r.worker.run_once("promote")["state"] == "awaiting_repin"
    assert "fixture-tool" in blocked_directories(r.manager.runtime)
    hold = read_hold(r.tools, "fixture-tool")
    r.update_runtime(hold["entrypoints"])
    r.control(operation, "repin")
    repinned = r.worker.run_once("action")
    assert repinned["operation"]["phase"] == "repinned", repinned
    r.responses["repinned"] = repinned
    r.control(operation, "postflight")
    released = r.worker.run_once("action")
    assert released["operation"]["phase"] == "active", released
    r.responses["released"] = released
    authority = r.promotion.release_authority()
    r.manager.runtime = replace(r.manager.runtime, tool_release_authority=authority)
    r.operator.runtime = r.manager.runtime
    assert "fixture-tool" not in blocked_directories(r.manager.runtime)
    return released


def test_complete_http_source_build_first_install_release_and_withdrawal(pipeline):
    r = pipeline
    operation = build_tested(r)
    assert r.current(operation)["oldCommit"] is None
    assert r.current(operation)["kind"] == "install"
    activate_release(r, operation)
    r.control(operation, "rollback")
    rollback = r.worker.run_once("action")
    assert rollback["operation"]["phase"] == "rolled_back", rollback
    assert not (r.tools / "fixture-tool").exists()
    assert "fixture-tool" in blocked_directories(r.manager.runtime)
    r.update_runtime({})
    r.control(operation, "repin")
    withdrawn = r.worker.run_once("action")
    assert withdrawn["operation"]["phase"] == "withdrawn", withdrawn
    assert withdrawn["operation"]["installedState"] == "absent"
    assert withdrawn["operation"]["allowedActions"] == []
    assert len(r.builder.calls) == 1 and len(r.executor.calls) == 2 and len(r.postflight.calls) == 2
    r.responses["withdrawn"] = withdrawn
    r.responses["detail"] = r.client.get(f"{BASE}/{operation}", headers=r.headers).json()
    r.responses["list"] = r.client.get(BASE, headers=r.headers).json()
    r.responses["events"] = r.client.get(f"{BASE}/{operation}/events", headers=r.headers).json()
    assert "withdrawn" in {event["action"] for event in r.responses["events"]["items"]}
    assert TOKEN not in json.dumps(r.responses) and str(r.private) not in json.dumps(r.responses)
    destination = os.environ.get("POTBOT_TOOL_ACQUISITION_CONTRACT_OUTPUT")
    if destination:
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "workspaceId": str(r.actor.workspace_id),
                    "principalId": str(r.actor.principal_id),
                    "operationId": str(operation),
                    "responses": r.responses,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )


def test_upgrade_and_rollback_reuse_original_installation_and_release_owners(pipeline):
    r = pipeline
    first = build_tested(r)
    activate_release(r, first)
    (r.remote / "README.md").write_text("A new exact source revision\n")
    git(r.remote, "add", ".")
    git(r.remote, "commit", "--quiet", "-m", "next fixture revision")
    updated = git(r.remote, "rev-parse", "HEAD")
    second = build_tested(r, commit=updated)
    reviewed = r.current(second)
    assert reviewed["kind"] == "upgrade" and reviewed["oldCommit"] == r.commit
    activate_release(r, second)
    assert r.store.definition(r.actor.workspace_id, "fixture-tool").commit == updated
    r.control(second, "rollback")
    assert r.worker.run_once("action")["operation"]["phase"] == "rolled_back"
    assert r.store.definition(r.actor.workspace_id, "fixture-tool").commit == r.commit
    hold = read_hold(r.tools, "fixture-tool")
    r.update_runtime(hold["entrypoints"])
    r.control(second, "repin")
    assert r.worker.run_once("action")["operation"]["phase"] == "repinned"
    r.control(second, "postflight")
    restored = r.worker.run_once("action")["operation"]
    assert restored["phase"] == "rolled_back" and restored["releaseState"] == "released"
    assert len(r.builder.calls) == 2 and len(r.postflight.calls) == 6


def test_changed_runtime_after_review_never_dispatches_builder(pipeline):
    r = pipeline
    operation = r.request_source()
    r.worker.run_once("source")
    r.control(operation, "approve-build")
    r.manager.runtime = replace(r.manager.runtime, qualification_sha256="c" * 64)
    r.operator.runtime = r.manager.runtime
    failed = r.worker.run_once("build")
    assert failed["operation"]["phase"] == "indeterminate"
    assert not r.builder.calls
    assert not (r.tools / "fixture-tool").exists()


def test_retained_source_tamper_never_dispatches_builder(pipeline):
    r = pipeline
    operation = r.request_source()
    r.worker.run_once("source")
    r.control(operation, "approve-build")
    (r.sources.root / operation.hex / "tree/checkout/run.py").write_text("unapproved source\n")
    failed = r.worker.run_once("build")
    assert failed["operation"]["phase"] == "indeterminate"
    assert not r.builder.calls


def test_recipe_private_copy_is_independently_required_by_host(pipeline):
    r = pipeline
    operation = r.request_source()
    (r.recipe_dir / (r.recipe.digest + ".json")).unlink()
    failed = r.worker.run_once("source")
    assert failed["operation"]["phase"] == "failed"
    assert not r.builder.calls and not (r.sources.root / operation.hex).exists()


def test_current_key_rotation_blocks_pending_source_claim(pipeline):
    r = pipeline
    operation = r.request_source()
    rotated = ToolAcquisitionService(r.app_env.db, setup_token_resolver=lambda: "r" * 64)
    with pytest.raises(ScopeDeniedError):
        rotated.claim(r.actor, r.agent, "source", setup_token="r" * 64)
    assert r.current(operation)["phase"] == "source_queued"


def test_completed_request_replay_preserves_identity_and_never_refetches(pipeline):
    r = pipeline
    operation = r.request_source(key="lost-response")
    original = r.responses["requested"]
    r.worker.run_once("source")
    response = r.client.post(
        BASE,
        headers={**r.headers, "Idempotency-Key": "lost-response"},
        json={"recipeId": r.recipe_id, "commit": r.commit, "confirm": True},
    )
    assert response.status_code == 200 and response.json() == original
    assert r.current(operation)["phase"] == "review"
    assert r.worker.run_once("source") == {"state": "idle"}


def test_cross_principal_cannot_observe_or_claim_acquisition(pipeline):
    r = pipeline
    operation = r.request_source()
    other = r.app_env.other_context
    service = ToolAcquisitionService(r.app_env.other_db, setup_token_resolver=lambda: TOKEN)
    with pytest.raises(ScopeDeniedError):
        service.inspect(other, operation)
    assert service.list(other)["items"] == []
    assert service.claim(other, r.agent, "source", setup_token=TOKEN) is None


def test_declared_artifact_missing_holds_completed_build_without_activation(pipeline, monkeypatch):
    r = pipeline
    operation = r.request_source()
    r.worker.run_once("source")
    r.control(operation, "approve-build")
    real = r.builder.run

    def missing(request, cancelled):
        result = real(request, cancelled)
        (request.cwd / "static/fixture-tool").unlink()
        return result

    monkeypatch.setattr(r.builder, "run", missing)
    failed = r.worker.run_once("build")
    assert failed["operation"]["phase"] == "indeterminate"
    assert not (r.tools / "fixture-tool").exists()
    with pytest.raises(StateMismatchError):
        r.store.definition(r.actor.workspace_id, "fixture-tool")


def test_rejected_candidate_is_abandoned_before_a_fresh_acquisition(pipeline):
    r = pipeline
    operation = build_tested(r)
    assert r.control(operation, "reject")["phase"] == "rejected"
    r.control(operation, "reconcile")
    abandoned = r.worker.run_once("action", operation_id=operation)
    assert abandoned["operation"]["phase"] == "cancelled", abandoned
    assert (
        r.manager.inspect(
            workspace_id=r.actor.workspace_id, agent_id=r.agent, operation_id=operation
        )["state"]
        == "aborted"
    )
    assert not (r.tools / "fixture-tool").exists()
    assert r.request_source() != operation


def test_delayed_build_claim_cannot_execute_after_signed_recovery(pipeline):
    r = pipeline
    operation = r.request_source()
    r.worker.run_once("source", operation_id=operation)
    r.control(operation, "approve-build")
    stale = r.service.claim(r.actor, r.agent, "build", setup_token=TOKEN, operation_id=operation)
    r.control(operation, "reconcile")
    assert r.worker.run_once("action", operation_id=operation)["operation"]["phase"] == "cancelled"
    with pytest.raises(StateMismatchError):
        r.worker._build(stale, r.recipe)
    assert not r.builder.calls
    assert r.ledger.active(r.actor.workspace_id) == []
    assert not (r.tools / "fixture-tool").exists()


def test_recovery_fences_between_confirmed_build_stop_and_staging(pipeline, monkeypatch):
    r = pipeline
    operation = r.request_source()
    r.worker.run_once("source", operation_id=operation)
    r.control(operation, "approve-build")
    real = r.builder.run

    def stop_and_recover(request, cancelled):
        result = real(request, cancelled)
        r.control(operation, "reconcile")
        return result

    monkeypatch.setattr(r.builder, "run", stop_and_recover)
    with pytest.raises(StateMismatchError):
        r.worker.run_once("build", operation_id=operation)
    assert r.ledger.active(r.actor.workspace_id) == []
    recovered = r.worker.run_once("action", operation_id=operation)
    assert recovered["operation"]["phase"] == "cancelled", recovered
    assert len(r.builder.calls) == 1 and not r.executor.calls
    assert not (r.tools / "fixture-tool").exists()


def test_running_postflight_claim_can_be_fenced_for_rollback_without_replay(pipeline):
    r = pipeline
    operation = build_tested(r)
    r.control(operation, "approve")
    r.worker.run_once("promote", operation_id=operation)
    r.update_runtime(read_hold(r.tools, "fixture-tool")["entrypoints"])
    r.control(operation, "repin")
    r.worker.run_once("action", operation_id=operation)
    r.control(operation, "postflight")
    stale = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN, operation_id=operation)
    assert r.current(operation)["hostAction"]["status"] == "running"
    r.control(operation, "rollback")
    result = r.worker.run_once("action", operation_id=operation)
    assert result["operation"]["phase"] == "rolled_back", result
    with pytest.raises(StateMismatchError):
        r.worker._action(stale, r.recipe)
    assert not r.postflight.calls and not (r.tools / "fixture-tool").exists()


def test_selected_host_operation_does_not_consume_another_queue_entry(pipeline):
    r = pipeline
    operation = r.request_source()
    assert r.worker.run_once("source", operation_id=uuid4()) == {"state": "idle"}
    assert r.current(operation)["phase"] == "source_queued"
    assert r.worker.run_once("source", operation_id=operation)["operation"]["phase"] == "review"


@pytest.mark.parametrize("completed", ["repin", "postflight"])
def test_completed_release_action_with_lost_ack_is_read_back_without_replaying(pipeline, completed):
    r = pipeline
    operation = build_tested(r)
    r.control(operation, "approve")
    r.worker.run_once("promote", operation_id=operation)
    r.update_runtime(read_hold(r.tools, "fixture-tool")["entrypoints"])
    r.control(operation, "repin")
    if completed == "postflight":
        r.worker.run_once("action", operation_id=operation)
        r.control(operation, "postflight")
    claim = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN, operation_id=operation)
    authority = r.promotion.release_authority()
    callbacks = {
        "authorize_attempt": lambda: r.service.validate_claim(r.actor, claim),
        "authorize_commit": lambda conn: r.service.validate_claim(r.actor, claim, connection=conn),
    }
    if completed == "repin":
        authority.record_repin(operation=operation, **callbacks)
    else:
        authority.postflight(operation=operation, suite=r.recipe.tests, **callbacks)
    committed = r.current(operation)
    assert committed["hostAction"]["status"] == "running"
    observed_calls = len(r.postflight.calls)
    r.control(operation, "reconcile")
    result = r.worker.run_once("action", operation_id=operation)["operation"]
    assert result["releaseState"] == committed["releaseState"]
    assert result["releaseGeneration"] == committed["releaseGeneration"]
    assert result["releaseEvidenceSha256"] == committed["releaseEvidenceSha256"]
    assert result["hostAction"] is None and len(r.postflight.calls) == observed_calls


def test_failed_postflight_survives_recovery_of_unstarted_rollback(pipeline, monkeypatch):
    r = pipeline
    operation = build_tested(r)
    r.control(operation, "approve")
    r.worker.run_once("promote", operation_id=operation)
    r.update_runtime(read_hold(r.tools, "fixture-tool")["entrypoints"])
    r.control(operation, "repin")
    r.worker.run_once("action", operation_id=operation)
    r.control(operation, "postflight")
    real = r.postflight.run

    def failing(**kwargs):
        result = real(**kwargs)
        return {**result, "passed": False, "outcome": "nonzero_exit", "exit_code": 1}

    monkeypatch.setattr(r.postflight, "run", failing)
    r.worker.run_once("action", operation_id=operation)
    failed = r.current(operation)
    assert failed["releaseState"] == "failed"
    r.control(operation, "rollback")
    r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN, operation_id=operation)
    r.control(operation, "reconcile")
    result = r.worker.run_once("action", operation_id=operation)["operation"]
    assert result["releaseState"] == "failed" and result["hostAction"] is None
    assert result["releaseGeneration"] == failed["releaseGeneration"]
    assert result["releaseEvidenceSha256"] == failed["releaseEvidenceSha256"]
    assert result["allowedActions"] == ["rollback"]
    assert len(r.postflight.calls) == 1
