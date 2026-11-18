"""R7.2-C scheduler cannot retry ambiguous effects or create authority."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.controller.atomic_graph_pump import AtomicGraphPump
from potbot.controller.atomic_graph_runtime import AtomicGraphService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)


class FakeGraphService(AtomicGraphService):
    def __init__(self, state="active", outcome="ready"):
        self.database = SimpleNamespace()
        self.state, self.outcome = state, outcome
        self.calls: list[tuple[str, int]] = []
        self.fault: type[Exception] | None = None

    def inspect(self, _actor, graph_id):
        return {"graph_id": str(graph_id), "state": self.state, "outcome": self.outcome}

    def launch_ready(self, _actor, graph_id, *, setup_token, limit):
        self.calls.append((setup_token, limit))
        if self.fault:
            raise self.fault("test failure")
        self.outcome = "running"
        return ({"node_id": "leaf", "job": {"id": str(uuid4())}},)


def actor():
    return WorkspaceContext(uuid4(), uuid4())


def test_tick_runs_only_admitted_ready_graph():
    current = FakeGraphService()
    graph_id = uuid4()
    result = AtomicGraphPump(current).tick_graph(actor(), graph_id, setup_token="test-secret", limit=3)
    assert result["decision"] == "launched"
    assert result["graph_id"] == str(graph_id)
    assert result["launched"][0]["node_id"] == "leaf"
    assert current.calls == [("test-secret", 3)]


@pytest.mark.parametrize("state,outcome", [
    ("interrupted", "interrupted"),
    ("active", "needs_reconciliation"),
    ("active", "blocked"),
    ("active", "succeeded"),
])
def test_terminal_graphs_never_dispatch(state, outcome):
    graph = FakeGraphService(state, outcome)
    result = AtomicGraphPump(graph).tick_graph(actor(), uuid4(), setup_token="test-secret")
    assert result["decision"] == "no_dispatch"
    assert graph.calls == []


def test_raced_checked_operation_reconciles_without_redispatch():
    graph = FakeGraphService()
    graph.fault = RevisionConflictError
    outcome = AtomicGraphPump(graph).tick_graph(actor(), uuid4(), setup_token="test-secret")
    assert outcome["decision"] == "reconciled_race"
    assert len(graph.calls) == 1


def test_expired_check_is_not_silently_refreshed():
    graph = FakeGraphService()
    graph.fault = ScopeDeniedError
    outcome = AtomicGraphPump(graph).tick_graph(actor(), uuid4(), setup_token="test-secret")
    assert outcome["decision"] == "admission_blocked"
    assert len(graph.calls) == 1


def test_indeterminate_commit_aborts_pass_without_an_automatic_retry():
    graph = FakeGraphService()
    graph.fault = IndeterminateExecutionError
    with pytest.raises(IndeterminateExecutionError):
        AtomicGraphPump(graph).tick_graph(actor(), uuid4(), setup_token="test-secret")
    assert len(graph.calls) == 1


def test_invalid_page_and_parallel_limits_fail_closed():
    graph = FakeGraphService()
    pump = AtomicGraphPump(graph)
    with pytest.raises(InvalidInputError):
        pump.tick_graph(actor(), uuid4(), setup_token="test-secret", limit=65)
    with pytest.raises(InvalidInputError):
        pump.pass_once(actor(), setup_token="test-secret", graphs=0)
