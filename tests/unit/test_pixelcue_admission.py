"""Real controller construction with explicit storage/sandbox doubles, not host qualification."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import TypedCogValue
from potbot.cogs.execution import execution_cog_snapshot
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.tools.execution_contracts import ExecutionPolicy
from potbot.tools.pixelcue_cog import install_pixelcue, require_pixelcue
from potbot.tools.pixelcue_worker import OPERATION, REQUEST, RESULT
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor


def test_registry_seals_the_optional_profile_and_rejects_profile_mixing():
    registry = BoundCogRegistry.provenance(pixelcue=True)
    assert registry.pixelcue_profile is True
    assert {REQUEST, RESULT} <= {str(item.ref) for item in registry.snapshot.types}
    assert len(registry.installed()) == len(BoundCogRegistry.provenance().installed())
    with pytest.raises(ScopeDeniedError, match="one exact execution transport"):
        BoundCogRegistry.provenance(pixelcue=True, python_profile=True)


def test_controller_automatically_selects_and_binds_the_pixelcue_profile(monkeypatch):
    from potbot.semantics.ember_service import EmberContractService
    from potbot.semantics.service import SynService
    from potbot.tools.repository import RepositoryStateStore

    policy = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "fixture-v1")
    operation = install_pixelcue("d" * 64, policy)
    snapshot = execution_cog_snapshot(pixelcue=True)
    actor = WorkspaceContext(uuid4(), uuid4())
    calls, bundles = [], []

    class Database:
        @contextmanager
        def transaction(self, selected):
            assert selected == actor
            yield None

    database = Database()
    artifacts = SimpleNamespace(
        read=lambda *_: canonical_json({"source": snapshot.to_dict()}).encode()
    )
    monkeypatch.setattr(
        SynService,
        "_snapshot",
        staticmethod(
            lambda *_: {
                "artifact_id": uuid4(),
                "source_hash": snapshot.fingerprint,
                "namespace": "fixture",
            }
        ),
    )
    monkeypatch.setattr(
        RepositoryStateStore, "get", lambda *_: {"content_digest": policy.source_digest}
    )
    monkeypatch.setattr(
        EmberContractService,
        "import_bundle",
        lambda _self, _actor, _snapshot, bundle: bundles.append(bundle),
    )
    monkeypatch.setattr(
        SandboxedCogExecutor, "bind_context", lambda _self, *args: calls.append(args)
    )
    executor = object.__new__(SandboxedCogExecutor)
    runtime = ExecutionCogRuntime(
        database=database,
        artifacts=artifacts,
        actor=actor,
        snapshot_id=uuid4(),
        task_revision_id=uuid4(),
        repository_state_id=uuid4(),
        emitter_run_id=uuid4(),
        policy=policy,
        operations=(operation,),
        admit=lambda *_: None,
        sandbox_executor=executor,
        sandbox_agent_id=uuid4(),
    )
    assert runtime.registry.pixelcue_profile is True
    assert runtime.registry.snapshot.fingerprint == snapshot.fingerprint
    assert runtime.operations[("tool", OPERATION)] == operation
    assert len(runtime.catalog.schema_hash(REQUEST)) == 64
    assert len(runtime.catalog.schema_hash(RESULT)) == 64
    runtime.catalog.validate(
        TypedCogValue(
            REQUEST, {"path": "media/page.png", "models": ["ocr"], "max_frames": 3, "max_edge": 640}
        )
    )
    assert calls == [(database, actor, (operation,))]
    assert len(bundles) == 1
    with pytest.raises(InvalidInputError, match="confined analysis scope"):
        runtime.catalog.validate(
            TypedCogValue(
                REQUEST, {"path": "../private.png", "models": [], "max_frames": 1, "max_edge": 640}
            )
        )


@pytest.mark.parametrize("change", ["argv", "output", "effect"])
def test_installed_worker_cannot_drift_or_claim_a_different_authority(change):
    policy = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "fixture-v1")
    operation = install_pixelcue("d" * 64, policy)
    if change == "argv":
        operation = replace(operation, argv=(operation.argv[0], "/models/changed"))
    elif change == "output":
        operation = replace(operation, binding=replace(operation.binding, output_type_ref=REQUEST))
    else:
        operation = replace(
            operation, binding=replace(operation.binding, effect_class="idempotent_write")
        )
    with pytest.raises(ScopeDeniedError, match="exact installed worker"):
        require_pixelcue(operation)
