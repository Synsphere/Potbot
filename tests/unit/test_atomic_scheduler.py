"""R7.2 admission and ready-set decisions remain non-executing."""

from __future__ import annotations

import pytest

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.controller.atomic_scheduler import (
    AtomicAdmissionPlanner,
    ProposedLeaf,
    RegisteredLeaf,
)
from potbot.controller.atomic_tasks import AtomicOperation, TaskGraph
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.syn import SynRef


def _binding(name: str = "run_registered_tool", *, locks: tuple[str, ...] = ()) -> RegisteredLeaf:
    return RegisteredLeaf(
        AtomicOperation(name, "execution", 1024, 1000),
        CogSignature(
            SynRef(f"syn://cog/potbot-atomic-{name.replace('_', '-')}-v1"),
            "syn://type/potbot-atomic-cog",
            "syn://type/potbot-atomic-request",
            "syn://type/potbot-atomic-response",
            "potbot.cogs.execution.atomic",
            1,
        ),
        locks,
    )


def _proposal(task_id: str, name: str = "run_registered_tool") -> ProposedLeaf:
    return ProposedLeaf(
        task_id,
        name,
        TypedCogValue("syn://type/potbot-atomic-request", {"task": task_id}),
    )


def _graph() -> TaskGraph:
    graph = TaskGraph("bounded atomic work")
    graph.decompose(
        "root",
        (
            ("first", "Independent first result", ()),
            ("second", "Independent second result", ()),
            ("third", "Depends on first", ("first",)),
        ),
    )
    return graph


def test_admission_is_atomic_on_invalid_later_proposal() -> None:
    graph = _graph()
    planner = AtomicAdmissionPlanner(graph, {"run_registered_tool": _binding()})
    with pytest.raises(InvalidInputError):
        planner.admit((_proposal("first"), _proposal("missing")))
    assert graph.unresolved() == ("first", "second", "third")
    assert planner.ready(completed=frozenset()) == ()


def test_dependency_and_exclusive_locks_are_deterministic() -> None:
    graph = _graph()
    planner = AtomicAdmissionPlanner(
        graph, {"run_registered_tool": _binding(locks=("repo:abc",))}, max_parallel=2
    )
    planner.admit((_proposal("first"), _proposal("second"), _proposal("third")))
    assert tuple(c.task_id for c in planner.ready(completed=frozenset())) == ("first",)
    assert planner.ready(completed=frozenset(), in_flight=frozenset({"first"})) == ()
    assert tuple(c.task_id for c in planner.ready(completed=frozenset({"first"}))) == ("second",)
    assert planner.ready(completed=frozenset({"first"}), interrupted=True) == ()
    assert (
        planner.ready(completed=frozenset({"first", "second"}), held_locks=frozenset({"repo:abc"}))
        == ()
    )
    assert tuple(c.task_id for c in planner.ready(completed=frozenset({"first", "second"}))) == (
        "third",
    )


def test_no_double_admission_or_untracked_flight() -> None:
    graph = _graph()
    planner = AtomicAdmissionPlanner(graph, {"run_registered_tool": _binding()}, max_parallel=1)
    planner.admit((_proposal("first"), _proposal("second")))
    assert tuple(c.task_id for c in planner.ready(completed=frozenset())) == ("first",)
    assert planner.ready(completed=frozenset(), in_flight=frozenset({"first"})) == ()
    with pytest.raises(InvalidInputError):
        planner.admit((_proposal("first"),))
    with pytest.raises(StateMismatchError):
        planner.ready(completed=frozenset(), in_flight=frozenset({"third"}))


def test_typed_cog_input_is_enforced() -> None:
    planner = AtomicAdmissionPlanner(_graph(), {"run_registered_tool": _binding()})
    with pytest.raises((InvalidInputError, StateMismatchError)):
        planner.admit(
            (
                ProposedLeaf(
                    "first", "run_registered_tool", TypedCogValue("syn://type/wrong-request", {})
                ),
            )
        )


def test_cannot_exceed_parallel_limit_even_without_locks() -> None:
    planner = AtomicAdmissionPlanner(_graph(), {"run_registered_tool": _binding()}, max_parallel=1)
    planner.admit((_proposal("first"), _proposal("second")))
    assert len(planner.ready(completed=frozenset())) == 1
    assert planner.ready(completed=frozenset(), in_flight=frozenset({"first"})) == ()


def test_model_cannot_claim_approval_by_itself() -> None:
    binding = RegisteredLeaf(
        AtomicOperation("request_human_approval", "steering", 1024, 1000, True),
        CogSignature(
            SynRef("syn://cog/potbot-atomic-approval-v1"),
            "syn://type/potbot-atomic-cog",
            "syn://type/potbot-atomic-request",
            "syn://type/potbot-atomic-response",
            "potbot.cogs.execution.atomic",
            1,
        ),
    )
    graph = _graph()
    planner = AtomicAdmissionPlanner(graph, {"request_human_approval": binding})
    proposal = _proposal("first", "request_human_approval")
    with pytest.raises(InvalidInputError):
        planner.admit((proposal,))
    assert graph.nodes["first"].state == "needs_decomposition"
    planner.admit((proposal,), approved_nodes=frozenset({"first"}))
    assert tuple(c.task_id for c in planner.ready(completed=frozenset())) == ("first",)
