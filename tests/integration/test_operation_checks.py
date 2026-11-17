"""Real queue/policy/budget SQL; explicit provider and native transport fixtures.

No paid call or installed native host qualification is claimed. Native file,
pin, grant, task and S12 checks run unchanged; process/transport are fixtures.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot.cogs.contracts import TypedCogValue
from potbot.context.syn_context_route import SynContextRoute
from potbot.controller.budgets import BudgetLedger
from potbot.controller.cog_capability_policy import (
    Capability,
    CogCapabilityGrant,
    CogCapabilityPolicy,
    installation_hash,
)
from potbot.controller.cog_grant_store import publish_cog_policy
from potbot.controller.job_runner import HostedJobRunner
from potbot.controller.jobs import JobService
from potbot.controller.native_cog_dispatch import NativeCogDispatcher
from potbot.controller.operation_checks import OperationChecks
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.controller.tasks import TaskService
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.jobs import CHECK_JOB
from potbot.domain.operation_workbench import sign_authorization
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynObject, SynRef, SynSnapshot
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.models.hosted_gateway import HostedInferenceGateway
from potbot.models.hosted_policy import HostedPolicy
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import provision_principal
from potbot.semantics.service import SynService
from potbot.syn_release.mesh_endpoint import NativeMeshClient
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.shared_tool_release_markers import write_hold
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessSupervisor
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from tests.integration.test_hosted_gateway import KEY, rig
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres
TOKEN = "operation-check-test-setup-capability-123456789"


def enqueue_check(value, *, key="check", request=None):
    actor = value.actor
    spec = request or value.spec
    identity = content_hash(
        {
            "workspace": str(actor.workspace_id),
            "kind": "model" if spec["job_type"] == "model.generate" else "cog",
            "reference": value.reference,
        }
    )
    data = {
        "operation_id": identity,
        "request": spec,
        "authorization": sign_authorization(actor, identity, spec, TOKEN, purpose="check"),
    }
    job = value.jobs.submit(
        actor,
        {
            "job_type": CHECK_JOB,
            "episode_id": spec["episode_id"],
            "input": data,
            "selected_models": [],
        },
        key,
        trusted_operation=True,
    )
    return job, data


def launch(value, check, data, *, key="launch"):
    authorization = sign_authorization(
        value.actor,
        data["operation_id"],
        data["request"],
        TOKEN,
        purpose="launch",
        check_id=UUID(check["id"]),
    )
    return value.jobs.submit(
        value.actor,
        data["request"],
        key,
        trusted_operation=True,
        operation_check_id=UUID(check["id"]),
        operation_authorization=authorization,
    )


def hosted(app_env):
    value = rig(app_env, claim=False)
    value.actor = app_env.context
    value.jobs.cancel(value.actor, UUID(value.job["id"]), "discard-initial-fixture")
    value.spec = {
        "job_type": "model.generate",
        "episode_id": str(value.task_id),
        "input": {"prompt": "private task prompt", "max_output_tokens": 16, "timeout_seconds": 30},
        "selected_models": [{"provider": "openai", "model": "fixture-model", "role": "reasoning"}],
    }
    value.reference = "model:fixture-model"
    value.runner = HostedJobRunner(value.jobs, value.gateway, operation_setup_token=lambda: TOKEN)
    return value


def effects(value):
    with value.jobs.database.transaction(value.actor) as connection:
        return connection.execute(
            "SELECT (SELECT count(*) FROM hosted_requests) AS requests,(SELECT count(*) FROM hosted_invocations) AS invocations,(SELECT count(*) FROM budget_reservations) AS reservations,(SELECT count(*) FROM budget_usage) AS usage,(SELECT count(*) FROM action_runs) AS actions"
        ).fetchone()


def test_hosted_check_has_zero_effects_then_linked_launch_uses_existing_worker(app_env):
    value = hosted(app_env)
    try:
        queued, data = enqueue_check(value)
        before = effects(value)
        checked = value.runner.run_once(value.actor, "checks")
        assert checked["id"] == queued["id"] and checked["state"] == "succeeded"
        report = checked["result"]["operation_check"]
        assert (
            report["eligible"] is True
            and report["bindings"]["policySha256"] == value.policy.fingerprint
        )
        assert effects(value) == before and value.requests == []
        assert KEY not in json.dumps(report) and "private task prompt" not in json.dumps(report)
        launched = launch(value, checked, data)
        result = value.runner.run_once(value.actor, "execution")
        assert result["id"] == launched["id"] and result["state"] == "succeeded"
        assert len(value.requests) == 1 and result["result"]["completed_models"] == 1
        events = value.jobs.events(value.actor, UUID(result["id"]))["items"]
        assert any(row["event_type"] == "job.output" for row in events)
        assert value.jobs.inspect(value.actor, UUID(checked["id"]))["result"] == checked["result"]
        assert value.runner.run_once(value.actor, "execution") is None
        assert effects(value)["usage"] == 1
    finally:
        value.http.close()


def test_hosted_invocations_remain_private_to_the_job_principal(app_env):
    value = hosted(app_env)
    try:
        _, data = enqueue_check(value)
        checked = value.runner.run_once(value.actor, "checks")
        launch(value, checked, data)
        result = value.runner.run_once(value.actor, "execution")
        invocation_id = UUID(result["result"]["invocation_ids"][0])
        assert value.gateway.inspect(value.actor, invocation_id)["status"] == "completed"
        with app_env.other_db.transaction(app_env.other_context) as connection:
            role = connection.execute("SELECT session_user AS role").fetchone()["role"]
        peer = provision_principal(
            app_env.admin_dsn, app_env.schema, role, value.actor.workspace_id
        )
        other = HostedInferenceGateway(
            app_env.other_db,
            value.policy,
            lambda _: pytest.fail("Reading invocation evidence cannot read credentials"),
            http_client=value.http,
        )
        with pytest.raises(ScopeDeniedError):
            other.inspect(peer, invocation_id)
        with pytest.raises(ScopeDeniedError):
            other.list_for_job(peer, UUID(result["id"]))
    finally:
        value.http.close()


@pytest.mark.parametrize("change", ["policy", "credential", "budget", "setup", "task", "expired"])
def test_hosted_linked_launch_revalidates_before_any_provider_or_reservation(app_env, change):
    value = hosted(app_env)
    try:
        _, data = enqueue_check(value)
        checked = value.runner.run_once(value.actor, "checks")
        assert checked["result"]["operation_check"]["eligible"] is True
        queued = launch(value, checked, data)
        before = effects(value)
        if change == "policy":
            value.gateway.policy = replace(value.gateway.policy, max_output_tokens=4096)
        elif change == "credential":
            value.gateway.secret_resolver = lambda _: (_ for _ in ()).throw(
                ScopeDeniedError("missing private credential")
            )
        elif change == "budget":
            BudgetLedger(value.env.db).reserve(
                value.actor, value.task_id, {"calls": 8}, "occupy-calls"
            )
            before = effects(value)
        elif change == "setup":
            value.runner.operation_setup_token = lambda: TOKEN + "rotated"
        elif change == "task":
            task = value.tasks.inspect(value.actor, value.task_id)
            value.tasks.transition(
                value.actor,
                value.task_id,
                task["update_token"],
                TaskStatus.PAUSED,
                "pause",
                idempotency_key="pause-check",
            )
        else:
            lease = value.jobs.claim(value.actor, "execution")
            checker = OperationChecks(
                value.jobs,
                value.gateway,
                None,
                setup_token=lambda: TOKEN,
                clock=lambda: datetime.now(UTC) + timedelta(minutes=3),
            )
            with pytest.raises(StateMismatchError, match="stale"):
                checker.revalidate(value.actor, lease)
            assert effects(value) == before and value.requests == []
            return
        finished = value.runner.run_once(value.actor, "execution")
        assert finished["id"] == queued["id"] and finished["state"] in {"failed", "cancelled"}
        assert finished["dispatched_at"] is None
        assert value.requests == [] and effects(value) == before
    finally:
        value.http.close()


def test_failed_credentials_check_is_safe_blocked_and_cancelled_check_never_runs(app_env):
    value = hosted(app_env)
    try:
        value.gateway.secret_resolver = lambda _: (_ for _ in ()).throw(
            ScopeDeniedError("secret-canary")
        )
        enqueue_check(value)
        result = value.runner.run_once(value.actor, "checks")
        report = result["result"]["operation_check"]
        assert report["eligible"] is False and report["checks"][-1] == {
            "code": "provider_credentials",
            "status": "blocked",
        }
        assert "secret-canary" not in json.dumps(result)
        pending, _ = enqueue_check(value, key="cancel-check")
        value.jobs.cancel(value.actor, UUID(pending["id"]), "cancel")
        assert value.runner.run_once(value.actor, "checks") is None
        assert value.requests == [] and effects(value)["reservations"] == 0
    finally:
        value.http.close()


def test_running_check_cancellation_prevents_report_publication_and_all_effects(app_env):
    value = hosted(app_env)
    try:
        queued, _ = enqueue_check(value)

        def credential(_):
            value.jobs.cancel(value.actor, UUID(queued["id"]), "cancel-running-check")
            return KEY

        value.gateway.secret_resolver = credential
        before = effects(value)
        finished = value.runner.run_once(value.actor, "checks")
        assert finished["state"] == "cancelled" and finished["result"] is None
        assert value.requests == [] and effects(value) == before
    finally:
        value.http.close()


def test_check_reports_uninstalled_native_handler_as_unsupported(app_env):
    value = hosted(app_env)
    try:
        value.spec = {
            "job_type": "cog.execute",
            "episode_id": str(value.task_id),
            "input": {
                "agent_id": str(uuid4()),
                "repository_state_id": str(uuid4()),
                "operation_ref": "syn://thing/uninstalled",
                "profile_sha256": "a" * 64,
                "type_ref": "syn://type/input",
                "value": {},
            },
            "selected_models": [],
        }
        before = effects(value)
        enqueue_check(value)
        finished = value.runner.run_once(value.actor, "checks")
        report = finished["result"]["operation_check"]
        assert finished["state"] == "succeeded" and report["eligible"] is False
        assert report["checks"][-1] == {"code": "native_handler", "status": "unsupported"}
        assert value.requests == [] and effects(value) == before
    finally:
        value.http.close()


def native(app_env, tmp_path, monkeypatch):
    _, _, _, _, state, _ = setup(app_env, tmp_path)
    actor, agent = app_env.context, uuid4()
    rootfs, home, private, tools = (
        tmp_path / name for name in ("rootfs", "home", "private", "tools")
    )
    for path in (rootfs, home, private, tools / "fixture/static"):
        path.mkdir(parents=True)
    executable = tools / "fixture/static/cog"
    executable.write_bytes(b"fixture native executable")
    executable.chmod(0o755)
    binary_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    entrypoint = "/usr/local/tools/fixture/static/cog"
    runtime = SharedWorkspaceRuntime(
        "a" * 64,
        rootfs,
        home,
        "b" * 64,
        shared_tools=tools,
        tool_entrypoints=((entrypoint, binary_hash),),
    )
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(
        actor.workspace_id,
        digest=runtime.image_digest,
        image_ref="ghcr.io/test/native@sha256:" + runtime.image_digest,
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256=runtime.qualification_sha256,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="operation-checks")
    homes.provision(actor.workspace_id, agent)
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    backend.operator_attachment = SimpleNamespace(require_healthy=lambda: None)
    provenance = {
        "profile": "potbot.syn-cog-native-binding/1",
        "workspace_id": str(actor.workspace_id),
        "agent_id": str(agent),
        "node": "d" * 64,
        "enrollment_sha256": "e" * 64,
        "boot_id": str(uuid4()),
        "session_id": str(uuid4()),
    }
    transport = NativeMeshClient.__new__(NativeMeshClient)
    transport._agents = {agent: provenance}
    transport._closed = False
    transport.workspace_id = actor.workspace_id
    transport.scope = {"session_id": provenance["session_id"]}
    probes = []
    transport._exchange = lambda request: probes.append(request) or {}
    mesh = ReleasedSynWorkerNodeMesh(transport)
    mesh._reader = SimpleNamespace(is_alive=lambda: True)
    route = SynContextRoute(
        workspace_id=actor.workspace_id,
        operator_boot="2" * 32,
        caller_uid=1000,
        round_trip=lambda *_: pytest.fail("Eligibility must not perform Context work"),
    )
    executor = SandboxedCogExecutor(
        runtime, ledger, homes, ProcessSupervisor(backend), mesh, syn_context_route=route
    )
    profile = "f" * 64
    operation = SandboxedCogOperation(
        WorkBinding(
            "tool",
            "syn://thing/fixture",
            "syn://type/in",
            "syn://type/out",
            binary_hash,
            "read_only",
            ExecutionPolicy(
                profile,
                runtime.qualification_sha256,
                state["content_digest"],
                "fixture",
                max_wall_ms=1000,
            ),
        ),
        (entrypoint,),
        binary_hash,
        protocol="context-rpc/v1",
    )
    executor.bind_context(app_env.db, actor, (operation,))
    task_service = TaskService(app_env.db)
    task = task_service.submit(
        actor,
        app_env.repository_id,
        ContractSpec(
            objective="Check and run exact installed native operation",
            baseline_ref="git:" + "1" * 40,
            deliverable_type=DeliverableType.INVESTIGATION,
            requirements=(Requirement("R1", "Check exact source", "Retain receipts"),),
            change_scope=(),
            capabilities=("read", "cog.execute:" + operation.binding.operation_ref),
            verification_policy="checks-v1",
            budget=(("tokens", 0), ("calls", 2), ("tool_ms", 2000), ("concurrency", 1)),
        ),
        "native-check-task",
    )
    task_id = UUID(task["task_id"])
    task_service.transition(
        actor, task_id, 0, TaskStatus.INITIALIZING, "initialize", idempotency_key="init"
    )
    task_service.transition(actor, task_id, 1, TaskStatus.RUNNING, "run", idempotency_key="run")
    policy = CogCapabilityPolicy(
        actor,
        agent,
        provenance["node"],
        profile,
        (CogCapabilityGrant(installation_hash(operation), frozenset({Capability.READ}), 2**31),),
        enrollment_sha256=provenance["enrollment_sha256"],
    )
    publish_cog_policy(app_env.admin_dsn, app_env.schema, policy, 0)
    executions = []

    async def execute(op, value, context, selected_actor, selected_agent, *, syn_authorization):
        mesh.consume(syn_authorization, op, value, context, selected_actor, selected_agent)
        executions.append(op.binding.operation_ref)
        return TypedCogValue(op.binding.output_type_ref, {"checked": True})

    monkeypatch.setattr(executor, "execute", execute)
    jobs = JobService(app_env.db)
    gateway = HostedInferenceGateway(
        app_env.db,
        HostedPolicy(),
        lambda _: pytest.fail("Native check touched provider credential"),
    )
    dispatcher = NativeCogDispatcher(
        executor, (operation,), frozenset({agent}), profile, persistent_cog_grants=True
    )
    spec = {
        "job_type": "cog.execute",
        "episode_id": str(task_id),
        "input": {
            "agent_id": str(agent),
            "repository_state_id": str(state["id"]),
            "operation_ref": operation.binding.operation_ref,
            "profile_sha256": profile,
            "type_ref": operation.binding.input_type_ref,
            "value": {"input": "private-native-value"},
        },
        "selected_models": [],
    }
    return SimpleNamespace(
        actor=actor,
        jobs=jobs,
        spec=spec,
        reference=operation.binding.operation_ref,
        runtime=runtime,
        ledger=ledger,
        executor=executor,
        dispatcher=dispatcher,
        policy=policy,
        mesh=mesh,
        probes=probes,
        executions=executions,
        state=state,
        executable=executable,
        task_id=task_id,
        runner=HostedJobRunner(
            jobs, gateway, native_cogs=dispatcher, operation_setup_token=lambda: TOKEN
        ),
    )


def test_native_check_observes_real_pins_and_grants_without_issuing_authority(
    app_env, tmp_path, monkeypatch
):
    value = native(app_env, tmp_path, monkeypatch)
    _, data = enqueue_check(value)
    before = effects(value)
    checked = value.runner.run_once(value.actor, "checks")
    report = checked["result"]["operation_check"]
    assert report["eligible"] is True and report["bindings"]["grantGeneration"] == 1
    assert value.mesh._grants == {} and value.executions == [] and effects(value) == before
    assert any(request.get("verify") is True for request in value.probes)
    assert "private-native-value" not in json.dumps(report)
    launch(value, checked, data)
    result = value.runner.run_once(value.actor, "native-worker")
    assert result["state"] == "succeeded" and value.executions == [value.reference]
    assert value.mesh._grants == {} and result["result"]["artifact_id"]
    assert BudgetLedger(app_env.db).inspect(value.actor, value.task_id)["used"] == {
        "calls": 1,
        "tool_ms": 1000,
    }


def test_native_workbench_catalogue_to_worker_lifecycle_and_verified_artifact(
    app_env, tmp_path, monkeypatch
):
    value = native(app_env, tmp_path, monkeypatch)
    reference = "syn://cog/fixture"
    record = SynObject(
        SynRef(reference),
        SynRef("syn://type/cog"),
        canonical_json({"id": reference, "typeRef": "syn://type/cog", "name": "Fixture"}),
    )
    SynService(
        app_env.db, ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    ).import_snapshot(
        value.actor,
        app_env.repository_id,
        "operation-check-fixture",
        SynSnapshot("explicit synthetic operation fixture", "extend_builtin", (), (record,)),
    )
    workbench = OperationWorkbench(app_env.db)
    item = next(
        item
        for item in workbench.catalogue.list(value.actor)["items"]
        if item["reference"] == reference
    )
    assert workbench.describe(value.actor, item["id"])["supported"] is True
    queued = workbench.check(value.actor, item["id"], value.spec, "check", setup_token=TOKEN)
    checked = value.runner.run_once(value.actor, "checks")
    assert checked["id"] == queued["job"]["id"]
    assert checked["result"]["operation_check"]["eligible"] is True
    assert value.mesh._grants == {} and value.executions == []
    launched = workbench.launch(
        value.actor,
        item["id"],
        UUID(checked["id"]),
        value.spec,
        "launch",
        setup_token=TOKEN,
    )
    executed = value.runner.run_once(value.actor, "execution")
    assert executed["id"] == launched["job"]["id"] and executed["state"] == "succeeded"
    result = workbench.result(value.actor, item["id"], UUID(executed["id"]))
    assert json.loads(result["artifactText"]) == {
        "type_ref": "syn://type/out",
        "value": {"checked": True},
    }
    assert result["artifact"]["id"] == executed["result"]["artifact_id"]
    assert value.executions == [value.reference] and value.mesh._grants == {}


@pytest.mark.parametrize("change", ["grant", "source", "release", "node", "profile", "context"])
def test_native_linked_launch_rejects_changed_binding_before_grant_or_budget(
    app_env, tmp_path, monkeypatch, change
):
    value = native(app_env, tmp_path, monkeypatch)
    _, data = enqueue_check(value)
    checked = value.runner.run_once(value.actor, "checks")
    assert checked["result"]["operation_check"]["eligible"] is True
    launch(value, checked, data)
    if change == "grant":
        publish_cog_policy(app_env.admin_dsn, app_env.schema, replace(value.policy, grants=()), 1)
    elif change == "source":
        value.executable.write_bytes(b"changed executable")
    elif change == "node":
        value.mesh.transport._agents[next(iter(value.mesh.transport._agents))]["boot_id"] = str(
            uuid4()
        )
    elif change == "profile":
        value.dispatcher.profile_hash = "0" * 64
    elif change == "context":
        value.executor._syn_context_route.operator_boot = "3" * 32
    else:
        hold = {
            "schema": "potbot-tool-release-hold/1",
            "slug": "fixture",
            "workspace_id": str(value.actor.workspace_id),
            "agent_id": str(value.policy.agent_id),
            "operation_id": str(uuid4()),
            "direction": "activate",
            "definition_sha256": "a" * 64,
            "commit": "b" * 40,
            "identity_sha256": "c" * 64,
            "image_digest": value.runtime.image_digest,
            "qualification_sha256": value.runtime.qualification_sha256,
            "test_evidence_sha256": "d" * 64,
            "suite_sha256": "e" * 64,
            "entrypoints": dict(value.runtime.tool_entrypoints),
        }
        write_hold(value.runtime.shared_tools, "fixture", hold)
    before = effects(value)
    result = value.runner.run_once(value.actor, "native-worker")
    assert result["state"] == "failed" and result["dispatched_at"] is None
    assert value.mesh._grants == {} and value.executions == [] and effects(value) == before


@pytest.mark.parametrize("missing", ["binding", "route", "broker"])
def test_native_check_reports_missing_protocol_dependencies_without_grants(
    app_env, tmp_path, monkeypatch, missing
):
    value = native(app_env, tmp_path, monkeypatch)
    if missing == "binding":
        value.executor._context_binding = None
    elif missing == "route":
        value.executor._syn_context_route = None
    else:
        from potbot.tools.rest_cog import REQUEST

        operation = value.dispatcher.operations[value.reference]
        value.dispatcher.operations[value.reference] = replace(
            operation, binding=replace(operation.binding, input_type_ref=REQUEST)
        )
        value.spec["input"]["type_ref"] = REQUEST
    before = effects(value)
    enqueue_check(value)
    checked = value.runner.run_once(value.actor, "checks")
    report = checked["result"]["operation_check"]
    assert report["eligible"] is False and report["checks"][-1] == {
        "code": "native_protocol",
        "status": "unsupported" if missing == "broker" else "blocked",
    }
    assert value.mesh._grants == {} and value.executions == [] and effects(value) == before
