"""Real PostgreSQL races, dependencies and temporal reasoning execution."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.controller.reasoning_investigation import ReasoningInvestigator
from potbot.controller.reasoning_runtime import (
    PostgresReasoningStore,
    PostgresSteeringInbox,
    ReasoningContextChanged,
    ReasoningDispatchInterrupted,
)
from potbot.controller.reasoning_temporal import TemporalSteeringController
from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

pytestmark = pytest.mark.postgres


def _runtime(env: Any, path: Path) -> tuple:
    run_id = uuid4().hex
    store, inbox = PostgresReasoningStore(env.db), PostgresSteeringInbox(env.db)
    token = store.start(env.context, run_id)
    tree = ReasoningTree(
        path / f"{run_id}.json",
        checkpoint=lambda snapshot: store.checkpoint(env.context, run_id, token, snapshot),
    )
    tree.save()
    owner = TemporalSteeringController(
        store, inbox, env.context, run_id, token, tree, lambda *a, **k: None
    )
    return store, inbox, run_id, token, tree, owner


def _claim(tree: ReasoningTree) -> Any:
    node = tree.add("bounded fixture call", role="hypothesis_generator")
    assert tree.claim(node.id)
    return node


def test_mixed_interrupt_fences_only_its_run_and_retains_sources(
    app_env: Any, tmp_path: Path
) -> None:
    store, inbox, run, token, tree, owner = _runtime(app_env, tmp_path)
    node = _claim(tree)
    dispatch = store.begin_dispatch(
        app_env.context, run, token, node_id=node.id, kind="model", context_epoch=0
    )
    request = inbox.submit(
        app_env.context,
        run,
        request_id="mixed",
        prompt="FYI, only 2GB RAM; stop now; add edge-case handling",
    )
    assert request["acknowledgement"]["dispatch_fenced"]
    assert not request["acknowledgement"]["in_flight_stop_confirmed"]
    deferred = request["components"][2]
    assert deferred["predecessor"] == {"kind": "dispatch", "id": str(dispatch)}
    with pytest.raises(ReasoningDispatchInterrupted):
        store.begin_dispatch(
            app_env.context, run, token, node_id=node.id, kind="model", context_epoch=1
        )
    with pytest.raises(ScopeDeniedError):
        inbox.get(app_env.other_context, run, "mixed")
    store.finish_dispatch(app_env.context, run, token, dispatch, outcome="indeterminate")
    with pytest.raises(ReasoningDispatchInterrupted):
        owner.check_interrupt()
    assert store.inspect(app_env.context, run)["state"] == "interrupted"
    retained = inbox.get(app_env.context, run, "mixed")
    assert retained["status"] == "queued"  # unexecuted deferred component survives
    assert {r["status"] for r in retained["component_results"].values()} == {
        "recorded",
        "dispatch_fenced",
    }
    assert tree.nodes[node.id].state == "indeterminate"
    assert tree.context_revisions[0]["principal_id"] == str(app_env.context.principal_id)
    assert inbox.component_readiness(app_env.context, run, deferred) == "blocked"
    assert (
        inbox.submit(app_env.context, run, request_id="mixed", prompt=request["prompt"]) == retained
    )
    with pytest.raises(InvalidInputError):
        inbox.submit(app_env.context, run, request_id="another", prompt="add more")
    other_store, _, other, other_token, other_tree, _ = _runtime(app_env, tmp_path)
    other_node = _claim(other_tree)
    assert other_store.begin_dispatch(
        app_env.context, other, other_token, node_id=other_node.id, kind="model", context_epoch=0
    )


@pytest.mark.parametrize(
    "outcome,expected",
    [("completed", "ready"), ("indeterminate", "blocked"), ("not_started", "blocked")],
)
def test_deferred_work_requires_exact_successful_predecessor(
    app_env: Any, tmp_path: Path, outcome: str, expected: str
) -> None:
    store, inbox, run, token, tree, owner = _runtime(app_env, tmp_path)
    node = _claim(tree)
    dispatch = store.begin_dispatch(
        app_env.context, run, token, node_id=node.id, kind="model", context_epoch=0
    )
    request = inbox.submit(
        app_env.context, run, request_id="later", prompt="Add edge-case handling"
    )
    component = request["components"][0]
    assert inbox.component_readiness(app_env.context, run, component) == "waiting"
    with pytest.raises(StateMismatchError, match="predecessor"):
        inbox.resolve_component(
            app_env.context, run, token, request, component["id"], {"status": "applied"}
        )
    assert not owner.eligible()
    store.finish_dispatch(app_env.context, run, token, dispatch, outcome=outcome)
    assert inbox.component_readiness(app_env.context, run, component) == expected
    eligible = owner.eligible()
    assert bool(eligible) == (expected == "ready")
    if eligible:
        owner.resolve(
            eligible[0],
            {"decision": "no_change", "explanation": "Already included", "task_revision": 0},
        )
    assert inbox.get(app_env.context, run, "later")["status"] == "decided"


def test_next_goal_waits_for_run_and_never_becomes_current_hypothesis(
    app_env: Any, tmp_path: Path
) -> None:
    store, inbox, run, token, tree, owner = _runtime(app_env, tmp_path)
    request = inbox.submit(
        app_env.context, run, request_id="next", prompt="Next task: build a dashboard"
    )
    component = request["components"][0]
    assert component["scope"] == "next_goal"
    assert not owner.eligible()
    tree.close("completed")
    assert inbox.component_readiness(app_env.context, run, component) == "ready_for_new_goal"
    assert not tree.nodes
    assert inbox.get(app_env.context, run, "next")["status"] == "queued"


def test_background_racing_dispatch_requires_recompile_without_spawning(
    app_env: Any, tmp_path: Path
) -> None:
    store, inbox, run, token, tree, owner = _runtime(app_env, tmp_path)
    node = _claim(tree)
    inbox.submit(app_env.context, run, request_id="memory", prompt="FYI, only 2GB RAM")
    with pytest.raises(ReasoningContextChanged):
        store.begin_dispatch(
            app_env.context, run, token, node_id=node.id, kind="model", context_epoch=0
        )
    assert owner.background()
    assert tree.context_epoch == 1 and tree.revision == 1
    assert len(tree.nodes) == 1
    dispatch = store.begin_dispatch(
        app_env.context, run, token, node_id=node.id, kind="model", context_epoch=1
    )
    store.finish_dispatch(app_env.context, run, token, dispatch, outcome="completed")
    assert len(store.events(app_env.context, run)) == len(tree.events)
    assert not owner.background()


def test_expired_owner_cannot_consume_or_dispatch_pending_components(
    app_env: Any, tmp_path: Path
) -> None:
    store, inbox, run, token, tree, _ = _runtime(app_env, tmp_path)
    node = _claim(tree)
    request = inbox.submit(app_env.context, run, request_id="queued", prompt="Add tests")
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE reasoning_runtime_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(hex=run)),
        )
    with pytest.raises(StateMismatchError):
        inbox.resolve_component(
            app_env.context,
            run,
            token,
            request,
            request["components"][0]["id"],
            {"status": "applied"},
        )
    with pytest.raises(StateMismatchError):
        store.begin_dispatch(
            app_env.context, run, token, node_id=node.id, kind="model", context_epoch=0
        )
    assert store.expire(app_env.context) == 1
    assert inbox.get(app_env.context, run, "queued")["component_results"] == {}
    assert inbox.component_readiness(app_env.context, run, request["components"][0]) == "blocked"


def test_source_and_decisions_are_immutable_and_writer_fenced(app_env: Any, tmp_path: Path) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    _, inbox, run, token, _, _ = _runtime(app_env, tmp_path)
    request = inbox.submit(app_env.context, run, request_id="source", prompt="FYI, Linux")
    component = request["components"][0]
    with pytest.raises(StateMismatchError):
        inbox.resolve_component(
            app_env.context, run, uuid4(), request, component["id"], {"status": "recorded"}
        )
    with pytest.raises(InvalidInputError):
        inbox.resolve(app_env.context, run, request, {"decision": "no_change"})
    result = inbox.resolve_component(
        app_env.context, run, token, request, component["id"], {"status": "recorded"}
    )
    assert (
        inbox.resolve_component(
            app_env.context, run, token, request, component["id"], {"status": "recorded"}
        )
        == result
    )
    with pytest.raises(StateMismatchError):
        inbox.resolve_component(
            app_env.context, run, token, request, component["id"], {"status": "applied"}
        )
    with pytest.raises(psycopg.Error, match="source is immutable"):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE reasoning_runtime_steering SET components=%s WHERE workspace_id=%s AND run_id=%s",
                (Jsonb([]), app_env.context.workspace_id, UUID(hex=run)),
            )
    with pytest.raises(psycopg.Error, match="decisions are immutable"):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE reasoning_runtime_steering SET component_results='{}'::jsonb WHERE workspace_id=%s AND run_id=%s",
                (app_env.context.workspace_id, UUID(hex=run)),
            )


def test_concurrent_intake_serializes_before_later_dispatch(app_env: Any, tmp_path: Path) -> None:
    store, inbox, run, token, tree, _ = _runtime(app_env, tmp_path)
    node = _claim(tree)
    committed = Event()

    def intake() -> None:
        inbox.submit(app_env.context, run, request_id="stop", prompt="stop now")
        committed.set()

    def worker() -> None:
        assert committed.wait(5)
        with pytest.raises(ReasoningDispatchInterrupted):
            store.begin_dispatch(
                app_env.context, run, token, node_id=node.id, kind="model", context_epoch=0
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(intake), pool.submit(worker)
        first.result(timeout=10)
        second.result(timeout=10)
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS count FROM reasoning_runtime_dispatches WHERE workspace_id=%s AND run_id=%s",
                (app_env.context.workspace_id, UUID(hex=run)),
            ).fetchone()["count"]
            == 0
        )


def _provider_reply(system: str, prompt: str) -> str:
    packet = json.loads(prompt.split("JSON data):\n", 1)[1])
    if "'hypotheses'" in system:
        return json.dumps(
            {
                "hypotheses": [
                    {"hypothesis": "Use a bounded index", "assumptions": [], "questions": []}
                ]
            }
        )
    if "'decision'" in system:
        return json.dumps(
            {
                "decision": "no_change",
                "explanation": "The bounded goal already covers this",
                "branch_id": None,
                "hypothesis": None,
            }
        )
    if "'evaluations'" in system:
        return json.dumps(
            {
                "evaluations": [
                    {
                        "node_id": b["node_id"],
                        "relevance": 3,
                        "evidence_alignment": 2,
                        "falsification_risk": 1,
                        "uncertainty": 2,
                        "recommendation": "keep",
                        "rationale": "Fixture assessment",
                        "issues": [],
                        "tests": [],
                    }
                    for b in packet["context"]["branches"]
                ]
            }
        )
    return "A bounded answer with the stated constraints."


def test_runner_observes_background_and_deferred_dependency_end_to_end(
    app_env: Any, tmp_path: Path
) -> None:
    store, inbox = PostgresReasoningStore(app_env.db), PostgresSteeringInbox(app_env.db)
    runner = ReasoningInvestigator(
        root=tmp_path / "runner", state_store=store, steering_inbox=inbox
    )
    run = uuid4().hex
    packets: list[dict[str, Any]] = []

    def invoke(model, system, prompt, options, on_text, run_id, cancelled):
        packet = json.loads(prompt.split("JSON data):\n", 1)[1])
        packets.append(packet)
        if len(packets) == 1:
            inbox.submit(
                app_env.context,
                run,
                request_id="mixed",
                prompt="Add edge-case handling; FYI, only 2GB RAM; Next task: build a dashboard",
            )
        return (
            _provider_reply(system, prompt),
            {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            "stop",
        )

    result = runner.run(
        context=app_env.context,
        run_id=run,
        task="Index records",
        system="Stay bounded",
        models=[{"provider": "openai", "model": "fixture"}],
        options={"max_output_tokens": 400},
        invoke=invoke,
        on_text=lambda x: None,
        emit=lambda *a, **k: None,
    )
    assert store.inspect(app_env.context, run)["state"] == "completed"
    assert any(p["context"].get("user_context_revisions") for p in packets[1:])
    assert any(p["context"].get("steering_prompt") == "Add edge-case handling" for p in packets)
    assert not any(
        p["context"].get("steering_prompt") == "Next task: build a dashboard" for p in packets
    )
    tree = ReasoningTree.load(result.tree_path)
    assert tree.context_revisions[0]["text"] == "FYI, only 2GB RAM"
    request = inbox.get(app_env.context, run, "mixed")
    assert len(request["component_results"]) == 2
    assert (
        inbox.component_readiness(app_env.context, run, request["components"][2])
        == "ready_for_new_goal"
    )


@pytest.mark.parametrize("transport_raises", [False, True])
def test_runner_stop_during_call_never_dispatches_another_model(
    app_env: Any, tmp_path: Path, transport_raises: bool
) -> None:
    store, inbox = PostgresReasoningStore(app_env.db), PostgresSteeringInbox(app_env.db)
    runner = ReasoningInvestigator(
        root=tmp_path / "runner", state_store=store, steering_inbox=inbox
    )
    run, calls = uuid4().hex, []

    def invoke(model, system, prompt, options, on_text, run_id, cancelled):
        calls.append(run_id)
        inbox.submit(
            app_env.context, run, request_id="stop", prompt="stop now; add tests; FYI, Linux"
        )
        assert cancelled()
        if transport_raises:
            raise OSError("transport cancellation without a terminal receipt")
        return (
            _provider_reply(system, prompt),
            {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            "stop",
        )

    with pytest.raises(ReasoningDispatchInterrupted):
        runner.run(
            context=app_env.context,
            run_id=run,
            task="Index records",
            system="Stay bounded",
            models=[{"provider": "openai", "model": "fixture"}],
            options={"max_output_tokens": 400},
            invoke=invoke,
            on_text=lambda x: None,
            emit=lambda *a, **k: None,
        )
    assert len(calls) == 1
    state = store.inspect(app_env.context, run)
    assert state["state"] == "interrupted"
    assert len(state["snapshot"]["context_revisions"]) == 1
    request = inbox.get(app_env.context, run, "stop")
    assert request["components"][1]["id"] not in request["component_results"]
    with app_env.db.transaction(app_env.context) as conn:
        statuses = [
            r["status"]
            for r in conn.execute(
                "SELECT status FROM reasoning_runtime_dispatches WHERE workspace_id=%s AND run_id=%s",
                (app_env.context.workspace_id, UUID(hex=run)),
            ).fetchall()
        ]
    assert statuses == ["indeterminate" if transport_raises else "completed"]


def test_runner_recompiles_raced_background_before_first_send(app_env: Any, tmp_path: Path) -> None:
    store, inbox = PostgresReasoningStore(app_env.db), PostgresSteeringInbox(app_env.db)
    runner = ReasoningInvestigator(
        root=tmp_path / "runner", state_store=store, steering_inbox=inbox
    )
    run, packets = uuid4().hex, []
    submitted = False

    def emit(event, **values):
        nonlocal submitted
        if event == "reasoning.node.started" and not submitted:
            submitted = True
            inbox.submit(app_env.context, run, request_id="race", prompt="FYI, only 2GB RAM")

    def invoke(model, system, prompt, options, on_text, run_id, cancelled):
        packet = json.loads(prompt.split("JSON data):\n", 1)[1])
        packets.append(packet)
        assert packet["context"]["context_epoch"] == 1
        assert packet["context"]["user_context_revisions"][0]["text"] == "FYI, only 2GB RAM"
        return (
            _provider_reply(system, prompt),
            {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            "stop",
        )

    result = runner.run(
        context=app_env.context,
        run_id=run,
        task="Index records",
        system="Stay bounded",
        models=[{"provider": "openai", "model": "fixture"}],
        options={"max_output_tokens": 400},
        invoke=invoke,
        on_text=lambda x: None,
        emit=emit,
    )
    tree = ReasoningTree.load(result.tree_path)
    assert any(
        n.state == "rejected" and n.rejection_reason == "context changed before dispatch"
        for n in tree.nodes.values()
    )
    with app_env.db.transaction(app_env.context) as conn:
        rows = conn.execute(
            "SELECT context_epoch FROM reasoning_runtime_dispatches WHERE workspace_id=%s AND run_id=%s",
            (app_env.context.workspace_id, UUID(hex=run)),
        ).fetchall()
    assert len(rows) == len(packets) and all(r["context_epoch"] == 1 for r in rows)


def test_urgent_intake_at_evidence_boundary_prevents_external_search(
    app_env: Any, tmp_path: Path
) -> None:
    from potbot.controller.reasoning_investigation import EvidenceGateway, RegisteredEvidenceTool

    store, inbox = PostgresReasoningStore(app_env.db), PostgresSteeringInbox(app_env.db)
    effects = []
    gateway = EvidenceGateway(
        (
            RegisteredEvidenceTool(
                "fixture",
                "external_libraries",
                "syn://type/test",
                lambda **kw: effects.append(kw) or (),
            ),
        )
    )
    runner = ReasoningInvestigator(
        root=tmp_path / "runner", state_store=store, steering_inbox=inbox, gateway=gateway
    )
    run = uuid4().hex

    def invoke(model, system, prompt, options, on_text, run_id, cancelled):
        if "'hypotheses'" in system:
            result = json.dumps(
                {
                    "hypotheses": [
                        {
                            "hypothesis": "Use a bounded index",
                            "assumptions": [],
                            "questions": [
                                {"channel": "external_libraries", "query": "bounded index"}
                            ],
                        }
                    ]
                }
            )
        else:
            result = _provider_reply(system, prompt)
        return result, {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}, "stop"

    def emit(event, **values):
        if event == "reasoning.evidence.started":
            inbox.submit(app_env.context, run, request_id="stop-search", prompt="stop now")

    with pytest.raises(ReasoningDispatchInterrupted):
        runner.run(
            context=app_env.context,
            run_id=run,
            task="Index records",
            system="Stay bounded",
            models=[{"provider": "openai", "model": "fixture"}],
            options={"max_output_tokens": 400},
            invoke=invoke,
            on_text=lambda x: None,
            emit=emit,
        )
    assert effects == []
    assert store.inspect(app_env.context, run)["state"] == "interrupted"


@pytest.mark.parametrize("before_send", [False, True])
def test_task_budget_distinguishes_unsent_stop_from_uncertain_paid_call(
    app_env: Any, tmp_path: Path, before_send: bool
) -> None:
    from potbot.controller.budgets import BudgetLedger
    from potbot.controller.reasoning_budget import ReasoningBudgetJournal
    from tests.integration.test_reasoning_budget_postgres import _journal, _task

    task = _task(app_env)
    store, inbox = PostgresReasoningStore(app_env.db), PostgresSteeringInbox(app_env.db)
    run, effects = uuid4().hex, []

    class Journal(ReasoningBudgetJournal):
        def dispatch(self, context, ticket, lease_token):
            if before_send:
                inbox.submit(context, run, request_id="stop", prompt="stop now")
            super().dispatch(context, ticket, lease_token)

    journal = Journal(app_env.db, _journal(app_env).policy)
    runner = ReasoningInvestigator(
        root=tmp_path / "runner", state_store=store, steering_inbox=inbox, budget_journal=journal
    )

    def invoke(model, system, prompt, options, on_text, run_id, cancelled):
        effects.append(run_id)
        inbox.submit(app_env.context, run, request_id="stop", prompt="stop now")
        assert cancelled()
        raise OSError("no terminal provider receipt")

    with pytest.raises(ReasoningDispatchInterrupted):
        runner.run(
            context=app_env.context,
            run_id=run,
            task="Index records",
            system="Stay bounded",
            models=[{"provider": "openai", "model": "reasoner-test", "protocol": "responses"}],
            options={"max_output_tokens": 400, "potbot_task_id": str(task)},
            invoke=invoke,
            on_text=lambda x: None,
            emit=lambda *a, **k: None,
        )
    rows = journal.list_for_run(app_env.context, run)
    assert len(rows) == 1
    assert rows[0]["status"] == ("released" if before_send else "indeterminate")
    assert len(effects) == (0 if before_send else 1)
    budget = BudgetLedger(app_env.db).inspect(app_env.context, task)
    if before_send:
        assert budget["held"] == {}
    else:
        assert budget["held"]["calls"] == 1
    assert budget["used"].get("calls", 0) == 0


def test_background_component_capacity_cannot_suppress_emergency_stop(
    app_env: Any, tmp_path: Path
) -> None:
    store, inbox, run, token, tree, _ = _runtime(app_env, tmp_path)
    node = _claim(tree)
    for index in range(8):
        inbox.submit(
            app_env.context,
            run,
            request_id=f"facts-{index}",
            prompt="; ".join(f"FYI, fact {index}-{part}" for part in range(16)),
        )
    with pytest.raises(InvalidInputError, match="context capacity"):
        inbox.submit(app_env.context, run, request_id="overflow", prompt="FYI, another fact")
    receipt = inbox.submit(
        app_env.context, run, request_id="urgent", prompt="stop now; FYI, another fact"
    )
    assert receipt["acknowledgement"]["dispatch_fenced"]
    assert receipt["acknowledgement"]["blocked_components"] == 1
    assert receipt["component_results"][receipt["components"][1]["id"]]["status"] == "blocked"
    assert store.control_state(app_env.context, run)["context_epoch"] == 8
    with pytest.raises(ReasoningDispatchInterrupted):
        store.begin_dispatch(
            app_env.context, run, token, node_id=node.id, kind="model", context_epoch=8
        )


def test_material_context_arriving_during_generation_blocks_its_old_queries(
    app_env: Any, tmp_path: Path
) -> None:
    from potbot.controller.reasoning_investigation import EvidenceGateway, RegisteredEvidenceTool

    store, inbox = PostgresReasoningStore(app_env.db), PostgresSteeringInbox(app_env.db)
    effects = []
    gateway = EvidenceGateway(
        (
            RegisteredEvidenceTool(
                "fixture",
                "external_libraries",
                "syn://type/test",
                lambda **kw: effects.append(kw) or (),
            ),
        )
    )
    runner = ReasoningInvestigator(
        root=tmp_path / "runner", state_store=store, steering_inbox=inbox, gateway=gateway
    )
    run, generations = uuid4().hex, 0

    def invoke(model, system, prompt, options, on_text, run_id, cancelled):
        nonlocal generations
        if "'hypotheses'" in system:
            generations += 1
            if generations == 1:
                inbox.submit(
                    app_env.context, run, request_id="late-fact", prompt="FYI, only 2GB RAM"
                )
                result = json.dumps(
                    {
                        "hypotheses": [
                            {
                                "hypothesis": "Load the complete dataset",
                                "assumptions": [],
                                "questions": [
                                    {"channel": "external_libraries", "query": "large index"}
                                ],
                            }
                        ]
                    }
                )
            else:
                result = _provider_reply(system, prompt)
        else:
            result = _provider_reply(system, prompt)
        return result, {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}, "stop"

    result = runner.run(
        context=app_env.context,
        run_id=run,
        task="Index records",
        system="Stay bounded",
        models=[{"provider": "openai", "model": "fixture"}],
        options={"max_output_tokens": 400},
        invoke=invoke,
        on_text=lambda x: None,
        emit=lambda *a, **k: None,
    )
    assert effects == []
    tree = ReasoningTree.load(result.tree_path)
    stale = next(n for n in tree.nodes.values() if n.hypothesis == "Load the complete dataset")
    assert "context_reassessment_required" in stale.result
