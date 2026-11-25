"""Live configuration and owned worker processes against disposable PostgreSQL/RLS."""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_resources import register_ui_routes
from potbot.api.ui_settings import register_settings_routes
from potbot.controller.budgets import BudgetLedger
from potbot.controller.jobs import JobService
from potbot.controller.live_configuration import LiveConfiguration
from potbot.controller.node_controls import NodeControls
from potbot.controller.tasks import TaskService
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.live_controls import NODE_OPERATION, worker_identity
from potbot.domain.serialization import content_hash
from potbot.models.hosted_policy import HostedPolicy
from potbot.persistence.database import admin_connection
from potbot.tools.node_supervisor import NativeNodeSupervisor, WorkerProcess, worker_environment
from tests.integration.test_budgets import running_task
from tests.integration.test_tasks import specification
from tests.integration.test_ui_settings import seed

pytestmark = pytest.mark.postgres
TOKEN = "live-api-fixture-" * 3
SETUP = "live-setup-fixture-" * 3
ORIGIN = "http://127.0.0.1:3100"
HEADERS = {"Authorization": "Bearer " + TOKEN, "Origin": ORIGIN, "X-Potbot-Setup-Token": SETUP}


def client_for(environment):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def errors(_request, error):
        status = (
            409
            if isinstance(error, RevisionConflictError)
            else 400
            if isinstance(error, InvalidInputError)
            else 403
        )
        return JSONResponse({"error": error.code.value}, status_code=status)

    def authenticate(value):
        if value != "Bearer " + TOKEN:
            raise ScopeDeniedError("Unavailable")
        return environment.context

    def setup(value):
        if value != SETUP:
            raise ScopeDeniedError("Unavailable")

    register_ui_routes(app, environment.db, authenticate, (ORIGIN,))
    register_settings_routes(
        app, environment.db, authenticate, (ORIGIN,), environment.context.workspace_id, setup
    )
    return TestClient(app)


def config_review(snapshot, values):
    return {
        "requestId": str(uuid4()),
        "expectedRevision": snapshot["revision"],
        "expectedSha256": snapshot["sha256"],
        "values": values,
        "confirm": True,
    }


def node_review(snapshot, action="restart"):
    node = snapshot["nodes"][0]
    return {
        "requestId": str(uuid4()),
        "nodeId": node["id"],
        "expectedGeneration": node["generation"],
        "expectedRevision": node["revision"],
        "expectedIncarnation": node["incarnation"],
        "action": action,
        "confirm": True,
    }


class Processes:
    """Only owns children launched for this test, including failed replacements."""

    def __init__(self):
        self.children = []
        self.fail_replacement = False

    def launch(self, worker_id):
        code = (
            "raise SystemExit(1)"
            if self.fail_replacement and self.children
            else "import json,sys,time;print(json.dumps({'data':{'worker_id':sys.argv[1],'status':'listening'}}),flush=True);time.sleep(300)"
        )
        child = WorkerProcess(
            [sys.executable, "-c", code, worker_id], worker_id, env=dict(os.environ)
        )
        self.children.append(child)
        return child

    def close(self):
        for child in self.children:
            child.stop()
            assert not child.running()


@pytest.fixture
def managed(app_env):
    node, _grant = seed(app_env)
    children = Processes()
    supervisor = NativeNodeSupervisor(
        app_env.admin_dsn,
        app_env.schema,
        app_env.context,
        node.id,
        node.generation,
        "a" * 64,
        SETUP,
        children.launch,
    )
    try:
        supervisor.start()
        yield supervisor, children, NodeControls(app_env.db)
    finally:
        # Synthetic children have no external effects. This cleanup is not an
        # operator receipt and never reclassifies indeterminate database work.
        children.close()


def read_nodes(environment, service):
    return service.read(environment.context, setup_available=True)


