"""Native inference admission and settlement against the real task budget ledger."""

from uuid import UUID, uuid4

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.jobs import JobService
from potbot.controller.tasks import TaskService
from potbot.domain.errors import BudgetUnavailableError, RevisionConflictError, StateMismatchError
from potbot.domain.jobs import COG_JOB
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.models.native_results import REQUEST
from tests.integration.test_repository_states import setup
from tests.unit.test_model_reasoning_results import deployment, request

pytestmark = pytest.mark.postgres

WALL_MS = 1_000
MODEL = deployment()
MODEL_HOLD = {
    "tokens": MODEL.usable_context_tokens,
    "calls": 1,
    "tool_ms": WALL_MS,
    "concurrency": 1,
}


@pytest.fixture
def model_task(app_env, tmp_path):
    _, _, _, _, repository_state, _ = setup(app_env, tmp_path)
    actor = app_env.context
    service = TaskService(app_env.db)
    operation = "syn://cog/native-model-budget-test"

    def launch(*, tokens=MODEL.usable_context_tokens):
        spec = ContractSpec(
            objective="Meter a native model call",
            baseline_ref="git:" + "1" * 40,
            deliverable_type=DeliverableType.INVESTIGATION,
            requirements=(Requirement("R1", "Run one model call", "Record bounded usage"),),
            change_scope=(),
            capabilities=("cog.execute:" + operation,),
            verification_policy="checks-v1",
            budget=(("tokens", tokens), ("calls", 2), ("tool_ms", 2 * WALL_MS), ("concurrency", 1)),
        )
        task = service.submit(actor, app_env.repository_id, spec, "model-budget-task")
        task_id = UUID(task["task_id"])
        service.transition(
            actor, task_id, 0, TaskStatus.INITIALIZING, "initialize", idempotency_key="init"
        )
        service.transition(actor, task_id, 1, TaskStatus.RUNNING, "run", idempotency_key="run")

        jobs = JobService(app_env.db)
        jobs.submit(
            actor,
            {
                "job_type": COG_JOB,
                "episode_id": str(task_id),
                "input": {
                    "agent_id": str(uuid4()),
                    "repository_state_id": str(repository_state["id"]),
                    "operation_ref": operation,
                    "profile_sha256": "f" * 64,
                    "type_ref": REQUEST,
                    "value": request(),
                },
                "selected_models": [],
            },
            "invoke-native-model",
        )
        lease = jobs.claim(actor, "native-model-worker", kinds=(COG_JOB,))
        assert lease is not None
        return jobs, lease

    return launch


def reserve(jobs, actor, lease):
    return jobs.mark_dispatched(actor, lease, cog_max_wall_ms=WALL_MS, cog_model_deployment=MODEL)


def test_native_model_settles_actual_tokens_once(app_env, model_task):
    actor = app_env.context
    jobs, lease = model_task()
    ledger = BudgetLedger(app_env.db)

    assert reserve(jobs, actor, lease)["dispatched_at"] is not None
    assert ledger.inspect(actor, lease.episode_id)["held"] == MODEL_HOLD
    with pytest.raises(StateMismatchError, match="cannot be repeated"):
        reserve(jobs, actor, lease)

    usage = {"input_tokens": 12, "output_tokens": 3, "tokens": 15}
    result = {"operation_id": str(lease.job_id), "artifact_id": str(uuid4()), "usage": usage}
    finished = jobs.finish(actor, lease, "succeeded", result=result)
    assert finished["result"] == result

    restarted = JobService(app_env.db)
    with pytest.raises(RevisionConflictError):
        restarted.finish(actor, lease, "succeeded", result=result)
    assert restarted.claim(actor, "restarted-worker", kinds=(COG_JOB,)) is None
    budget = ledger.inspect(actor, lease.episode_id)
    assert budget["held"] == {}
    assert budget["used"] == {"tokens": 15, "calls": 1, "tool_ms": WALL_MS}
    with app_env.db.transaction(actor) as conn:
        assert conn.execute("SELECT count(*) AS total FROM budget_usage").fetchone()["total"] == 1


