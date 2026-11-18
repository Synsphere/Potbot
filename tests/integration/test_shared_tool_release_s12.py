"""S12 real PostgreSQL/RLS and real Git swaps, with explicit process receipts.

The injected postflight executor is a fixture, not native Syn/TASKSTATS evidence.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api.ui_tool_upgrades import UiToolUpgradeStore, _approval_mac
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.persistence.database import (
    Database,
    admin_connection,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_tool_release_markers import blocked_directories, read_hold
from potbot.tools.shared_tool_store import SharedToolStore
from potbot.tools.shared_tool_upgrades import (
    SharedToolUpgradeManager,
    ToolTestCase,
    ToolTestSuite,
    stage_name,
)
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.tool_approval_worker_s11 import ToolApprovalWorker
from potbot.tools.tool_release_gate_s12 import bind_installed_tool_releases
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from tests.unit.test_shared_tool_upgrades_s9 import FixtureTestExecutor, install

pytestmark = pytest.mark.postgres


@pytest.fixture
def release_rig(app_env, tmp_path, request):
    actor, agent, operation = app_env.context, uuid4(), uuid4()
    rootfs, home, tools, private = [
        tmp_path / name for name in ("rootfs", "home", "tools", "private")
    ]
    for path in (rootfs, home, tools, private):
        path.mkdir(mode=0o700)
    ledger = WorkspaceLeaseLedger(private / "lease.sqlite", workspace_home=home)
    ledger.pin_image(
        actor.workspace_id,
        digest="a" * 64,
        image_ref="ghcr.io/fixture/image@sha256:" + "a" * 64,
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256="b" * 64,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="release-fixture")
    homes.provision(actor.workspace_id, agent)

    def authorized(w, a):
        return (w, a) == (actor.workspace_id, agent)

    store = SharedToolStore(ledger=ledger, shared_root=tools, authorize=authorized)
    old = (
        None
        if getattr(request, "param", None) == "first_install"
        else install(tools / "stemlab", "v1")
    )
    new = install(tools / stage_name("stemlab", operation), "v2")
    if old is not None:
        with ledger._connect() as conn:
            conn.execute(
                "INSERT INTO trusted_shared_tools VALUES(?,?,?)",
                (str(actor.workspace_id), "stemlab", json.dumps(old.to_dict(), sort_keys=True)),
            )
    runtime = SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64, shared_tools=tools)
    manager = SharedToolUpgradeManager(
        store=store,
        runtime=runtime,
        homes=homes,
        authorize=authorized,
        executor=FixtureTestExecutor(),
    )
    suite = ToolTestSuite(
        tuple(
            ToolTestCase(name, kind, ("/usr/bin/python3", "-c", "pass", "{checkout}"))
            for name, kind in (("smoke", "smoke"), ("unit", "unit"))
        )
    )
    manager.prepare(
        workspace_id=actor.workspace_id,
        agent_id=agent,
        operation_id=operation,
        expected_old_commit=old.commit if old is not None else None,
        replacement=new,
        suite=suite,
    )
    tested = manager.inspect(
        workspace_id=actor.workspace_id, agent_id=agent, operation_id=operation
    )
    approvals = UiToolUpgradeStore(app_env.db)
    approvals.publish_test(actor, agent_id=agent, tested=tested)
    approvals.decide(
        actor,
        operation,
        expected_revision=0,
        evidence_sha256=tested["test_evidence_sha256"],
        approve=True,
        setup_token="S" * 64,
    )
    worker = ToolApprovalWorker(
        approvals=approvals,
        manager=manager,
        context=actor,
        proposal_dir=private,
        agent_id=agent,
        setup_token="S" * 64,
    )
    worker.run_once()
    hold = read_hold(tools, "stemlab")
    manager.runtime = replace(runtime, tool_entrypoints=tuple(hold["entrypoints"].items()))
    authority = worker.release_authority()
    manager.runtime = replace(manager.runtime, tool_release_authority=authority)
    return SimpleNamespace(**locals())


class Postflight:
    def __init__(self, *, passed=True, stopped=True, during=None):
        self.passed, self.stopped, self.during, self.calls = passed, stopped, during, []

    def run(self, *, case, argv, cancelled, **kwargs):
        assert argv[-1] == "/usr/local/tools/stemlab/checkout"
        assert not cancelled()
        self.calls.append(case.name)
        if self.during is not None:
            self.during()
        return dict(
            name=case.name,
            kind=case.kind,
            passed=self.passed,
            outcome="completed" if self.passed else "nonzero_exit",
            exit_code=0 if self.passed else 1,
            elapsed_ms=2,
            dispatched=True,
            stopped=self.stopped,
            isolation_identity="explicit-postflight-process-fixture",
            output_sha256="e" * 64,
        )


def release(rig, executor=None):
    rig.authority.record_repin(operation=rig.operation)
    return rig.authority.postflight(
        operation=rig.operation, suite=rig.suite, executor=executor or Postflight()
    )


def test_postflight_is_required_and_replay_reads_signed_pg_receipt(release_rig):
    r = release_rig
    assert "stemlab" in blocked_directories(r.manager.runtime)
    assert r.authority.record_repin(operation=r.operation)["state"] == "awaiting_postflight"
    assert "stemlab" in blocked_directories(r.manager.runtime)
    executor = Postflight()
    assert (
        r.authority.postflight(operation=r.operation, suite=r.suite, executor=executor)["state"]
        == "active"
    )
    assert executor.calls == ["smoke", "unit"]
    assert "stemlab" not in blocked_directories(r.manager.runtime)
    fresh = r.worker.release_authority()
    assert fresh.admits(r.hold, r.manager.runtime)
    fresh.postflight(operation=r.operation, suite=r.suite, executor=executor)
    assert executor.calls == ["smoke", "unit"]
    public = r.approvals.inspect(r.actor, r.operation)
    assert public["releaseState"] == "released" and public["releaseGeneration"] == 1
    assert len(public["releaseBindingSha256"]) == len(public["releaseEvidenceSha256"]) == 64
    with r.app_env.other_db.transaction(r.app_env.other_context) as conn:
        assert (
            conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE id=%s", (r.operation,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT * FROM ui_tool_upgrade_audit WHERE upgrade_id=%s", (r.operation,)
            ).fetchall()
            == []
        )


def test_changed_source_pins_policy_and_forged_evidence_block_dispatch(release_rig):
    r = release_rig
    release(r)
    changed = replace(
        r.manager.runtime, tool_entrypoints=(("/usr/local/tools/stemlab/static/stemlab", "0" * 64),)
    )
    assert "stemlab" in blocked_directories(changed)
    changed_policy = replace(r.manager.runtime, qualification_sha256="0" * 64)
    assert "stemlab" in blocked_directories(changed_policy)
    extra_source = r.tools / "stemlab/extra.py"
    extra_source.write_text("print('not included in the approved installation')\n")
    assert "stemlab" in blocked_directories(r.manager.runtime)
    extra_source.unlink()
    assert "stemlab" not in blocked_directories(r.manager.runtime)
    binary = r.tools / "stemlab/static/stemlab"
    binary.write_bytes(b"altered")
    assert "stemlab" in blocked_directories(r.manager.runtime)
    binary.write_bytes(b"v2")
    assert "stemlab" not in blocked_directories(r.manager.runtime)
    with r.app_env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET release_evidence='{}'::jsonb WHERE workspace_id=%s AND id=%s",
            (r.actor.workspace_id, r.operation),
        )
    assert "stemlab" in blocked_directories(r.manager.runtime)
    with pytest.raises(StateMismatchError):
        r.authority.postflight(operation=r.operation, suite=r.suite, executor=Postflight())


def test_failed_postflight_keeps_hold_and_rollback_needs_new_postflight(release_rig):
    r = release_rig
    failed = Postflight(passed=False)
    with pytest.raises(StateMismatchError):
        release(r, failed)
    assert r.approvals.inspect(r.actor, r.operation)["releaseState"] == "failed"
    with pytest.raises(StateMismatchError):
        r.authority.postflight(operation=r.operation, suite=r.suite, executor=failed)
    assert failed.calls == ["smoke"]
    r.manager.rollback(
        workspace_id=r.actor.workspace_id,
        agent_id=r.agent,
        operation_id=r.operation,
        slug="stemlab",
        expected_current_commit=r.new.commit,
        confirm=True,
    )
    restored = read_hold(r.tools, "stemlab")
    assert "stemlab" in blocked_directories(r.manager.runtime)
    r.manager.runtime = replace(
        r.manager.runtime, tool_entrypoints=tuple(restored["entrypoints"].items())
    )
    outcome = release(r)
    assert outcome["state"] == "rolled_back"
    assert r.approvals.inspect(r.actor, r.operation)["releaseGeneration"] == 2
    assert "stemlab" not in blocked_directories(r.manager.runtime)


def test_unconfirmed_stop_and_duplicate_postflight_cannot_release(release_rig):
    r = release_rig
    r.authority.record_repin(operation=r.operation)

    def attempt_concurrent():
        with pytest.raises(StateMismatchError):
            r.worker.release_authority().postflight(
                operation=r.operation, suite=r.suite, executor=Postflight()
            )

    uncertain = Postflight(stopped=False, during=attempt_concurrent)
    with pytest.raises(StateMismatchError):
        r.authority.postflight(operation=r.operation, suite=r.suite, executor=uncertain)
    assert uncertain.calls == ["smoke"]
    assert r.approvals.inspect(r.actor, r.operation)["releaseState"] == "indeterminate"
    assert "stemlab" in blocked_directories(r.manager.runtime)
    assert r.ledger.active(r.actor.workspace_id)
    with pytest.raises(StateMismatchError):
        r.authority.record_repin(operation=r.operation)


def test_installed_restart_binds_protected_pg_reader_and_missing_secret_holds(
    release_rig, monkeypatch
):
    r = release_rig
    release(r)
    monkeypatch.setattr("potbot.credentials.CredentialStore.get", lambda *args: "S" * 64)
    fresh = bind_installed_tool_releases(
        r.app_env.db,
        r.actor,
        replace(r.manager.runtime, tool_release_authority=None),
        r.ledger,
        r.homes,
    )
    assert "stemlab" not in blocked_directories(fresh)
    with pytest.raises(ScopeDeniedError):
        fresh.tool_release_authority.record_repin(operation=r.operation)
    monkeypatch.setattr(
        "potbot.credentials.CredentialStore.get",
        lambda *args: (_ for _ in ()).throw(RuntimeError("locked")),
    )
    unavailable = bind_installed_tool_releases(
        r.app_env.db, r.actor, replace(fresh, tool_release_authority=None), r.ledger, r.homes
    )
    assert "stemlab" in blocked_directories(unavailable)
    monkeypatch.setattr("potbot.credentials.CredentialStore.get", lambda *args: None)
    missing = bind_installed_tool_releases(
        r.app_env.db, r.actor, replace(fresh, tool_release_authority=None), r.ledger, r.homes
    )
    assert missing.tool_release_authority is None
    assert "stemlab" in blocked_directories(missing)


def test_postflight_cannot_release_before_durable_host_lease_completion(release_rig, monkeypatch):
    from potbot.tools.workspace_supervision import WorkspaceLeaseGuard

    r = release_rig
    r.authority.record_repin(operation=r.operation)
    original = WorkspaceLeaseGuard.finish

    def fail_completion(guard, *, evidence_sha256, uncertain=False):
        if guard.request.operation == "tool.postflight" and not uncertain:
            guard._fenced.set()
            raise StateMismatchError("Injected host lease completion failure")
        return original(guard, evidence_sha256=evidence_sha256, uncertain=uncertain)

    monkeypatch.setattr(WorkspaceLeaseGuard, "finish", fail_completion)
    with pytest.raises(StateMismatchError, match="completion failure"):
        r.authority.postflight(operation=r.operation, suite=r.suite, executor=Postflight())
    public = r.approvals.inspect(r.actor, r.operation)
    assert public["releaseState"] == "indeterminate"
    assert public["releaseEvidenceSha256"] is None
    assert "stemlab" in blocked_directories(r.manager.runtime)
    assert r.ledger.active(r.actor.workspace_id)


@pytest.mark.parametrize("field", ("old_commit", "new_commit", "evidence_sha256", "suite_sha256"))
def test_matching_operation_id_cannot_release_different_signed_content(release_rig, field):
    """A valid independently signed decision still has to match this installation."""
    r = release_rig
    with r.app_env.db.transaction(r.actor) as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE id=%s", (r.operation,)
            ).fetchone()
        )
        revision = conn.execute(
            "SELECT revision FROM ui_tool_upgrade_audit WHERE upgrade_id=%s AND action='approved'",
            (r.operation,),
        ).fetchone()["revision"]
        row[field] = "0" * (40 if field.endswith("commit") else 64)
        signature = _approval_mac("S" * 64, row, row["approval_principal_id"], revision)
        # Field names are a fixed test parametrization, never request data.
        conn.execute(
            f"UPDATE ui_tool_upgrade_approvals SET {field}=%s,approval_mac=%s WHERE id=%s",
            (row[field], signature, r.operation),
        )
    with pytest.raises(ScopeDeniedError, match="differs from signed"):
        r.authority.record_repin(operation=r.operation)
    with pytest.raises(ScopeDeniedError, match="differs from signed"):
        r.authority.reconcile_activation(operation=r.operation, confirm=True)
    assert r.approvals.inspect(r.actor, r.operation)["releaseState"] == "none"
    assert "stemlab" in blocked_directories(r.manager.runtime)


def test_migration_preserves_pre_s12_approval_without_inventing_release(app_env):
    schema = "before_s12"
    revisions = application_migrations()
    assert revisions[37].version == 38
    migrate(app_env.admin_dsn, schema, revisions[:37])
    with app_env.db.transaction(app_env.context) as conn:
        role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    workspace = provision_workspace(app_env.admin_dsn, schema, "before S12")
    context = provision_principal(app_env.admin_dsn, schema, role, workspace)
    ident = uuid4()
    with admin_connection(app_env.admin_dsn, schema) as conn:
        conn.execute(
            "INSERT INTO ui_tool_upgrade_approvals(workspace_id,id,principal_id,agent_id,slug,old_commit,new_commit,"
            "evidence_sha256,suite_sha256,passed_tests,tests,state) VALUES(%s,%s,%s,%s,'tool',%s,%s,%s,%s,2,'[{},{}]'::jsonb,'active')",
            (
                workspace,
                ident,
                context.principal_id,
                uuid4(),
                "a" * 40,
                "b" * 40,
                "c" * 64,
                "d" * 64,
            ),
        )
    migrate(app_env.admin_dsn, schema)
    with Database(app_env.db.dsn, schema).transaction(context) as conn:
        row = conn.execute(
            "SELECT * FROM ui_tool_upgrade_approvals WHERE id=%s", (ident,)
        ).fetchone()
    assert row["state"] == "active" and row["release_phase"] == "none"
    assert row["release_binding"] is None and row["release_mac"] is None