def test_live_configuration_pins_tasks_and_has_one_compare_and_swap_winner(app_env):
    seed(app_env)
    service = LiveConfiguration(app_env.db)
    initial = service.read(app_env.context, setup_available=True)
    layers = {
        "workspace": {
            "policy_versions": {"retained": "v1"},
            "secret_refs": {"provider": "secret://fixture/private"},
        },
        "task": {"physical_inference_concurrency": 3},
    }
    tasks = TaskService(app_env.db, configuration_layers=layers)
    before = tasks.submit(app_env.context, app_env.repository_id, specification(), "before")
    first = config_review(
        initial,
        {
            "max_packet_tokens": 4096,
            "policy_versions": {"live": "v2"},
            "physical_inference_concurrency": 2,
        },
    )
    receipt = service.save(app_env.context, first)
    assert service.save(app_env.context, first) == receipt
    after = tasks.submit(app_env.context, app_env.repository_id, specification(), "after")
    settings = after["configuration"]["content"]
    assert settings["max_packet_tokens"] == 4096
    assert settings["physical_inference_concurrency"] == 3
    assert (
        settings["policy_versions"] | {"retained": "v1", "live": "v2"}
        == settings["policy_versions"]
    )
    assert settings["secret_refs"] == layers["workspace"]["secret_refs"]
    assert tasks.submit(app_env.context, app_env.repository_id, specification(), "before") == before
    assert (
        tasks.inspect(app_env.context, UUID(before["task_id"]))["configuration"]
        == before["configuration"]
    )
    latest = service.read(app_env.context, setup_available=True)
    assert "secret://" not in json.dumps(latest)

    def attempt(value):
        try:
            return service.save(
                app_env.context, config_review(latest, {"max_packet_tokens": value})
            )["revision"]
        except RevisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (2048, 8192)))
    assert sorted(str(value) for value in results) == ["2", "conflict"]
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "UPDATE workspace_configuration_revisions SET overrides='{}' WHERE revision=1"
            )
    from psycopg.errors import ObjectNotInPrerequisiteState

    with pytest.raises(ObjectNotInPrerequisiteState):
        with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
            connection.execute(
                "UPDATE workspace_configuration_revisions SET overrides='{}' WHERE revision=1"
            )


def test_live_api_enforces_owner_setup_scope_and_unambiguous_bodies(app_env):
    seed(app_env)
    with client_for(app_env) as http:
        endpoint = "/v1/ui/settings/live"
        initial = http.get(endpoint, headers=HEADERS)
        assert (
            initial.status_code == 200 and initial.headers["cache-control"] == "private, no-store"
        )
        review = config_review(initial.json(), {"max_packet_tokens": 4096})
        for headers in (
            {},
            {**HEADERS, "X-Potbot-Setup-Token": "wrong"},
            {**HEADERS, "Origin": "https://untrusted.invalid"},
        ):
            assert http.put(endpoint, headers=headers, json=review).status_code == 403
        for body in (
            '{"values":{},"values":{}}',
            '{"values":NaN}',
            '{"padding":"' + "x" * 32768 + '"}',
        ):
            assert (
                http.put(
                    endpoint, headers={**HEADERS, "Content-Type": "application/json"}, content=body
                ).status_code
                == 400
            )
        assert (
            http.put(
                endpoint,
                headers=HEADERS,
                json={**review, "values": {"secret_refs": {"x": "secret://no"}}},
            ).status_code
            == 400
        )
        assert http.put(endpoint, headers=HEADERS, json=review).status_code == 200
        assert (
            http.put(endpoint, headers=HEADERS, json=config_review(initial.json(), {})).status_code
            == 409
        )
    with pytest.raises(ScopeDeniedError):
        LiveConfiguration(app_env.other_db).read(app_env.context, setup_available=True)
    # The second restricted login becomes a member, never the native owner.
    member = replace(app_env.other_context, workspace_id=app_env.context.workspace_id)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        connection.execute(
            "INSERT INTO workspace_memberships(workspace_id,principal_id) VALUES(%s,%s)",
            (member.workspace_id, member.principal_id),
        )
    service = LiveConfiguration(app_env.other_db)
    assert not service.read(member, setup_available=True)["canEdit"]
    with pytest.raises(ScopeDeniedError):
        service.save(member, config_review(initial.json(), {}))


