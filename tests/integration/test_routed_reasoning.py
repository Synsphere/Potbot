"""Routed reasoning resumes through the real checked queue and budget ledger.

The HTTP fixture supplies synthetic model replies. No live model or transport
qualification is claimed.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.model_route_bindings import worker_admission_identity
from potbot.controller.model_routing import ModelRoutingService
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.controller.routed_reasoning import RoutedReasoning
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from tests.integration import test_hosted_gateway as mock
from tests.integration.test_model_route_bindings import accept
from tests.integration.test_operation_checks import TOKEN, effects, hosted
from tests.integration.test_operation_workbench import peer

pytestmark = pytest.mark.postgres

ROLES = ("hypothesis_generator", "critic", "synthesizer")
PROPOSALS = {
    "hypotheses": [
        {"hypothesis": "Try an indexed lookup", "assumptions": [], "questions": []},
        {"hypothesis": "Measure a scan baseline", "assumptions": [], "questions": []},
    ]
}
CRITIQUE = "Check the indexing benefit against a measured baseline."
SYNTHESIS = "Measure a baseline, then compare the indexed lookup on the same input."


@pytest.fixture
def routed(app_env):
    value = hosted(app_env)
    try:
        value.env = app_env
        value.routing = ModelRoutingService(app_env.db)
        value.review_service = ModelRoutingService(app_env.other_db)
        value.reviewer = peer(app_env)
        workbench = OperationWorkbench(app_env.db)
        with app_env.db.transaction(value.actor) as connection:
            connection.execute(
                "INSERT INTO model_catalog(workspace_id,id,canonical_model,role)"
                " VALUES(%s,%s,'fixture-model','reasoning')",
                (value.actor.workspace_id, uuid4()),
            )
        operation_id = next(
            item["id"]
            for item in workbench.catalogue.list(value.actor)["items"]
            if item["reference"] == value.reference
        )
        executor = {
            "job_type": "model.generate",
            "selected_models": value.spec["selected_models"],
            "input": {},
        }
        baseline = {
            "operation_id": operation_id,
            "runtime_identity_sha256": "a" * 64,
            "executor": executor,
        }
        baseline["runtime_identity_sha256"] = worker_admission_identity(
            baseline, {"policySha256": value.policy.fingerprint}
        )
        request = deepcopy(value.spec)
        del request["input"]["prompt"]
        stages = []
        for role in ROLES:
            value.policy_id = uuid4()
            recipe = {
                "schema": "potbot-route-prompt-recipe/v1",
                "model": "fixture-model",
                "role": role,
                "variant": "model_role",
                "output_contract": None if role == "synthesizer" else "json_object_v1",
            }
            value.routing.create(
                value.actor,
                value.policy_id,
                reviewer_id=value.reviewer.principal_id,
                role="reasoning",
                atomic_kind=role,
                baseline=baseline,
                candidate={**baseline, "runtime_identity_sha256": "b" * 64},
                prompt_contract_sha256=content_hash(recipe),
                evaluation_plan_sha256="e" * 64,
            )
            value.routing.configure_prompt(value.actor, value.policy_id, recipe)
            accept(value)
            value.routing.transition(value.actor, value.policy_id, expected_epoch=1, stage="shadow")
            stages.append({"role": role, "policy_id": str(value.policy_id), "request": request})
        value.plan = {
            "task": "Compare two synthetic indexing approaches",
            "evidence": [{"source": "fixture", "text": "Timing evidence is not yet available."}],
            "constraints": ["Treat all candidate timings as unverified."],
            "stages": stages,
        }
        yield value
    finally:
        value.http.close()


def _advance(routed, run_id):
    return RoutedReasoning(routed.env.db).advance(routed.actor, run_id, setup_token=TOKEN)


def _worker(routed, state):
    job = routed.runner.run_once(routed.actor, "routed-reasoning-worker")
    assert job is not None and job["state"] == state, job
    return job


def _queue_current_stage(routed, run_id, role):
    pending = _advance(routed, run_id)
    assert pending["phase"] == role and pending["state"] == "active", pending
    assert pending["check_job_id"] is not None and pending["job_id"] is None, pending
    assert _advance(routed, run_id) == pending
    check = _worker(routed, "succeeded")
    assert (
        check["id"] == pending["check_job_id"] and check["result"]["operation_check"]["eligible"]
    ), check
    queued = _advance(routed, run_id)
    assert queued["job_id"] is not None and queued["check_job_id"] == pending["check_job_id"]
    assert queued["decision_id"] == pending["decision_id"]
    assert _advance(routed, run_id) == queued
    return queued


def _fixture_packet(request):
    messages = json.loads(request.content)["messages"]
    prompt = next(message["content"] for message in messages if message["role"] == "user")
    return json.loads(prompt.split("JSON data):\n", 1)[1])


def _critique(packet):
    branches = packet["context"]["branches"]
    assert len(branches) == len(PROPOSALS["hypotheses"])
    return {
        "evaluations": [
            {
                "node_id": branch.get("node_id", branch.get("id")),
                "relevance": 4,
                "evidence_alignment": 2,
                "falsification_risk": 1,
                "uncertainty": 2,
                "recommendation": "keep",
                "rationale": CRITIQUE,
                "issues": [],
                "tests": [],
            }
            for branch in branches
        ]
    }


def _race_adoption(routed, run_id, monkeypatch):
    both_have_read = Barrier(2, timeout=10)
    adopt = RoutedReasoning._adopt

    def simultaneous(controller, actor, row, answer):
        both_have_read.wait()
        return adopt(controller, actor, row, answer)

    with monkeypatch.context() as race:
        race.setattr(RoutedReasoning, "_adopt", simultaneous)
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(_advance, routed, run_id) for _ in range(2)]
            left, right = [attempt.result(timeout=15) for attempt in attempts]
    assert left == right
    return left


class _ExitAfterAllocation(BaseException):
    pass


def test_routed_reasoning_resumes_three_stages_and_charges_each_once(routed, monkeypatch):
    wire = mock.wire
    run_id = uuid4()
    controller = RoutedReasoning(routed.env.db)
    start = controller.start(routed.actor, run_id, routed.plan)
    assert start["state"] == "active" and start["stage_index"] == 0 and start["completed"] == []
    assert RoutedReasoning(routed.env.db).start(routed.actor, run_id, routed.plan) == start
    assert routed.runner.run_once(routed.actor, "before-admission") is None

    def exit_before_check(*_args, **_kwargs):
        raise _ExitAfterAllocation

    with monkeypatch.context() as crash:
        crash.setattr(ModelRoutingService, "check", exit_before_check)
        with pytest.raises(_ExitAfterAllocation):
            _advance(routed, run_id)
    allocated = controller.inspect(routed.actor, run_id)
    assert allocated["decision_id"] is not None and allocated["check_job_id"] is None

    stage_hashes = []

    def response():
        packet = _fixture_packet(routed.requests[-1])
        assert packet["task"] == routed.plan["task"]
        assert packet["evidence"] == routed.plan["evidence"]
        assert packet["constraints"] == routed.plan["constraints"]
        assert packet["context"].get("stage_outputs", []) == stage_hashes
        phase = len(routed.requests)
        if phase == 1:
            return wire((json.dumps(PROPOSALS),))
        if phase == 2:
            assert "Try an indexed lookup" in json.dumps(packet["context"])
            return wire((json.dumps(_critique(packet)),))
        assert phase == 3
        assert CRITIQUE in json.dumps(packet["context"])
        assert "Measure a scan baseline" in json.dumps(packet["context"])
        return wire((SYNTHESIS,))

    monkeypatch.setattr(mock, "wire", response)
    decisions = []
    for index, role in enumerate(ROLES, start=1):
        queued = _queue_current_stage(routed, run_id, role)
        assert len(routed.requests) == effects(routed)["usage"] == index - 1
        job = _worker(routed, "succeeded")
        assert queued["job_id"] == job["id"]
        decisions.append(queued["decision_id"])
        if index == 1:
            assert queued["decision_id"] == allocated["decision_id"]
            adopted = _race_adoption(routed, run_id, monkeypatch)
        else:
            adopted = _advance(routed, run_id)
        assert adopted["stage_index"] == index and len(adopted["completed"]) == index, adopted
        assert adopted["completed"][-1]["decision_id"] == decisions[-1]
        stage_hashes.append(adopted["completed"][-1]["output_sha256"])
        assert effects(routed)["usage"] == len(routed.requests) == index
        assert routed.runner.run_once(routed.actor, "no-automatic-successor") is None

    assert adopted["state"] == "completed" and adopted["phase"] is None
    assert _advance(routed, run_id) == adopted
    result = RoutedReasoning(routed.env.db).result(routed.actor, run_id)
    assert result["text"] == SYNTHESIS and result["decision_id"] == decisions[-1]
    assert result == controller.result(routed.actor, run_id)
    assert len(set(decisions)) == 3
    ledger = BudgetLedger(routed.env.db).inspect(routed.actor, routed.task_id)
    assert ledger["used"]["tokens"] == 45 and ledger["used"]["calls"] == 3
    assert ledger["held"] == {}
    assert len(routed.requests) == 3


@pytest.mark.parametrize("lost", [False, True], ids=["invalid-role", "lost-reply"])
def test_unconfirmed_or_invalid_routed_reply_blocks_successors(routed, monkeypatch, lost):
    wire = mock.wire
    if lost:

        def fail(request):
            raise httpx.ReadError("Synthetic routed reply lost", request=request)

        routed.callbacks.before_response = fail
    else:
        monkeypatch.setattr(mock, "wire", lambda: wire(('{"queries":["Wrong role"]}',)))
    service = RoutedReasoning(routed.env.db)
    run_id = uuid4()
    service.start(routed.actor, run_id, routed.plan)
    queued = _queue_current_stage(routed, run_id, ROLES[0])
    result = _worker(routed, "indeterminate" if lost else "succeeded")
    assert result["id"] == queued["job_id"]

    retained = _advance(routed, run_id)
    assert retained["state"] == "blocked" and retained["stage_index"] == 0, retained
    assert (
        retained["job_id"] == queued["job_id"] and retained["decision_id"] == queued["decision_id"]
    )
    assert retained["completed"] == [] and retained["error_code"]
    with pytest.raises(StateMismatchError):
        service.result(routed.actor, run_id)
    assert _advance(routed, run_id) == retained
    assert routed.runner.run_once(routed.actor, "no-replay") is None
    assert len(routed.requests) == 1
    assert effects(routed)["usage"] == int(not lost)
    with routed.env.db.transaction(routed.actor) as connection:
        reservations = connection.execute("SELECT status FROM budget_reservations").fetchall()
        decisions = connection.execute(
            "SELECT allocation_key FROM model_route_decisions"
        ).fetchall()
    assert len(decisions) == len(reservations) == 1
    if lost:
        assert reservations[0]["status"] == "indeterminate"


def test_routed_owner_and_plan_are_pinned_and_interrupt_fences_worker(routed):
    service = RoutedReasoning(routed.env.db)
    run_id = uuid4()
    service.start(routed.actor, run_id, routed.plan)
    with pytest.raises(StateMismatchError):
        service.start(routed.actor, run_id, {**routed.plan, "task": "Changed intent"})
    other = RoutedReasoning(routed.env.other_db)
    with pytest.raises(ScopeDeniedError):
        other.inspect(routed.reviewer, run_id)
    with pytest.raises(ScopeDeniedError):
        other.interrupt(routed.reviewer, run_id)

    queued = _queue_current_stage(routed, run_id, ROLES[0])
    interrupted = service.interrupt(routed.actor, run_id)
    assert interrupted["state"] == "interrupted" and interrupted["stage_index"] == 0
    assert interrupted["job_id"] == queued["job_id"] and interrupted["completed"] == []
    assert _advance(routed, run_id) == interrupted == service.interrupt(routed.actor, run_id)
    routed.runner.run_once(routed.actor, "interrupted-run")
    assert routed.runner.run_once(routed.actor, "no-extra-work") is None
    job = routed.jobs.inspect(routed.actor, UUID(queued["job_id"]))
    assert job["state"] in {"cancelled", "failed"} and job["dispatched_at"] is None, job
    assert effects(routed)["reservations"] == 0 and routed.requests == []


def test_routed_sql_prevents_future_allocation_and_dispatch_after_interrupt(routed):
    service = RoutedReasoning(routed.env.db)
    run_id = uuid4()
    service.start(routed.actor, run_id, routed.plan)
    future = routed.plan["stages"][1]
    packet = {
        "task": routed.plan["task"],
        "constraints": routed.plan["constraints"],
        "evidence": routed.plan["evidence"],
        "context": {"branches": [{"node_id": "fixture"}]},
    }
    with pytest.raises(RevisionConflictError):
        routed.routing.allocate_prompted(
            routed.actor,
            UUID(future["policy_id"]),
            f"rrun-v1:{run_id}:1",
            future["request"],
            packet,
        )
    initial = routed.plan["stages"][0]
    invalid_facts = {**packet, "task": "Replace the retained investigation", "context": {}}
    with pytest.raises(RevisionConflictError):
        routed.routing.allocate_prompted(
            routed.actor,
            UUID(initial["policy_id"]),
            f"rrun-v1:{run_id}:0",
            initial["request"],
            invalid_facts,
        )
    assert service.inspect(routed.actor, run_id)["decision_id"] is None
    queued = _queue_current_stage(routed, run_id, ROLES[0])
    lease = routed.jobs.claim(routed.actor, "claimed-before-interrupt")
    assert lease is not None and str(lease.job_id) == queued["job_id"]
    # Stop the run directly so this exercises the database fence independently
    # of the controller's best-effort cancellation of a linked job.
    _record_interrupt(routed, run_id)
    with pytest.raises(RevisionConflictError):
        routed.jobs.mark_dispatched(routed.actor, lease)
    job = routed.jobs.inspect(routed.actor, UUID(queued["job_id"]))
    assert job["id"] == queued["job_id"] and job["dispatched_at"] is None
    assert service.inspect(routed.actor, run_id)["state"] == "interrupted"
    assert effects(routed)["reservations"] == 0 and routed.requests == []


def _record_interrupt(routed, run_id):
    with routed.env.db.transaction(routed.actor) as connection:
        connection.execute(
            "UPDATE routed_reasoning_runs SET state='interrupted'"
            " WHERE workspace_id=%s AND id=%s AND principal_id=%s",
            (routed.actor.workspace_id, run_id, routed.actor.principal_id),
        )


def test_routed_interrupt_between_job_and_hosted_dispatch_releases_unsent_hold(routed, monkeypatch):
    run_id = uuid4()
    service = RoutedReasoning(routed.env.db)
    service.start(routed.actor, run_id, routed.plan)
    queued = _queue_current_stage(routed, run_id, ROLES[0])
    authorize = routed.gateway._authorize_dispatch
    interrupted = []

    def stop_before_send(actor, lease, invocation_id):
        job = routed.jobs.inspect(actor, lease.job_id)
        assert job["dispatched_at"] is not None
        _record_interrupt(routed, run_id)
        interrupted.append(lease.job_id)
        authorize(actor, lease, invocation_id)

    monkeypatch.setattr(routed.gateway, "_authorize_dispatch", stop_before_send)
    job = _worker(routed, "failed")
    assert interrupted == [UUID(queued["job_id"])]
    assert job["error_code"] == "revision_conflict" and routed.requests == [], job
    assert job["result"]["completed_models"] == 0
    ledger = BudgetLedger(routed.env.db).inspect(routed.actor, routed.task_id)
    assert ledger["held"] == {} and ledger["used"].get("calls", 0) == 0
    [invocation] = routed.gateway.list_for_job(routed.actor, UUID(queued["job_id"]))["items"]
    assert invocation["status"] == "rejected"
    with routed.env.db.transaction(routed.actor) as connection:
        events = connection.execute("SELECT kind FROM hosted_invocation_events").fetchall()
    assert "dispatched" not in {event["kind"] for event in events}
    assert _advance(routed, run_id)["state"] == "interrupted"
    assert routed.runner.run_once(routed.actor, "no-replay") is None


def test_routed_reasoning_cli_requires_scope_and_resumes_checked_worker(
    routed, monkeypatch, tmp_path, capsys
):
    from potbot.api import jobs_cli, operations_cli
    from potbot.cli import main

    token_reads = []

    def database_context(args):
        assert (args.workspace_id, args.principal_id, args.schema) == (
            routed.actor.workspace_id,
            routed.actor.principal_id,
            routed.env.schema,
        )
        return routed.env.db, routed.actor

    def setup_token():
        token_reads.append(None)
        return TOKEN

    monkeypatch.setattr(jobs_cli, "database_context", database_context)
    monkeypatch.setattr(operations_cli, "_setup_token", setup_token)
    run_id = str(uuid4())
    workspace_scope = ["--workspace", str(routed.actor.workspace_id)]
    principal_scope = ["--principal", str(routed.actor.principal_id)]
    for present, missing in ((workspace_scope, "--principal"), (principal_scope, "--workspace")):
        with pytest.raises(SystemExit) as failure:
            main(["reasoning", *present, "inspect", run_id])
        assert failure.value.code == 2
        assert missing in capsys.readouterr().err
    scope = [*workspace_scope, *principal_scope, "--schema", routed.env.schema]
    plan_path = tmp_path / "private-reasoning-plan.json"
    plan_path.write_text(json.dumps(routed.plan), encoding="utf-8")

    def command(name, *options, expected=0):
        assert main(["reasoning", *scope, name, run_id, *options]) == expected
        output = capsys.readouterr()
        assert output.err == ""
        assert TOKEN not in output.out and routed.plan["task"] not in output.out
        assert routed.plan["evidence"][0]["text"] not in output.out
        return json.loads(output.out)

    started = command("start", "--plan", str(plan_path))
    assert command("inspect") == started and started["check_job_id"] is None
    assert command("result", expected=1)["error"]["code"] == "state_mismatch"
    assert token_reads == []
    checking = command("advance")
    check = _worker(routed, "succeeded")
    assert (
        check["id"] == checking["check_job_id"] and check["result"]["operation_check"]["eligible"]
    )
    queued = command("advance")
    assert queued["decision_id"] == checking["decision_id"] and queued["job_id"]
    interrupted = command("interrupt", "--confirm")
    assert interrupted["state"] == "interrupted" and interrupted["job_id"] == queued["job_id"]
    assert command("inspect") == interrupted == command("follow", expected=2)
    assert len(token_reads) == 2
    assert routed.runner.run_once(routed.actor, "after-cli-interruption") is None
    assert routed.requests == [] and effects(routed)["reservations"] == 0
