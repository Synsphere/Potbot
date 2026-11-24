"""R7.2-B transactional gate over actual PostgreSQL and the existing job owner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.controller.atomic_graph_runtime import AtomicGraphService, AtomicLaunchBinding
from potbot.controller.atomic_scheduler import AtomicAdmissionPlanner, ProposedLeaf, RegisteredLeaf
from potbot.controller.atomic_tasks import AtomicOperation, TaskGraph
from potbot.controller.jobs import JobService
from potbot.controller.tasks import TaskService
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError, StateMismatchError
from potbot.domain.jobs import CHECK_JOB, COG_JOB
from potbot.domain.syn import SynRef
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement

pytestmark = pytest.mark.postgres

OP_ID = "a" * 64
COG = "syn://cog/potbot-atomic-run-registered-tool-v1"
THING = "syn://thing/potbot-atomic-run-registered-tool-v1"


def _fixture(app_env, *, max_parallel=2, locks=()):
    contract = ContractSpec(
        objective="Execute small checked operations",
        baseline_ref="git:" + "1" * 40,
        deliverable_type=DeliverableType.INVESTIGATION,
        requirements=(Requirement("R1", "Verify each leaf", "Retain checked results"),),
        change_scope=("src/app.py",),
        capabilities=("cog.execute:" + THING,),
        verification_policy="tests-v1",
        budget=(("tokens", 10000), ("calls", 10), ("tool_ms", 300000), ("concurrency", 2)),
    )
    task = TaskService(app_env.db).submit(
        app_env.context, app_env.repository_id, contract, str(uuid4())
    )
    episode = UUID(task["task_id"])
    graph = TaskGraph("Three bounded operations")
    graph.decompose(
        "root",
        (
            ("first", "First", ()),
            ("second", "Independent", ()),
            ("third", "Depends on first", ("first",)),
        ),
    )
    registry = {
        "run_registered_tool": RegisteredLeaf(
            AtomicOperation("run_registered_tool", "execution", 1024, 1000),
            CogSignature(
                SynRef(COG),
                "syn://type/potbot-atomic-cog",
                "syn://type/potbot-atomic-request",
                "syn://type/potbot-atomic-response",
                "potbot.cogs.execution.atomic",
                1,
            ),
            locks,
        )
    }
    planner = AtomicAdmissionPlanner(graph, registry, max_parallel=max_parallel)
    requests = {}
    proposals = []
    for name in ("first", "second", "third"):
        value = {"task": name}
        proposals.append(
            ProposedLeaf(
                name,
                "run_registered_tool",
                TypedCogValue("syn://type/potbot-atomic-request", value),
            )
        )
        requests[name] = {
            "job_type": COG_JOB,
            "episode_id": str(episode),
            "input": {
                "agent_id": str(uuid4()),
                "repository_state_id": str(uuid4()),
                "operation_ref": THING,
                "profile_sha256": "b" * 64,
                "type_ref": "syn://type/potbot-atomic-request",
                "value": value,
            },
            "selected_models": [],
        }
    planner.admit(tuple(proposals))
    jobs = JobService(app_env.db)
    bindings = {}
    for name, payload in requests.items():
        # The actual checked-workbench suite establishes signed verification.
        # These fixture receipts deliberately exercise the graph SQL boundary
        # without claiming to qualify an installed Syn profile.
        check = jobs.submit(
            app_env.context,
            {
                "job_type": CHECK_JOB,
                "episode_id": str(episode),
                "input": {
                    "operation_id": OP_ID,
                    "request": payload,
                    "authorization": {"expires_at": 2000000000, "mac": "a" * 64},
                },
                "selected_models": [],
            },
            f"check-{uuid4()}",
            trusted_operation=True,
        )
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE jobs SET state='succeeded',finished_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (app_env.context.workspace_id, UUID(check["id"])),
            )
        bindings[name] = AtomicLaunchBinding(OP_ID, UUID(check["id"]), payload)
    service = AtomicGraphService(app_env.db)
    graph_id = uuid4()
    assert service.admit(app_env.context, graph_id, planner, bindings)["state"] == "active"
    return service, jobs, graph_id, planner, bindings


def _launch(app_env, jobs, binding, *, key=None):
    return jobs.submit(
        app_env.context,
        binding.request,
        key or str(uuid4()),
        trusted_operation=True,
        operation_check_id=binding.check_job_id,
        operation_authorization={"expires_at": 2000000000, "mac": "a" * 64},
    )


def _succeed(app_env, job):
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE jobs SET state='succeeded',finished_at=clock_timestamp() "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(job["id"])),
        )


def test_graph_is_idempotent_and_owner_scoped(app_env):
    service, _, identity, planner, bindings = _fixture(app_env)
    again = service.admit(app_env.context, identity, planner, bindings)
    assert again["replayed"] is True
    assert [row["node_id"] for row in service.ready(app_env.context, identity)] == [
        "first",
        "second",
    ]
    with pytest.raises(ScopeDeniedError):
        service.inspect(app_env.other_context, identity)
    planner.max_parallel = 1  # changed replay content must not overwrite immutable admission
    with pytest.raises(StateMismatchError):
        service.admit(app_env.context, identity, planner, bindings)


def test_dependencies_and_existing_queue_are_the_only_execution_authority(app_env):
    service, jobs, identity, _, bindings = _fixture(app_env)
    with pytest.raises(RevisionConflictError):
        _launch(app_env, jobs, bindings["third"])
    first = _launch(app_env, jobs, bindings["first"])
    assert service.inspect(app_env.context, identity)["nodes"][0]["job_id"] == first["id"]
    with pytest.raises(RevisionConflictError):
        _launch(app_env, jobs, bindings["third"])
    _succeed(app_env, first)
    third = _launch(app_env, jobs, bindings["third"])
    assert third["state"] == "queued"
    assert service.inspect(app_env.context, identity)["nodes"][2]["job_id"] == third["id"]
    with pytest.raises(RevisionConflictError):
        _launch(app_env, jobs, bindings["first"], key=str(uuid4()))


def test_two_workers_cannot_bypass_exclusive_locks(app_env):
    service, jobs, identity, _, bindings = _fixture(app_env, locks=("repo:shared",))
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_launch, app_env, jobs, bindings["first"])
        b = pool.submit(_launch, app_env, jobs, bindings["second"])
        outcomes = []
        for future in (a, b):
            try:
                outcomes.append(future.result(timeout=20))
            except RevisionConflictError:
                outcomes.append(None)
    assert sum(item is not None for item in outcomes) == 1
    assert (
        len(
            [
                item
                for item in service.inspect(app_env.context, identity)["nodes"]
                if item["job_id"] is not None
            ]
        )
        == 1
    )
    _succeed(app_env, next(item for item in outcomes if item is not None))
    remaining = [
        name
        for name in ("first", "second")
        if not any(
            row["node_id"] == name and row["job_id"]
            for row in service.inspect(app_env.context, identity)["nodes"]
        )
    ]
    assert len(remaining) == 1
    assert _launch(app_env, jobs, bindings[remaining[0]])["state"] == "queued"


def test_interrupt_fences_launch_and_dispatch_and_requests_cancellation(app_env):
    service, jobs, identity, _, bindings = _fixture(app_env)
    first = _launch(app_env, jobs, bindings["first"])
    assert first["state"] == "queued"
    outcome = service.interrupt(app_env.context, identity)
    assert outcome["state"] == "interrupted"
    assert outcome["jobs"][0]["state"] == "cancelled"
    assert service.ready(app_env.context, identity) == ()
    with pytest.raises(RevisionConflictError):
        _launch(app_env, jobs, bindings["second"])
    with pytest.raises(RevisionConflictError):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE jobs SET dispatched_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (app_env.context.workspace_id, UUID(first["id"])),
            )
    assert service.interrupt(app_env.context, identity)["state"] == "interrupted"


def test_failed_or_indeterminate_leaf_never_implicitly_requeues(app_env):
    service, jobs, identity, _, bindings = _fixture(app_env)
    first = _launch(app_env, jobs, bindings["first"])
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE jobs SET state='indeterminate',finished_at=clock_timestamp() "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(first["id"])),
        )
    third = next(
        n for n in service.inspect(app_env.context, identity)["nodes"] if n["node_id"] == "third"
    )
    assert third["state"] == "blocked_dependency"
    with pytest.raises(RevisionConflictError):
        _launch(app_env, jobs, bindings["third"])
    assert third["job_id"] is None


def test_workspace_resource_holds_span_independent_graphs_and_survive_uncertainty(app_env):
    first_service, first_jobs, first_graph, _, first_bindings = _fixture(
        app_env, locks=("workspace-repo:source",)
    )
    second_service, second_jobs, second_graph, _, second_bindings = _fixture(
        app_env, locks=("workspace-repo:source",)
    )
    first = _launch(app_env, first_jobs, first_bindings["first"])
    with pytest.raises(RevisionConflictError):
        _launch(app_env, second_jobs, second_bindings["first"])
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE jobs SET state='indeterminate',finished_at=clock_timestamp() "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(first["id"])),
        )
    assert (
        first_service.inspect(app_env.context, first_graph)["nodes"][0]["state"] == "indeterminate"
    )
    # The owner cannot manufacture a release for work whose effects are unknown.
    with pytest.raises(RevisionConflictError):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE atomic_graph_resource_holds SET active=false,released_at=clock_timestamp() "
                "WHERE workspace_id=%s AND job_id=%s",
                (app_env.context.workspace_id, UUID(first["id"])),
            )
    with pytest.raises(RevisionConflictError):
        _launch(app_env, second_jobs, second_bindings["first"])
    assert second_service.inspect(app_env.context, second_graph)["nodes"][0]["job_id"] is None