def test_drain_fences_claims_then_waits_for_jobs_and_both_kinds_of_holds(app_env, managed):
    supervisor, children, nodes = managed
    episode_id = running_task(app_env)
    jobs = JobService(app_env.db)
    queued = jobs.submit(
        app_env.context,
        {
            "job_type": "model.generate",
            "episode_id": str(episode_id),
            "input": {"prompt": "fixture"},
            "selected_models": [{"provider": "openai", "model": "fixture", "role": "reasoning"}],
        },
        "work",
    )
    worker_id = worker_identity(supervisor.node_id, supervisor.incarnation)
    lease = jobs.claim(app_env.context, worker_id, lease_seconds=120)
    assert lease is not None
    budget = BudgetLedger(app_env.db)
    hold = budget.reserve(app_env.context, episode_id, {"calls": 1}, "hold")
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute(
            "INSERT INTO atomic_graph_resource_holds(workspace_id,principal_id,job_id,lock_key) VALUES(%s,%s,%s,'fixture-lock')",
            (app_env.context.workspace_id, app_env.context.principal_id, lease.job_id),
        )
    review = node_review(read_nodes(app_env, nodes), "drain")
    receipt = nodes.request(app_env.context, review, SETUP)
    assert receipt["state"] == "requested"
    assert jobs.claim(app_env.context, worker_id) is None
    supervisor.tick()
    observed = read_nodes(app_env, nodes)
    assert observed["nodes"][0]["phase"] == "draining" and observed["nodes"][0]["unsettled"] == 3
    assert observed["requests"][0]["state"] == "accepted"
    jobs.finish(app_env.context, lease, "failed", error_code="model_unavailable")
    supervisor.tick()
    waiting = read_nodes(app_env, nodes)["nodes"][0]
    assert waiting["phase"] == "draining" and waiting["unsettled"] == 1
    # The existing job terminal trigger owns atomic-hold release. The native
    # supervisor must still wait for the independently unsettled budget hold.
    with app_env.db.transaction(app_env.context) as connection:
        atomic = connection.execute(
            "SELECT active,released_at FROM atomic_graph_resource_holds WHERE job_id=%s",
            (lease.job_id,),
        ).fetchone()
    assert not atomic["active"] and atomic["released_at"] is not None
    budget.settle(app_env.context, UUID(hold["id"]), {"calls": 0}, "synthetic")
    supervisor.tick()
    drained = read_nodes(app_env, nodes)
    assert (
        drained["nodes"][0]["phase"] == "drained" and drained["requests"][0]["state"] == "applied"
    )
    assert children.children[0].running()  # Drain retains an idle worker.
    restart = node_review(drained)
    nodes.request(app_env.context, restart, SETUP)
    supervisor.tick()
    applied = read_nodes(app_env, nodes)
    request = next(row for row in applied["requests"] if row["requestId"] == restart["requestId"])
    assert (
        request["state"] == "applied"
        and request["observedIncarnation"] != restart["expectedIncarnation"]
    )
    assert not children.children[0].running() and children.children[1].running()
    assert nodes.request(app_env.context, restart, SETUP)["state"] == "requested"
    supervisor.tick()
    assert len(children.children) == 2
    assert jobs.inspect(app_env.context, UUID(queued["id"]))["state"] == "failed"


def test_protected_receipts_and_claim_trigger_cannot_be_forged(app_env, managed):
    supervisor, _children, nodes = managed
    review = node_review(read_nodes(app_env, nodes))
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute("UPDATE node_runtime_controls SET phase='ready',desired='active'")
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "INSERT INTO node_control_events(workspace_id,node_id,request_id,stage,reason,incarnation,revision) VALUES(%s,%s,%s,'applied','forged',%s,2)",
                (app_env.context.workspace_id, supervisor.node_id, uuid4(), supervisor.incarnation),
            )
    from psycopg.types.json import Jsonb

    forged = {
        "workspaceId": str(app_env.context.workspace_id),
        "principalId": str(app_env.context.principal_id),
        "review": review,
        "proof": "0" * 64,
    }
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute(
            "INSERT INTO command_results(workspace_id,id,principal_id,operation,idempotency_key,request_hash,result) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                app_env.context.workspace_id,
                uuid4(),
                app_env.context.principal_id,
                NODE_OPERATION,
                review["requestId"],
                content_hash(review),
                Jsonb(forged),
            ),
        )
    supervisor.tick()
    assert read_nodes(app_env, nodes)["requests"][0]["state"] == "rejected"
    assert supervisor.child.running()
    nodes.request(app_env.context, node_review(read_nodes(app_env, nodes), "drain"), SETUP)
    supervisor.tick()
    jobs = JobService(app_env.db)
    task = TaskService(app_env.db).submit(
        app_env.context, app_env.repository_id, specification(), "queued"
    )
    queued = jobs.submit(
        app_env.context,
        {
            "job_type": "model.generate",
            "episode_id": task["task_id"],
            "input": {"prompt": "fixture"},
            "selected_models": [{"provider": "openai", "model": "fixture", "role": "reasoning"}],
        },
        "queued",
    )
    with pytest.raises(RevisionConflictError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "UPDATE jobs SET state='running',generation=generation+1,lease_owner=%s,lease_until=clock_timestamp()+interval '2 minutes',started_at=clock_timestamp() WHERE id=%s",
                (worker_identity(supervisor.node_id, supervisor.incarnation), UUID(queued["id"])),
            )
    assert jobs.inspect(app_env.context, UUID(queued["id"]))["state"] == "queued"