def test_native_model_exhausted_tokens_block_before_dispatch(app_env, model_task):
    actor = app_env.context
    jobs, lease = model_task(tokens=MODEL.usable_context_tokens - 1)

    with pytest.raises(BudgetUnavailableError):
        reserve(jobs, actor, lease)

    assert jobs.inspect(actor, lease.job_id)["dispatched_at"] is None
    assert BudgetLedger(app_env.db).inspect(actor, lease.episode_id)["held"] == {}
    events = jobs.events(actor, lease.job_id)["items"]
    assert "job.dispatched" not in {event["event_type"] for event in events}


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": 12, "output_tokens": 3, "tokens": 16},
        {"input_tokens": 12, "output_tokens": 17, "tokens": 29},
        {
            "input_tokens": MODEL.usable_context_tokens,
            "output_tokens": 3,
            "tokens": MODEL.usable_context_tokens + 3,
        },
    ],
)
def test_native_model_refuses_invalid_usage_without_settling(app_env, model_task, usage):
    actor = app_env.context
    jobs, lease = model_task()
    reserve(jobs, actor, lease)
    result = {"operation_id": str(lease.job_id), "artifact_id": str(uuid4()), "usage": usage}

    with pytest.raises(StateMismatchError, match="Native model usage"):
        jobs.finish(actor, lease, "succeeded", result=result)

    assert jobs.inspect(actor, lease.job_id)["state"] == "running"
    budget = BudgetLedger(app_env.db).inspect(actor, lease.episode_id)
    assert budget["used"] == {} and budget["held"] == MODEL_HOLD


@pytest.mark.parametrize("expired", [False, True])
def test_native_model_loss_keeps_hold_and_cannot_replay_after_restart(app_env, model_task, expired):
    actor = app_env.context
    jobs, lease = model_task()
    reserve(jobs, actor, lease)

    if expired:
        with app_env.db.transaction(actor) as conn:
            conn.execute(
                "UPDATE jobs SET lease_until=clock_timestamp() WHERE id=%s", (lease.job_id,)
            )
        assert jobs.expire(actor) == 1
    else:
        jobs.finish(actor, lease, "indeterminate")

    restarted = JobService(app_env.db)
    original = restarted.inspect(actor, lease.job_id)
    assert original["state"] == "indeterminate" and original["result"] is None
    assert restarted.claim(actor, "restarted-worker", kinds=(COG_JOB,)) is None
    budget = BudgetLedger(app_env.db).inspect(actor, lease.episode_id)
    assert budget["used"] == {} and budget["held"] == MODEL_HOLD
    with app_env.db.transaction(actor) as conn:
        assert (
            conn.execute("SELECT status FROM budget_reservations").fetchone()["status"]
            == "indeterminate"
        )


def test_native_model_route_veto_at_dispatch_rolls_back_reservation(app_env, tmp_path, monkeypatch):
    from potbot.controller.checked_reasoning import CheckedReasoning
    from tests.integration.test_checked_reasoning import begin
    from tests.integration.test_model_reasoning_results import native_route
    from tests.integration.test_operation_checks import TOKEN, effects

    routed = native_route(app_env, tmp_path, monkeypatch, installed_model=True)
    reasoning = CheckedReasoning(app_env.db)
    pending = begin(reasoning, routed)
    decision = UUID(pending["decision_id"])
    routed.runner.run_once(routed.actor, "native-route-check")
    queued = reasoning.launch(routed.actor, decision, setup_token=TOKEN)
    dispatch = routed.jobs.mark_dispatched

    def block_at_dispatch(*args, **kwargs):
        # The application-level recheck already passed. PostgreSQL must fence
        # this update and roll back the reservation in the dispatch transaction.
        routed.routing.transition(routed.actor, routed.policy_id, expected_epoch=2, stage="blocked")
        return dispatch(*args, **kwargs)

    monkeypatch.setattr(routed.jobs, "mark_dispatched", block_at_dispatch)
    completed = routed.runner.run_once(routed.actor, "native-route-execution")
    assert completed["id"] == queued["job_id"]
    assert completed["state"] == "failed" and completed["error_code"] == "state_mismatch"
    assert completed["dispatched_at"] is None and completed["result"] is None
    assert effects(routed)["reservations"] == effects(routed)["usage"] == 0
    assert routed.executions == routed.inference_holds == []
    recovered = CheckedReasoning(app_env.db)
    assert recovered.launch(routed.actor, decision, setup_token=TOKEN)["state"] == "failed"
    assert routed.runner.run_once(routed.actor, "native-route-recovery") is None
