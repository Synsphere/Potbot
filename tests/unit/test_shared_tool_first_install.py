"""Actual Git and filesystem first installs, with explicit process fixtures.

These cases verify the existing S9 journal/leases and S12 denial projection;
they do not claim a native sandbox build or postflight qualification.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.shared_tool_release_markers import blocked_directories, read_hold
from potbot.tools.shared_tool_upgrades import _move_to_absent, stage_name
from tests.unit.test_shared_tool_upgrades_s9 import install, reconcile_uncertain
from tests.unit.test_shared_tool_upgrades_s9 import rig as rig


@pytest.fixture
def initial_rig(rig):
    shutil.rmtree(rig["tools"] / "stemlab")
    with rig["ledger"]._connect() as db:
        db.execute("DELETE FROM trusted_shared_tools")
    return rig


def prepare_initial(r):
    return r["manager"].prepare(
        workspace_id=r["workspace_id"],
        agent_id=r["agent_id"],
        operation_id=r["operation_id"],
        expected_old_commit=None,
        replacement=r["new"],
        suite=r["suite"],
    )


def activate_initial(r):
    return r["manager"].activate(
        workspace_id=r["workspace_id"],
        agent_id=r["agent_id"],
        operation_id=r["operation_id"],
        slug="stemlab",
        expected_old_commit=None,
        confirm=True,
    )


def withdraw(r):
    return r["manager"].rollback(
        workspace_id=r["workspace_id"],
        agent_id=r["agent_id"],
        operation_id=r["operation_id"],
        slug="stemlab",
        expected_current_commit=r["new"].commit,
        confirm=True,
    )


def test_first_install_and_withdrawal_preserve_absence_and_exact_retained_bytes(initial_rig):
    r = initial_rig
    prepared = prepare_initial(r)
    assert prepared["old_commit"] is None and prepared["state"] == "ready"
    assert len(r["executor"].calls) == 2
    assert not (r["tools"] / "stemlab").exists()
    with pytest.raises(StateMismatchError):
        r["store"].definition(r["workspace_id"], "stemlab")
    with r["ledger"]._connect() as db:
        assert (
            db.execute("SELECT old_definition FROM trusted_tool_upgrades").fetchone()[0] == "null"
        )
    activated = activate_initial(r)
    assert activated["old_commit"] is None and activated["new_commit"] == r["new"].commit
    assert r["store"].definition(r["workspace_id"], "stemlab") == r["new"]
    stage = r["tools"] / stage_name("stemlab", r["operation_id"])
    assert not stage.exists()
    hold = read_hold(r["tools"], "stemlab")
    assert hold["direction"] == "activate"
    r["manager"].runtime = replace(
        r["runtime"], tool_entrypoints=tuple(hold["entrypoints"].items())
    )
    assert "stemlab" in blocked_directories(r["manager"].runtime)
    result = withdraw(r)
    assert result["installation_state"] == "absent" and result["old_commit"] is None
    assert not (r["tools"] / "stemlab").exists()
    assert (stage / "static" / "stemlab").read_bytes() == b"v2"
    with pytest.raises(StateMismatchError):
        r["store"].definition(r["workspace_id"], "stemlab")
    withdrawn = read_hold(r["tools"], "stemlab")
    assert withdrawn["direction"] == "withdraw"
    assert withdrawn["commit"] == r["new"].commit  # Identifies the removed tree.
    assert "stemlab" in blocked_directories(r["manager"].runtime)
    proposal = r["manager"].runtime_entrypoint_updates(
        workspace_id=r["workspace_id"], agent_id=r["agent_id"], operation_id=r["operation_id"]
    )
    assert proposal["candidate_commit"] is None
    assert proposal["pins"] == {}
    assert proposal["remove_prior_pins"] == ["/usr/local/tools/stemlab/static/stemlab"]
    with pytest.raises(StateMismatchError):
        activate_initial(r)


def test_source_preflight_distinguishes_absence_from_unfinished_or_damaged_installation(
    initial_rig,
):
    r = initial_rig
    arguments = dict(workspace_id=r["workspace_id"], agent_id=r["agent_id"], slug="stemlab")
    assert r["manager"].acquisition_predecessor(**arguments) is None
    prepare_initial(r)
    with pytest.raises(StateMismatchError, match="unfinished"):
        r["manager"].acquisition_predecessor(**arguments)
    activate_initial(r)
    assert r["manager"].acquisition_predecessor(**arguments) == r["new"]
    (r["tools"] / "stemlab" / "static" / "stemlab").write_bytes(b"damaged")
    with pytest.raises(StateMismatchError):
        r["manager"].acquisition_predecessor(**arguments)


def test_incomplete_s10_copy_remains_hidden_after_lease_reconciliation(initial_rig):
    r = initial_rig
    name = ".potbot-s10-incoming-" + uuid4().hex
    (r["tools"] / name).mkdir()
    assert name in blocked_directories(r["runtime"])


@pytest.mark.parametrize("prepared", [False, True])
def test_abandoned_candidate_stays_hidden_and_releases_the_next_review(initial_rig, prepared):
    r = initial_rig
    if prepared:
        prepare_initial(r)
    arguments = dict(
        workspace_id=r["workspace_id"],
        agent_id=r["agent_id"],
        operation_id=r["operation_id"],
        slug="stemlab",
        confirm=True,
    )
    result = r["manager"].abandon_candidate(**arguments)
    assert result["state"] == "aborted" and result["candidate_retained"] is True
    assert r["manager"].abandon_candidate(**arguments) == result
    assert stage_name("stemlab", r["operation_id"]) in blocked_directories(r["runtime"])
    assert (
        r["manager"].acquisition_predecessor(
            workspace_id=r["workspace_id"], agent_id=r["agent_id"], slug="stemlab"
        )
        is None
    )
    assert len(r["executor"].calls) == (2 if prepared else 0)


def test_abandonment_cannot_clear_an_active_or_uncertain_transition(initial_rig, monkeypatch):
    r = initial_rig
    prepare_initial(r)
    activate_initial(r)
    arguments = dict(
        workspace_id=r["workspace_id"],
        agent_id=r["agent_id"],
        operation_id=r["operation_id"],
        slug="stemlab",
        confirm=True,
    )
    with pytest.raises(StateMismatchError):
        r["manager"].abandon_candidate(**arguments)
    monkeypatch.setattr(
        r["manager"], "_commit_swap", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        withdraw(r)
    with pytest.raises(StateMismatchError):
        r["manager"].abandon_candidate(**arguments)
    with r["ledger"]._connect() as db:
        assert db.execute("SELECT state FROM trusted_tool_upgrades").fetchone()[0] == "rolling_back"
    assert len(r["executor"].calls) == 2


def test_s9_claim_fence_is_checked_inside_lease_before_any_test(initial_rig):
    r = initial_rig

    def deny():
        assert any(
            row["operation"] == "tool.test_upgrade" for row in r["ledger"].active(r["workspace_id"])
        )
        raise ScopeDeniedError("stale acquisition claim")

    with pytest.raises(ScopeDeniedError, match="stale"):
        r["manager"].prepare(
            workspace_id=r["workspace_id"],
            agent_id=r["agent_id"],
            operation_id=r["operation_id"],
            expected_old_commit=None,
            replacement=r["new"],
            suite=r["suite"],
            authorize_attempt=deny,
        )
    assert r["executor"].calls == []
    assert r["ledger"].active(r["workspace_id"]) == []


def test_s9_revalidates_claim_between_tests_without_replaying_a_completed_test(initial_rig):
    r = initial_rig

    def authorize():
        if r["executor"].calls:
            raise ScopeDeniedError("claim fenced after confirmed first test stop")

    with pytest.raises(ScopeDeniedError, match="fenced"):
        r["manager"].prepare(
            workspace_id=r["workspace_id"],
            agent_id=r["agent_id"],
            operation_id=r["operation_id"],
            expected_old_commit=None,
            replacement=r["new"],
            suite=r["suite"],
            authorize_attempt=authorize,
        )
    assert len(r["executor"].calls) == 1
    with r["ledger"]._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trusted_tool_upgrades").fetchone()[0] == 0
    assert r["ledger"].active(r["workspace_id"]) == []


def test_s9_rejects_non_executable_artifacts_before_spending_test_work(initial_rig):
    r = initial_rig
    (r["manager"]._location("stemlab", r["operation_id"]) / "static" / "stemlab").chmod(0o644)
    with pytest.raises(StateMismatchError, match="executable artifact"):
        prepare_initial(r)
    assert r["executor"].calls == []


@pytest.mark.parametrize("action", ["activate", "rollback", "reconcile", "abandon_candidate"])
def test_stale_host_actions_cannot_mutate_after_recovery_or_create_false_uncertainty(
    initial_rig, monkeypatch, action
):
    r = initial_rig
    prepare_initial(r)
    if action == "rollback":
        activate_initial(r)
    if action == "reconcile":
        original = r["manager"]._commit_swap
        monkeypatch.setattr(
            r["manager"], "_commit_swap", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash"))
        )
        with pytest.raises(OSError):
            activate_initial(r)
        reconcile_uncertain(r)
        monkeypatch.setattr(r["manager"], "_commit_swap", original)
    before = read_hold(r["tools"], "stemlab")
    with r["ledger"]._connect() as db:
        journal_state = db.execute("SELECT state FROM trusted_tool_upgrades").fetchone()[0]

    def deny():
        assert r["ledger"].active(r["workspace_id"])
        raise ScopeDeniedError("stale host action")

    arguments = dict(
        workspace_id=r["workspace_id"],
        agent_id=r["agent_id"],
        operation_id=r["operation_id"],
        slug="stemlab",
        confirm=True,
        authorize_attempt=deny,
    )
    if action == "activate":
        arguments["expected_old_commit"] = None
    elif action == "rollback":
        arguments["expected_current_commit"] = r["new"].commit
    with pytest.raises(ScopeDeniedError, match="stale host"):
        getattr(r["manager"], action)(**arguments)
    assert read_hold(r["tools"], "stemlab") == before
    with r["ledger"]._connect() as db:
        assert db.execute("SELECT state FROM trusted_tool_upgrades").fetchone()[0] == journal_state
    assert r["ledger"].active(r["workspace_id"]) == []
    # A separate, freshly authorized host attempt must not inherit a consumed
    # lease key from the no-effect stale worker. The journal still prevents
    # a completed physical transition from executing twice.
    arguments["authorize_attempt"] = lambda: None
    recovered = getattr(r["manager"], action)(**arguments)
    assert (
        recovered["state"]
        == {
            "activate": "active",
            "rollback": "rolled_back",
            "reconcile": "aborted",
            "abandon_candidate": "aborted",
        }[action]
    )
    assert r["ledger"].active(r["workspace_id"]) == []


@pytest.mark.parametrize("occupied", ["registry", "directory", "dangling_link"])
def test_first_install_requires_physical_and_registered_absence(initial_rig, occupied):
    r = initial_rig
    if occupied == "registry":
        with r["ledger"]._connect() as db:
            db.execute(
                "INSERT INTO trusted_shared_tools VALUES(?,?,?)",
                (str(r["workspace_id"]), "stemlab", json.dumps(r["old"].to_dict(), sort_keys=True)),
            )
    elif occupied == "directory":
        (r["tools"] / "stemlab").mkdir()
    else:
        (r["tools"] / "stemlab").symlink_to(r["tools"] / "missing")
    with pytest.raises(StateMismatchError):
        prepare_initial(r)
    assert r["executor"].calls == []


@pytest.mark.parametrize("changed", ["registry", "destination", "source"])
def test_first_install_rechecks_absence_and_tested_bytes_before_promotion(initial_rig, changed):
    r = initial_rig
    prepare_initial(r)
    if changed == "registry":
        with r["ledger"]._connect() as db:
            db.execute(
                "INSERT INTO trusted_shared_tools VALUES(?,?,?)",
                (str(r["workspace_id"]), "stemlab", json.dumps(r["old"].to_dict(), sort_keys=True)),
            )
    elif changed == "destination":
        (r["tools"] / "stemlab").mkdir()
        (r["tools"] / "stemlab" / "keep").write_text("unrelated")
    else:
        (r["tools"] / stage_name("stemlab", r["operation_id"]) / "static/stemlab").write_bytes(
            b"changed"
        )
    with pytest.raises(StateMismatchError):
        activate_initial(r)
    assert read_hold(r["tools"], "stemlab") is None
    if changed == "destination":
        assert (r["tools"] / "stemlab" / "keep").read_text() == "unrelated"


@pytest.mark.parametrize("withdrawing", [False, True])
@pytest.mark.parametrize("after_move", [False, True])
def test_first_install_crashes_require_reconciliation_without_process_replay(
    initial_rig, monkeypatch, withdrawing, after_move
):
    from potbot.tools import shared_tool_upgrades

    r = initial_rig
    prepare_initial(r)
    if withdrawing:
        activate_initial(r)
    manager = r["manager"]
    owner, attribute = (
        (manager, "_commit_swap") if after_move else (shared_tool_upgrades, "_move_to_absent")
    )
    original = getattr(owner, attribute)

    def fail(*args, **kwargs):
        raise OSError("injected transition crash")

    monkeypatch.setattr(owner, attribute, fail)
    action = withdraw if withdrawing else activate_initial
    with pytest.raises(OSError, match="injected"):
        action(r)
    with pytest.raises(StateMismatchError):
        action(r)
    assert "stemlab" in blocked_directories(r["runtime"])
    reconcile_uncertain(r)
    monkeypatch.setattr(owner, attribute, original)
    result = manager.reconcile(
        workspace_id=r["workspace_id"],
        agent_id=r["agent_id"],
        operation_id=r["operation_id"],
        slug="stemlab",
        confirm=True,
    )
    expected = (
        "rolled_back" if withdrawing and after_move else "active" if withdrawing else "aborted"
    )
    assert result["state"] == expected
    assert len(r["executor"].calls) == 2
    if expected == "active":
        assert r["store"].definition(r["workspace_id"], "stemlab") == r["new"]
        assert read_hold(r["tools"], "stemlab")["direction"] == "activate"
    else:
        assert not (r["tools"] / "stemlab").exists()
        with pytest.raises(StateMismatchError):
            r["store"].definition(r["workspace_id"], "stemlab")


def test_withdrawn_slot_accepts_new_review_but_old_operation_cannot_replace_it(initial_rig):
    r = initial_rig
    prepare_initial(r)
    activate_initial(r)
    withdraw(r)
    follow = dict(r, operation_id=uuid4())
    follow["new"] = install(r["tools"] / stage_name("stemlab", follow["operation_id"]), "v3")
    prepare_initial(follow)
    activate_initial(follow)
    with pytest.raises(StateMismatchError):
        withdraw(r)
    with pytest.raises(StateMismatchError):
        r["manager"].runtime_entrypoint_updates(
            workspace_id=r["workspace_id"], agent_id=r["agent_id"], operation_id=r["operation_id"]
        )
    assert r["store"].definition(r["workspace_id"], "stemlab") == follow["new"]


def test_no_replace_rename_never_overwrites_or_follows_a_destination(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "data").write_text("source")
    (tmp_path / "target").symlink_to(tmp_path / "absent")
    with pytest.raises(StateMismatchError):
        _move_to_absent(tmp_path, "source", "target")
    assert (tmp_path / "source" / "data").read_text() == "source"
    with pytest.raises(ScopeDeniedError):
        _move_to_absent(tmp_path, "target", "another")
