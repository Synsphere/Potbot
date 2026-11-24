"""R7.2-B checked operation bindings and non-executing admission guards."""

from __future__ import annotations

from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.controller.atomic_graph_runtime import AtomicGraphService, AtomicLaunchBinding
from potbot.controller.atomic_scheduler import AtomicAdmissionPlanner, ProposedLeaf, RegisteredLeaf
from potbot.controller.atomic_tasks import AtomicOperation, TaskGraph
from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import SynRef
from potbot.persistence.schema import application_migrations

_REF = "syn://cog/potbot-atomic-run-registered-tool-v1"
_TYPE = "syn://type/potbot-atomic-request"


def _build(*, runtime_ms: int = 1000):
    graph = TaskGraph("Atomic test")
    graph.decompose("root", (("a", "First leaf", ()), ("b", "Next leaf", ("a",))))
    registry = {
        "run_registered_tool": RegisteredLeaf(
            AtomicOperation("run_registered_tool", "execution", 1024, runtime_ms),
            CogSignature(
                SynRef(_REF),
                "syn://type/potbot-atomic-cog",
                _TYPE,
                "syn://type/potbot-atomic-response",
                "potbot.cogs.execution.atomic",
                1,
            ),
        )
    }
    planner = AtomicAdmissionPlanner(graph, registry)
    planner.admit(
        tuple(
            ProposedLeaf(node, "run_registered_tool", TypedCogValue(_TYPE, {"task": node}))
            for node in ("a", "b")
        )
    )
    episode = uuid4()
    bindings = {}
    for node in ("a", "b"):
        request = {
            "job_type": "cog.execute",
            "episode_id": str(episode),
            "input": {
                "agent_id": str(uuid4()),
                "repository_state_id": str(uuid4()),
                "operation_ref": _REF.replace("syn://cog/", "syn://thing/"),
                "profile_sha256": "f" * 64,
                "type_ref": _TYPE,
                "value": {"task": node},
            },
            "selected_models": [],
        }
        bindings[node] = AtomicLaunchBinding("a" * 64, uuid4(), request)
    return planner, bindings


def test_manifest_retains_exact_owner_checked_bindings_without_dispatch() -> None:
    planner, bindings = _build()
    document, leaves = AtomicGraphService._manifest(planner, bindings)
    assert document["schema"] == "potbot-atomic-graph/v1"
    assert len(leaves) == 2
    assert leaves[1]["dependencies"] == ["a"]
    assert leaves[0]["cog_ref"] == _REF
    assert leaves[0]["input_fingerprint"] == TypedCogValue(_TYPE, {"task": "a"}).fingerprint


def test_mismatched_cog_input_or_duplicate_check_never_gets_manifest() -> None:
    planner, bindings = _build()
    wrong = bindings["a"]
    req = {**wrong.request, "input": {**wrong.request["input"], "value": {"task": "other"}}}
    with pytest.raises(InvalidInputError):
        AtomicGraphService._manifest(
            planner,
            {**bindings, "a": AtomicLaunchBinding(wrong.operation_id, wrong.check_job_id, req)},
        )
    with pytest.raises(InvalidInputError):
        AtomicGraphService._manifest(
            planner,
            {
                **bindings,
                "b": AtomicLaunchBinding(
                    wrong.operation_id, wrong.check_job_id, bindings["b"].request
                ),
            },
        )


def test_oversized_native_wall_time_cannot_claim_handler_qualification() -> None:
    planner, bindings = _build(runtime_ms=350000)
    with pytest.raises(InvalidInputError):
        AtomicGraphService._manifest(planner, bindings)


def test_bindings_are_copied_and_cannot_use_model_job_route() -> None:
    planner, bindings = _build()
    request = bindings["a"].request
    copy = AtomicLaunchBinding("a" * 64, uuid4(), request)
    request["input"]["value"] = {"task": "changed"}
    assert copy.request["input"]["value"] == {"task": "a"}
    with pytest.raises(InvalidInputError):
        AtomicLaunchBinding(
            "a" * 64,
            uuid4(),
            {
                "job_type": "model.generate",
                "episode_id": str(uuid4()),
                "input": {"prompt": "hi"},
                "selected_models": [{"provider": "openai", "model": "test", "role": "reasoning"}],
            },
        )


def test_migration_is_scoped_and_hooks_checked_queue_and_dispatch_fence() -> None:
    migration = next(m for m in application_migrations() if m.version == 49)
    assert migration.version == 49 and migration.name == "atomic_graph_schema"
    assert migration.workspace_tables == (
        "atomic_graph_runs",
        "atomic_graph_nodes",
        "atomic_graph_resource_holds",
    )
    assert migration.append_only_tables == ()
    scripts = "\n".join(migration.statements)
    for required in (
        "CREATE TRIGGER atomic_graph_job_admission AFTER INSERT ON jobs",
        "CREATE TRIGGER atomic_graph_dispatch_fence BEFORE UPDATE OF dispatched_at ON jobs",
        "FOR UPDATE",
        "FOR SHARE OF g",
        "n.lock_keys && leaf.lock_keys",
        "atomic_active_resource_hold",
        "j.state IS DISTINCT FROM 'succeeded'",
        "principal_id",
    ):
        assert required in scripts
