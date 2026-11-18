"""First-install S11/S12 authority using real PG, Git and filesystem journals.

Process receipts are named fixtures; native host qualification is independent.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.shared_tool_release_markers import blocked_directories, read_hold
from potbot.tools.tool_release_gate_s12 import ToolReleaseAuthority
from potbot.tools.workspace_supervision import WorkspaceLeaseGuard
from tests.integration.test_shared_tool_release_s12 import Postflight, release
from tests.integration.test_shared_tool_release_s12 import release_rig as release_rig

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("release_rig", ["first_install"], indirect=True),
]


def withdraw(r):
    return r.manager.rollback(
        workspace_id=r.actor.workspace_id,
        agent_id=r.agent,
        operation_id=r.operation,
        slug="stemlab",
        expected_current_commit=r.new.commit,
        confirm=True,
    )


def remove_pins(r):
    r.manager.runtime = replace(r.manager.runtime, tool_entrypoints=())


def test_signed_first_install_needs_postflight_then_withdraws_to_verified_absence(release_rig):
    r = release_rig
    public = r.approvals.inspect(r.actor, r.operation)
    assert public["oldCommit"] is None
    assert public["state"] == "awaiting_repin"
    assert "stemlab" in blocked_directories(r.manager.runtime)
    executor = Postflight()
    assert release(r, executor)["releaseState"] == "released"
    assert executor.calls == ["smoke", "unit"]
    assert "stemlab" not in blocked_directories(r.manager.runtime)
    assert withdraw(r)["installation_state"] == "absent"
    assert "stemlab" in blocked_directories(r.manager.runtime)
    with pytest.raises(StateMismatchError, match="pins"):
        r.authority.record_repin(operation=r.operation)
    remove_pins(r)
    result = r.authority.record_repin(operation=r.operation)
    assert result == {"id": str(r.operation), "state": "rolled_back", "releaseState": "withdrawn"}
    assert r.authority.record_repin(operation=r.operation) == result
    assert r.approvals.inspect(r.actor, r.operation)["releaseGeneration"] == 2
    assert not (r.tools / "stemlab").exists()
    with pytest.raises(StateMismatchError):
        r.store.definition(r.actor.workspace_id, "stemlab")
    with pytest.raises(StateMismatchError):
        r.authority.admits(read_hold(r.tools, "stemlab"), r.manager.runtime)
    with pytest.raises(StateMismatchError):
        r.authority.postflight(operation=r.operation, suite=r.suite, executor=executor)
    assert executor.calls == ["smoke", "unit"]


def test_failed_first_install_postflight_can_be_removed_without_replaying_it(release_rig):
    r = release_rig
    executor = Postflight(passed=False)
    with pytest.raises(StateMismatchError):
        release(r, executor)
    assert r.approvals.inspect(r.actor, r.operation)["releaseState"] == "failed"
    withdraw(r)
    remove_pins(r)
    assert r.authority.record_repin(operation=r.operation)["releaseState"] == "withdrawn"
    assert executor.calls == ["smoke"]
    assert "stemlab" in blocked_directories(r.manager.runtime)


def test_interrupted_activation_reconciliation_hands_off_pins_without_releasing(release_rig):
    r = release_rig
    remove_pins(r)
    with r.app_env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET state='indeterminate' WHERE workspace_id=%s AND id=%s",
            (r.actor.workspace_id, r.operation),
        )
    assert r.authority.reconcile_activation(operation=r.operation, confirm=True) == {
        "id": str(r.operation),
        "state": "awaiting_repin",
        "releaseState": "none",
    }
    assert "stemlab" in blocked_directories(r.manager.runtime)
    with pytest.raises(StateMismatchError, match="pins"):
        r.authority.record_repin(operation=r.operation)
    (r.tools / "stemlab" / "static" / "stemlab").write_bytes(b"tampered")
    with pytest.raises(StateMismatchError):
        r.authority.reconcile_activation(operation=r.operation, confirm=True)


@pytest.mark.parametrize("action", ["record_repin", "reconcile_activation"])
def test_release_control_commit_validator_uses_the_locked_transaction_and_can_deny(
    release_rig, action
):
    r = release_rig
    before = r.approvals.inspect(r.actor, r.operation)
    seen = []

    def deny(conn):
        row = conn.execute(
            "SELECT id FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (r.actor.workspace_id, r.operation),
        ).fetchone()
        assert row["id"] == r.operation
        seen.append(row["id"])
        raise StateMismatchError("current host claim was fenced")

    arguments = {"operation": r.operation, "authorize_commit": deny}
    if action == "reconcile_activation":
        arguments["confirm"] = True
    with pytest.raises(StateMismatchError, match="fenced"):
        getattr(r.authority, action)(**arguments)
    assert seen == [r.operation]
    assert r.approvals.inspect(r.actor, r.operation) == before
    assert r.ledger.active(r.actor.workspace_id) == []


def test_stale_postflight_claim_is_denied_before_process_dispatch(release_rig):
    r = release_rig
    r.authority.record_repin(operation=r.operation)
    before = r.approvals.inspect(r.actor, r.operation)
    executor = Postflight()

    def deny():
        assert any(
            row["operation"] == "tool.postflight" for row in r.ledger.active(r.actor.workspace_id)
        )
        raise ScopeDeniedError("stale postflight claim")

    with pytest.raises(ScopeDeniedError, match="stale"):
        r.authority.postflight(
            operation=r.operation, suite=r.suite, executor=executor, authorize_attempt=deny
        )
    assert executor.calls == []
    assert r.approvals.inspect(r.actor, r.operation) == before
    assert r.ledger.active(r.actor.workspace_id) == []


def test_new_recovery_fence_between_host_completion_and_pg_commit_cannot_be_overwritten(
    release_rig, monkeypatch
):
    r = release_rig
    r.authority.record_repin(operation=r.operation)
    original = WorkspaceLeaseGuard.finish
    recovery_revision = []

    def finish(guard, **kwargs):
        original(guard, **kwargs)
        if guard.request.operation == "tool.postflight" and not recovery_revision:
            # Deterministically inject the durable state change performed by
            # an independently signed recovery request after the old host
            # lease completed, before its release transaction starts.
            with r.app_env.db.transaction(r.actor) as conn:
                row = conn.execute(
                    "UPDATE ui_tool_upgrade_approvals SET release_phase='indeterminate',"
                    "postflight_token=NULL,revision=revision+1 WHERE workspace_id=%s AND id=%s RETURNING revision",
                    (r.actor.workspace_id, r.operation),
                ).fetchone()
                recovery_revision.append(row["revision"])

    def current_claim(conn):
        phase = conn.execute(
            "SELECT release_phase FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND id=%s",
            (r.actor.workspace_id, r.operation),
        ).fetchone()["release_phase"]
        if phase == "indeterminate":
            raise StateMismatchError("new recovery fenced the old host claim")

    monkeypatch.setattr(WorkspaceLeaseGuard, "finish", finish)
    executor = Postflight()
    with pytest.raises(StateMismatchError, match="fenced"):
        r.authority.postflight(
            operation=r.operation,
            suite=r.suite,
            executor=executor,
            authorize_commit=current_claim,
        )
    public = r.approvals.inspect(r.actor, r.operation)
    assert public["revision"] == recovery_revision[0]
    assert public["releaseState"] == "indeterminate"
    assert executor.calls == ["smoke", "unit"]
    assert "stemlab" in blocked_directories(r.manager.runtime)
    assert r.ledger.active(r.actor.workspace_id) == []
    assert r.authority.read_held(operation=r.operation)["releaseState"] == "indeterminate"
    assert r.approvals.inspect(r.actor, r.operation)["revision"] == recovery_revision[0]


def test_recovery_can_acknowledge_failed_held_release_without_new_pins_or_test_replay(release_rig):
    r = release_rig
    executor = Postflight(passed=False)
    with pytest.raises(StateMismatchError):
        release(r, executor)
    before = r.approvals.inspect(r.actor, r.operation)
    remove_pins(r)
    assert r.authority.read_held(operation=r.operation) == {
        "id": str(r.operation),
        "state": before["state"],
        "releaseState": "failed",
    }
    assert r.approvals.inspect(r.actor, r.operation) == before
    assert executor.calls == ["smoke"]
    assert "stemlab" in blocked_directories(r.manager.runtime)
    (r.tools / "stemlab" / "static" / "stemlab").write_bytes(b"changed")
    with pytest.raises(StateMismatchError):
        r.authority.read_held(operation=r.operation)


def test_held_state_readback_cannot_substitute_for_confirmed_process_termination(release_rig):
    r = release_rig
    executor = Postflight(passed=False, stopped=False)
    with pytest.raises(StateMismatchError):
        release(r, executor)
    before = r.approvals.inspect(r.actor, r.operation)
    with pytest.raises(StateMismatchError):
        r.authority.read_held(operation=r.operation)
    assert r.approvals.inspect(r.actor, r.operation) == before
    assert r.ledger.active(r.actor.workspace_id)[0]["state"] == "indeterminate"


@pytest.mark.parametrize("changed", ["pins", "tree", "active_path", "approval", "key"])
def test_first_install_withdrawal_rechecks_exact_absence_and_signed_approval(release_rig, changed):
    r = release_rig
    release(r)
    withdraw(r)
    remove_pins(r)
    authority = r.authority
    if changed == "pins":
        r.manager.runtime = replace(
            r.manager.runtime, tool_entrypoints=tuple(r.hold["entrypoints"].items())
        )
    elif changed == "tree":
        (r.manager._location("stemlab", r.operation) / "static" / "stemlab").write_bytes(b"drift")
    elif changed == "active_path":
        (r.tools / "stemlab").symlink_to(r.tools / "not-present")
    elif changed == "approval":
        with r.app_env.db.transaction(r.actor) as conn:
            conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET new_commit=%s WHERE workspace_id=%s AND id=%s",
                ("0" * 40, r.actor.workspace_id, r.operation),
            )
    else:
        authority = ToolReleaseAuthority(
            approvals=r.approvals,
            manager=r.manager,
            context=r.actor,
            agent_id=r.agent,
            setup_token="R" * 64,
        )
    with pytest.raises((ScopeDeniedError, StateMismatchError)):
        authority.record_repin(operation=r.operation)
    assert r.approvals.inspect(r.actor, r.operation)["releaseState"] != "withdrawn"
    assert "stemlab" in blocked_directories(r.manager.runtime)


def test_forged_withdrawal_evidence_is_not_a_valid_retained_receipt(release_rig):
    r = release_rig
    release(r)
    withdraw(r)
    remove_pins(r)
    r.authority.record_repin(operation=r.operation)
    with r.app_env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET release_evidence='{}'::jsonb "
            "WHERE workspace_id=%s AND id=%s",
            (r.actor.workspace_id, r.operation),
        )
    with pytest.raises(StateMismatchError, match="evidence"):
        r.authority.record_repin(operation=r.operation)
    assert "stemlab" in blocked_directories(r.manager.runtime)