def test_expired_lease_stays_indeterminate_and_prevents_restart(app_env, managed):
    supervisor, children, nodes = managed
    jobs = JobService(app_env.db)
    task = TaskService(app_env.db).submit(
        app_env.context, app_env.repository_id, specification(), "expiry"
    )
    jobs.submit(
        app_env.context,
        {
            "job_type": "model.generate",
            "episode_id": task["task_id"],
            "input": {"prompt": "fixture"},
            "selected_models": [{"provider": "openai", "model": "fixture", "role": "reasoning"}],
        },
        "expiry",
    )
    lease = jobs.claim(app_env.context, worker_identity(supervisor.node_id, supervisor.incarnation))
    assert lease is not None
    jobs.mark_dispatched(app_env.context, lease)
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (lease.job_id,),
        )
    jobs.expire(app_env.context)
    nodes.request(app_env.context, node_review(read_nodes(app_env, nodes)), SETUP)
    supervisor.tick()
    supervisor.tick()
    observed = read_nodes(app_env, nodes)
    assert observed["nodes"][0]["phase"] == "draining"
    assert observed["requests"][0]["state"] == "accepted" and len(children.children) == 1
    assert not supervisor.close()


def test_failed_replacement_never_receives_an_applied_receipt_or_replays(app_env, managed):
    supervisor, children, nodes = managed
    review = node_review(read_nodes(app_env, nodes))
    nodes.request(app_env.context, review, SETUP)
    children.fail_replacement = True
    with pytest.raises(StateMismatchError):
        supervisor.tick()
    supervisor.tick()
    observed = read_nodes(app_env, nodes)
    assert observed["nodes"][0]["phase"] == "indeterminate"
    assert observed["requests"][0]["state"] == "indeterminate"
    assert len(children.children) == 2 and all(not child.running() for child in children.children)
    replacement = NativeNodeSupervisor(
        app_env.admin_dsn,
        app_env.schema,
        app_env.context,
        supervisor.node_id,
        supervisor.generation,
        "a" * 64,
        SETUP,
        children.launch,
    )
    with pytest.raises(StateMismatchError, match="reconciliation"):
        replacement.start()


def test_actual_restricted_jobs_worker_starts_and_restarts_with_empty_queue(app_env, tmp_path):
    node, _grant = seed(app_env)
    policy = tmp_path / "empty-hosted-policy.json"
    policy.write_text(json.dumps(HostedPolicy().to_dict()), encoding="utf-8")
    children = Processes()
    environment = worker_environment(dict(os.environ), app_env.db.dsn, app_env.admin_dsn, SETUP)
    environment["POTBOT_CREDENTIAL_PROFILE"] = "live-acceptance-" + uuid4().hex
    for key in tuple(environment):
        if key.startswith("POTBOT_NATIVE_") or key.startswith("POTBOT_LOG_"):
            environment.pop(key)

    def launch(worker_id):
        child = WorkerProcess(
            [
                sys.executable,
                "-m",
                "potbot.api.jobs_cli",
                "worker",
                "--workspace-id",
                str(app_env.context.workspace_id),
                "--principal-id",
                str(app_env.context.principal_id),
                "--schema",
                app_env.schema,
                "--provider-policy",
                str(policy),
                "--worker-id",
                worker_id,
            ],
            worker_id,
            env=environment,
        )
        children.children.append(child)
        return child

    supervisor = NativeNodeSupervisor(
        app_env.admin_dsn,
        app_env.schema,
        app_env.context,
        node.id,
        node.generation,
        "a" * 64,
        SETUP,
        launch,
    )
    try:
        supervisor.start()
        nodes = NodeControls(app_env.db)
        review = node_review(read_nodes(app_env, nodes))
        nodes.request(app_env.context, review, SETUP)
        supervisor.tick()
        assert read_nodes(app_env, nodes)["requests"][0]["state"] == "applied"
        assert len(children.children) == 2
        assert not children.children[0].running() and children.children[1].running()
        supervisor.begin_shutdown()
        assert supervisor.close()
        assert read_nodes(app_env, nodes)["nodes"][0]["phase"] == "stopped"
    finally:
        children.close()
