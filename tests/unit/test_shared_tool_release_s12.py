"""Real host swap/denial projections; these do not claim live-host qualification."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.tools.shared_tool_release_markers import (
    DIRECTORY,
    ToolMaintenanceProbe,
    blocked_directories,
    read_hold,
)
from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource
from tests.unit.test_shared_tool_upgrades_s9 import (
    activate,
    prepare,
    reconcile_uncertain,
    rollback,
    stage_name,
)
from tests.unit.test_shared_tool_upgrades_s9 import (
    rig as rig,
)
from tests.unit.test_shared_workspace_isolation import (
    seccomp_descriptors as seccomp_descriptors,
)


def test_direct_activation_and_rollback_cannot_release_changed_tools(rig):
    runtime, manager = rig["runtime"], rig["manager"]
    stage = stage_name("stemlab", rig["operation_id"])
    (rig["tools"] / "unrelated").mkdir()
    assert blocked_directories(runtime) == (stage,)
    prepare(rig)
    activate(rig)
    hold = read_hold(rig["tools"], "stemlab")
    assert hold["commit"] == rig["new"].commit
    assert hold["direction"] == "activate"
    # Even independently matching executable pins cannot substitute for a PG release.
    loaded = replace(runtime, tool_entrypoints=tuple(hold["entrypoints"].items()))
    assert set(blocked_directories(loaded)) == {stage, DIRECTORY, "stemlab"}
    assert "unrelated" not in blocked_directories(loaded)
    # A fresh runtime after restart retains the conservative hold.
    assert "stemlab" in blocked_directories(replace(loaded))
    rollback(rig)
    restored = read_hold(rig["tools"], "stemlab")
    assert restored["direction"] == "rollback"
    assert restored["commit"] == rig["old"].commit
    assert restored != hold
    assert (
        manager.inspect(
            workspace_id=rig["workspace_id"],
            agent_id=rig["agent_id"],
            operation_id=rig["operation_id"],
        )["state"]
        == "rolled_back"
    )
    assert "stemlab" in blocked_directories(loaded)
    proposal = manager.runtime_entrypoint_updates(
        workspace_id=rig["workspace_id"],
        agent_id=rig["agent_id"],
        operation_id=rig["operation_id"],
    )
    assert proposal["candidate_commit"] == rig["old"].commit
    assert proposal["pins"] == restored["entrypoints"]
    assert proposal["requires_operator_runtime_repin"] is True


def test_hold_is_durable_before_filesystem_swap(rig, monkeypatch):
    prepare(rig)
    from potbot.tools import shared_tool_upgrades

    original = shared_tool_upgrades._exchange
    observed = []

    def exchange(root, left, right):
        observed.append(read_hold(root, left))
        assert observed[-1]["commit"] == rig["new"].commit
        assert "stemlab" in blocked_directories(rig["runtime"])
        return original(root, left, right)

    monkeypatch.setattr(shared_tool_upgrades, "_exchange", exchange)
    activate(rig)
    assert len(observed) == 1


def test_interrupted_swap_does_not_release_or_blindly_retry(rig, monkeypatch):
    prepare(rig)
    original = rig["manager"]._commit_swap
    monkeypatch.setattr(
        rig["manager"], "_commit_swap", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        activate(rig)
    assert "stemlab" in blocked_directories(rig["runtime"])
    with pytest.raises(StateMismatchError):
        activate(rig)
    reconcile_uncertain(rig)
    monkeypatch.setattr(rig["manager"], "_commit_swap", original)
    result = rig["manager"].reconcile(
        workspace_id=rig["workspace_id"],
        agent_id=rig["agent_id"],
        operation_id=rig["operation_id"],
        slug="stemlab",
        confirm=True,
    )
    assert result["state"] == "aborted"
    assert read_hold(rig["tools"], "stemlab") is None
    assert "stemlab" not in blocked_directories(rig["runtime"])
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"


def test_maintenance_probe_is_one_tool_one_binding_one_live_lease(rig):
    prepare(rig)
    activate(rig)
    hold = read_hold(rig["tools"], "stemlab")
    manager = rig["manager"]
    guard = manager._guard(
        rig["workspace_id"], rig["agent_id"], "stemlab", uuid4().hex, "tool.postflight"
    )
    probe = ToolMaintenanceProbe(guard, "stemlab", content_hash(hold))
    runtime = replace(rig["runtime"], tool_maintenance_probe=probe)
    try:
        assert "stemlab" not in blocked_directories(runtime)
        assert stage_name("stemlab", rig["operation_id"]) in blocked_directories(runtime)
        assert DIRECTORY in blocked_directories(runtime)
    finally:
        guard.finish(evidence_sha256="a" * 64)
    with pytest.raises(StateMismatchError):
        blocked_directories(runtime)


def test_missing_authority_or_corrupt_projection_cannot_grant_release(rig):
    prepare(rig)
    activate(rig)
    with pytest.raises(ScopeDeniedError):
        blocked_directories(replace(rig["runtime"], tool_release_authority=lambda *_: True))
    hold = rig["tools"] / DIRECTORY / "stemlab.json"
    hold.write_text('{"state":"released"}')
    with pytest.raises(StateMismatchError):
        blocked_directories(rig["runtime"])


def test_journal_and_hold_recovery_are_atomic_before_marker_rename(rig, monkeypatch):
    from potbot.tools import shared_tool_upgrades

    prepare(rig)
    original = shared_tool_upgrades.write_hold
    monkeypatch.setattr(
        shared_tool_upgrades,
        "write_hold",
        lambda *args: (_ for _ in ()).throw(OSError("before marker rename")),
    )
    with pytest.raises(OSError, match="before marker"):
        activate(rig)
    with rig["ledger"]._connect() as db:
        assert db.execute("SELECT state FROM trusted_tool_upgrades").fetchone()[0] == "switching"
        assert (
            db.execute("SELECT count(*) FROM trusted_tool_release_projections").fetchone()[0] == 1
        )
    assert read_hold(rig["tools"], "stemlab") is None
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"
    reconcile_uncertain(rig)
    monkeypatch.setattr(shared_tool_upgrades, "write_hold", original)
    assert (
        rig["manager"].reconcile(
            workspace_id=rig["workspace_id"],
            agent_id=rig["agent_id"],
            operation_id=rig["operation_id"],
            slug="stemlab",
            confirm=True,
        )["state"]
        == "aborted"
    )


def test_whole_tree_readers_block_other_tool_writers_without_serializing_reads(rig):
    store, ledger = rig["store"], rig["ledger"]
    with store.use(
        workspace_id=rig["workspace_id"],
        agent_id=rig["agent_id"],
        slug="stemlab",
        idempotency_key="reader-one",
    ):
        with store.use(
            workspace_id=rig["workspace_id"],
            agent_id=rig["agent_id"],
            slug="stemlab",
            idempotency_key="reader-two",
        ):
            assert len(ledger.active(rig["workspace_id"])) == 2
        with pytest.raises(StateMismatchError, match="conflicting"):
            ledger.claim(
                AccessRequest(
                    rig["workspace_id"],
                    rig["agent_id"],
                    "tool.upgrade",
                    "different-tool",
                    "write",
                    (AccessResource("shared_tool", "another", "write"),),
                    rig["runtime"].image_digest,
                )
            )


def test_image_interpreter_sees_hold_mask_and_write_grant_cannot_overlay_it(
    rig, tmp_path, monkeypatch, seccomp_descriptors
):
    from potbot.tools.shared_tool_mounts import ToolWriteGrant

    prepare(rig)
    activate(rig)
    runtime = rig["runtime"]
    for relative in (
        "bin",
        "home",
        "proc",
        "dev",
        "var/ai",
        "opt/potbot/models",
        "usr/local/tools",
    ):
        (runtime.rootfs / relative).mkdir(parents=True, exist_ok=True)
    (runtime.rootfs / "bin/python3").write_bytes(b"fixture-only")
    (runtime.rootfs / "tmp").symlink_to("/home/.tmp")
    (runtime.home / ".tmp").mkdir(exist_ok=True)
    models, staging = tmp_path / "models", tmp_path / "staging"
    models.mkdir()
    staging.mkdir()
    runtime = replace(runtime, shared_models=models, model_staging=staging)
    for name in ("bwrap", "prlimit"):
        path = tmp_path / name
        path.write_bytes(b"fixture-only")
        path.chmod(0o755)
    agent = rig["homes"].lookup(rig["workspace_id"], rig["agent_id"])
    assert agent is not None
    (rig["tools"] / "stemlab").chmod(0o2775)
    monkeypatch.setattr("potbot.tools.shared_workspace_isolation._seccomp_fd", seccomp_descriptors)
    options = dict(
        argv=("/bin/python3", "/usr/local/tools/stemlab/checkout/main.py"),
        workspace_root=runtime.home,
        cwd=runtime.home / agent.directory,
        read_only_roots=(),
        writable_roots=(),
        sandbox_root=runtime.home,
        status_fd=64,
        memory_bytes=1_000_000_000,
        cpu_seconds=30,
        max_file_bytes=10_000_000,
        max_open_files=256,
        guest_uid=agent.uid,
        guest_gid=agent.gid,
    )
    backend = SharedWorkspaceBubblewrapBackend(
        runtime, executable=tmp_path / "bwrap", prlimit=tmp_path / "prlimit"
    )
    launch = backend.prepare(**options)
    assert ("--tmpfs", "/usr/local/tools/stemlab") in tuple(
        zip(launch.argv, launch.argv[1:], strict=False)
    )
    guard = rig["manager"]._guard(
        rig["workspace_id"], rig["agent_id"], "stemlab", uuid4().hex, "tool.install"
    )
    try:
        backend.tool_write_grant = ToolWriteGrant("stemlab", agent, guard)
        with pytest.raises(ScopeDeniedError, match="cannot bypass"):
            backend.prepare(**options)
    finally:
        guard.finish(evidence_sha256="f" * 64)
