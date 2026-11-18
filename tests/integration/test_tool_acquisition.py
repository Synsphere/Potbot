"""Real PG admission, privacy, signed review, claims, recovery and migration.

S9/S10 readbacks here are explicit contract fixtures, not process qualification.
The separate pipeline acceptance test exercises the actual filesystem owners.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from potbot.api.ui_tool_upgrades import UiToolUpgradeStore
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.persistence.database import (
    Database,
    admin_connection,
    grant_application_role,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.shared_tool_store import SharedToolDefinition
from potbot.tools.tool_acquisition import ToolAcquisitionService, ToolRecipe
from tests.unit.test_tool_acquisition_recipe import recipe_document

pytestmark = pytest.mark.postgres
TOKEN = "tool-acquisition-independent-fixture-" + "a" * 48


@pytest.fixture
def rig(app_env):
    token = [TOKEN]
    service = ToolAcquisitionService(app_env.db, setup_token_resolver=lambda: token[0])
    recipe = ToolRecipe.from_dict(recipe_document())
    agent = uuid4()
    published = service.publish_recipe(app_env.context, recipe, agent, "publish", setup_token=TOKEN)
    return SimpleNamespace(
        env=app_env,
        actor=app_env.context,
        service=service,
        token=token,
        recipe=recipe,
        recipe_id=UUID(published["recipe"]["id"]),
        agent=agent,
    )


def request(r, key="request", commit="b" * 40):
    return r.service.request(r.actor, r.recipe_id, commit, key, setup_token=r.token[0])["operation"]


def observed_source(r, operation):
    identity = {
        "repository": r.recipe.repository,
        "commit": operation["newCommit"],
        "git_tree": "d" * 40,
        "tree_sha256": "e" * 64,
        "files": 3,
        "bytes": 256,
    }
    return {
        "schema": "potbot-tool-source/v1",
        "operation_id": operation["id"],
        **identity,
        "source_sha256": hashlib.sha256(
            (
                json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                + "\n"
            ).encode()
        ).hexdigest(),
        "storage_sha256": "f" * 64,
    }


def observed_execution(r, operation, *, old=None):
    return {
        **r.recipe.render(operation_id=UUID(operation["id"]), agent_directory="fixture-agent"),
        "image_digest": "1" * 64,
        "qualification_sha256": "2" * 64,
        "lockfiles": [["requirements.lock", "3" * 64]],
        "old_definition": old,
    }


def review(r, *, old=None):
    initial = request(r)
    claim = r.service.claim(
        r.actor, r.agent, "source", setup_token=r.token[0], operation_id=UUID(initial["id"])
    )
    assert claim is not None
    result = r.service.complete_source(
        r.actor, claim, observed_source(r, initial), observed_execution(r, initial, old=old)
    )
    return result["operation"]


def build_claim(r, *, old=None):
    current = review(r, old=old)
    approved = r.service.approve_build(
        r.actor,
        UUID(current["id"]),
        current["revision"],
        current["review"]["sha256"],
        "approve-build",
        setup_token=r.token[0],
    )
    claim = r.service.claim(
        r.actor, r.agent, "build", setup_token=r.token[0], operation_id=UUID(current["id"])
    )
    assert claim is not None
    return claim, approved["operation"]


def measured(r, claim):
    staging = {
        "schema": "potbot-tool-stage-s10/v1",
        "workspace_id": str(r.actor.workspace_id),
        "agent_id": str(r.agent),
        "operation_id": str(claim["id"]),
        "slug": r.recipe.slug,
        "new_commit": claim["new_commit"],
        "expected_old_commit": claim["old_commit"],
        "command_sha256": claim["execution"]["command_sha256"],
        "isolation_identity": "4" * 64,
        "inventory": {"tree_sha256": "5" * 64, "files_and_directories": 12, "bytes": 512},
    }
    tested = {
        "id": str(claim["id"]),
        "slug": r.recipe.slug,
        "state": "ready",
        "old_commit": claim["old_commit"],
        "new_commit": claim["new_commit"],
        "suite_sha256": r.recipe.tests.digest,
        "test_evidence_sha256": "6" * 64,
        "test_results": [
            {"name": case.name, "kind": case.kind, "elapsed_ms": 2, "passed": True}
            for case in r.recipe.tests.cases
        ],
    }
    return staging, tested


def prepared(r, *, old=None):
    claim, _ = build_claim(r, old=old)
    stage, test = measured(r, claim)
    return r.service.complete_build(r.actor, claim, stage, test)["operation"]


def action(r, current, kind, key=None):
    return r.service.action(
        r.actor,
        UUID(current["id"]),
        kind,
        current["revision"],
        current["bindingSha256"],
        key or uuid4().hex,
        setup_token=r.token[0],
    )["operation"]


def test_complete_admission_reuses_s11_identity_and_durable_original_replies(rig):
    r = rig
    initial = request(r)
    assert initial["phase"] == "source_queued" and initial["revision"] == 1
    assert UiToolUpgradeStore(r.env.db).list(r.actor)["items"] == []
    checked = prepared(r)
    assert checked["id"] == initial["id"] and checked["phase"] == "awaiting_approval"
    assert checked["oldCommit"] is None and checked["kind"] == "install"
    assert checked["installedState"] == "absent" and len(checked["tests"]) == 2
    assert request(r) == initial
    with pytest.raises(RevisionConflictError):
        request(r, commit="c" * 40)
    identity = UUID(checked["id"])
    approved = r.service.decide(
        r.actor,
        identity,
        checked["revision"],
        checked["evidenceSha256"],
        True,
        "promote",
        setup_token=TOKEN,
    )
    approvals = UiToolUpgradeStore(r.env.db)
    claim = approvals.claim(r.actor, agent_id=r.agent, setup_token=TOKEN, operation_id=identity)
    assert claim["id"] == identity and claim["old_commit"] is None
    assert (
        approvals.claim(r.actor, agent_id=r.agent, setup_token=TOKEN, operation_id=identity) is None
    )
    approvals.finish(r.actor, operation=identity, worker_token=claim["worker_token"], active=True)
    assert r.service.inspect(r.actor, identity)["operation"]["phase"] == "awaiting_repin"
    assert (
        r.service.decide(
            r.actor,
            identity,
            checked["revision"],
            checked["evidenceSha256"],
            True,
            "promote",
            setup_token=TOKEN,
        )
        == approved
    )
    events, cursor = [], 0
    while True:
        page = r.service.events(r.actor, identity, after=cursor, limit=2)
        events.extend(page["items"])
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            break
    assert [event["revision"] for event in events] == list(range(1, 10))
    public = json.dumps([initial, checked, approved, events])
    for private in (
        TOKEN,
        "worker_token",
        "source_request_mac",
        "approval_mac",
        "build_authorization",
    ):
        assert private not in public


def test_private_operations_and_legacy_decisions_cannot_cross_principals(rig):
    r = rig
    other = provision_principal(
        r.env.admin_dsn,
        r.env.schema,
        urlsplit(r.env.other_db.dsn).username,
        r.actor.workspace_id,
        principal_id=r.env.other_context.principal_id,
    )
    remote = ToolAcquisitionService(r.env.other_db, setup_token_resolver=lambda: TOKEN)
    current = prepared(r)
    identity = UUID(current["id"])
    assert remote.recipes(other)["items"] == remote.list(other)["items"] == []
    assert UiToolUpgradeStore(r.env.other_db).list(other)["items"] == []
    for call in (
        lambda: remote.inspect(other, identity),
        lambda: remote.events(other, identity),
        lambda: remote.request(other, r.recipe_id, "b" * 40, "request", setup_token=TOKEN),
        lambda: remote.decide(
            other,
            identity,
            current["revision"],
            current["evidenceSha256"],
            True,
            "promote",
            setup_token=TOKEN,
        ),
        lambda: UiToolUpgradeStore(r.env.other_db).inspect(other, identity),
        lambda: UiToolUpgradeStore(r.env.other_db).decide(
            other,
            identity,
            expected_revision=current["revision"],
            evidence_sha256=current["evidenceSha256"],
            approve=True,
            setup_token=TOKEN,
        ),
    ):
        with pytest.raises(ScopeDeniedError):
            call()
    assert remote.claim(other, r.agent, "build", setup_token=TOKEN) is None
    with r.env.other_db.transaction(other) as conn:
        assert conn.execute("SELECT * FROM ui_tool_acquisition_recipes").fetchall() == []
    with pytest.raises(ScopeDeniedError):
        remote.inspect(r.env.other_context, identity)


def test_review_covers_source_runtime_rendered_command_and_predecessor(rig):
    r = rig
    initial = request(r)
    claim = r.service.claim(r.actor, r.agent, "source", setup_token=TOKEN)
    receipt, execution = observed_source(r, initial), observed_execution(r, initial)
    for changed, exec_value in (
        ({**receipt, "operation_id": str(uuid4())}, execution),
        ({**receipt, "tree_sha256": "0" * 64}, execution),
        (receipt, {**execution, "build_argv": ["/usr/bin/python3", "unreviewed.py"]}),
        (receipt, {**execution, "lockfiles": [["different.lock", "3" * 64]]}),
    ):
        with pytest.raises((ScopeDeniedError, StateMismatchError, InvalidInputError)):
            r.service.complete_source(r.actor, claim, changed, exec_value)
    reviewed = r.service.complete_source(r.actor, claim, receipt, execution)["operation"]
    assert reviewed["review"]["sourceSha256"] == receipt["source_sha256"]
    assert reviewed["review"]["imageDigest"] == execution["image_digest"]
    assert reviewed["review"]["buildArgv"] == execution["build_argv"]
    with pytest.raises(StateMismatchError):
        r.service.complete_source(r.actor, claim, receipt, execution)
    for revision, digest in (
        (reviewed["revision"] - 1, reviewed["review"]["sha256"]),
        (reviewed["revision"], "0" * 64),
    ):
        with pytest.raises(RevisionConflictError):
            r.service.approve_build(
                r.actor, UUID(initial["id"]), revision, digest, uuid4().hex, setup_token=TOKEN
            )
    with pytest.raises(RevisionConflictError):
        with r.env.db.transaction(r.actor) as conn:
            conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET execution_contract='{}',revision=revision+1 WHERE id=%s",
                (UUID(initial["id"]),),
            )


def test_current_key_and_database_rows_cannot_forge_build_authority(rig):
    r = rig
    initial = request(r)
    current = review(r)
    rotated = "rotated-independent-capability-" + "z" * 48
    r.token[0] = rotated
    assert not r.service.recipes(r.actor)["items"][0]["approvalValid"]
    assert request(r) == initial  # replay recovers, but does not authorize new effects
    with pytest.raises(ScopeDeniedError):
        r.service.approve_build(
            r.actor,
            UUID(current["id"]),
            current["revision"],
            current["review"]["sha256"],
            "rotated",
            setup_token=rotated,
        )
    r.token[0] = TOKEN
    queued = r.service.approve_build(
        r.actor,
        UUID(current["id"]),
        current["revision"],
        current["review"]["sha256"],
        "build",
        setup_token=TOKEN,
    )["operation"]
    r.token[0] = rotated
    with pytest.raises(ScopeDeniedError):
        r.service.claim(r.actor, r.agent, "build", setup_token=rotated)
    assert r.service.inspect(r.actor, UUID(current["id"]))["operation"]["phase"] == "build_queued"
    r.token[0] = TOKEN
    with pytest.raises(RevisionConflictError):
        with r.env.db.transaction(r.actor) as conn:
            conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET build_authorization='{}',revision=revision+1 WHERE id=%s",
                (UUID(current["id"]),),
            )
    action(r, queued, "cancel")
    fake_recipe = uuid4()
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "INSERT INTO ui_tool_acquisition_recipes(workspace_id,id,principal_id,agent_id,recipe_sha256,content,approval_mac) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                r.actor.workspace_id,
                fake_recipe,
                r.actor.principal_id,
                r.agent,
                r.recipe.digest,
                Jsonb(r.recipe.to_dict()),
                "0" * 64,
            ),
        )
    with pytest.raises(ScopeDeniedError):
        r.service.request(r.actor, fake_recipe, "c" * 40, "forged", setup_token=TOKEN)


def test_exact_worker_claim_is_single_use_and_serializes_the_active_tool(rig):
    r = rig
    initial = request(r)
    with pytest.raises(RevisionConflictError):
        request(r, key="overlap", commit="c" * 40)
    assert r.service.claim(r.actor, uuid4(), "source", setup_token=TOKEN) is None
    assert (
        r.service.claim(r.actor, r.agent, "source", setup_token=TOKEN, operation_id=uuid4()) is None
    )
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return r.service.claim(
            r.actor, r.agent, "source", setup_token=TOKEN, operation_id=UUID(initial["id"])
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    live = next(value for value in claims if value is not None)
    assert claims.count(None) == 1
    r.service.validate_claim(r.actor, live)
    with pytest.raises(ScopeDeniedError):
        r.service.validate_claim(r.actor, {**live, "new_commit": "c" * 40})
    with pytest.raises(StateMismatchError):
        r.service.validate_claim(r.actor, {**live, "worker_token": uuid4()})
    r.service.fail(r.actor, live, "source_unavailable", uncertain=False)
    assert r.service.claim(r.actor, r.agent, "source", setup_token=TOKEN) is None
    assert request(r, key="fresh", commit="c" * 40)["id"] != initial["id"]


def test_unknown_build_is_never_replayed_and_signed_recovery_fences_a_delayed_worker(rig):
    r = rig
    claim, _ = build_claim(r)
    current = r.service.inspect(r.actor, claim["id"])["operation"]
    assert current["allowedActions"] == ["reconcile"]
    pending = action(r, current, "reconcile", key="recover")
    assert pending["phase"] == "indeterminate" and pending["hostAction"]["status"] == "queued"
    with pytest.raises(StateMismatchError):
        r.service.validate_claim(r.actor, claim)
    assert r.service.claim(r.actor, r.agent, "build", setup_token=TOKEN) is None
    recovery = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    assert (
        recovery["acquisition_phase"] == "building" and recovery["operation_state"] == "acquiring"
    )
    done = r.service.complete_action(
        r.actor, recovery, {"id": str(claim["id"]), "state": "aborted", "candidate_retained": True}
    )["operation"]
    assert done["phase"] == "cancelled" and done["allowedActions"] == []
    assert action(r, current, "reconcile", key="recover") == pending
    assert request(r, key="next", commit="c" * 40)["id"] != current["id"]


def test_rejected_tested_candidate_requires_host_abandonment_before_fresh_attempt(rig):
    r = rig
    current = prepared(r)
    rejected = r.service.decide(
        r.actor,
        UUID(current["id"]),
        current["revision"],
        current["evidenceSha256"],
        False,
        "reject",
        setup_token=TOKEN,
    )["operation"]
    assert rejected["allowedActions"] == ["reconcile"]
    with pytest.raises(RevisionConflictError):
        request(r, key="blocked", commit="c" * 40)
    action(r, rejected, "reconcile")
    recovery = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    done = r.service.complete_action(
        r.actor, recovery, {"id": current["id"], "state": "aborted", "candidate_retained": True}
    )["operation"]
    assert done["phase"] == "cancelled" and done["tests"] == current["tests"]
    assert done["evidenceSha256"] == current["evidenceSha256"]
    assert UiToolUpgradeStore(r.env.db).list(r.actor)["items"] == []
    assert request(r, key="fresh", commit="c" * 40)["id"] != current["id"]


@pytest.mark.parametrize("final_action", ["postflight", "reconcile", "lost_postflight_ack"])
def test_rollback_clears_previous_release_but_preserves_new_s12_postflight(rig, final_action):
    r = rig
    old = SharedToolDefinition(
        r.recipe.slug,
        r.recipe.repository,
        "a" * 40,
        ("build-cache",),
        (),
        False,
        (("static/tool", "a" * 64),),
    ).to_dict()
    current = prepared(r, old=old)
    identity = UUID(current["id"])
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET state='active',release_phase='released',release_generation=1,release_binding=%s,release_evidence=%s,release_mac=%s,revision=revision+1 WHERE id=%s",
            (
                Jsonb({"fixture": "prior binding"}),
                Jsonb({"fixture": "prior evidence"}),
                "7" * 64,
                identity,
            ),
        )
    current = r.service.inspect(r.actor, identity)["operation"]
    action(r, current, "rollback")
    claim = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    returned = r.service.complete_action(
        r.actor, claim, {"id": str(identity), "state": "rolled_back"}
    )["operation"]
    assert returned["phase"] == "rolled_back" and returned["releaseState"] == "none"
    assert returned["releaseGeneration"] == 1 and returned["releaseEvidenceSha256"] is None
    assert returned["installedState"] == "held" and "repin" in returned["allowedActions"]
    if final_action != "reconcile":
        with r.env.db.transaction(r.actor) as conn:
            conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET release_phase='repinned',revision=revision+1 WHERE id=%s",
                (identity,),
            )
    current = r.service.inspect(r.actor, identity)["operation"]
    action(r, current, "postflight" if final_action == "lost_postflight_ack" else final_action)
    claim = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET release_phase='released',release_generation=2,release_binding=%s,release_evidence=%s,release_mac=%s,revision=revision+1 WHERE id=%s",
            (
                Jsonb({"fixture": "restored binding"}),
                Jsonb({"fixture": "restored evidence"}),
                "8" * 64,
                identity,
            ),
        )
    if final_action == "lost_postflight_ack":
        current = r.service.inspect(r.actor, identity)["operation"]
        assert (
            current["releaseState"] == "released" and current["hostAction"]["status"] == "running"
        )
        assert current["allowedActions"] == ["reconcile"]
        action(r, current, "reconcile")
        with pytest.raises(StateMismatchError):
            r.service.validate_claim(r.actor, claim)
        claim = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    completed = r.service.complete_action(
        r.actor, claim, {"id": str(identity), "state": "rolled_back", "releaseState": "released"}
    )["operation"]
    assert completed["releaseState"] == "released" and completed["releaseGeneration"] == 2
    assert (
        completed["releaseEvidenceSha256"] is not None and completed["installedState"] == "released"
    )


def test_failed_action_keeps_its_uncertain_outcome_until_an_explicit_new_decision(rig):
    r = rig
    claim, _ = build_claim(r)
    current = r.service.fail(r.actor, claim, "execution_unknown", uncertain=True)["operation"]
    assert (
        current["phase"] == "indeterminate"
        and r.service.claim(r.actor, r.agent, "build", setup_token=TOKEN) is None
    )
    action(r, current, "reconcile")
    recovery = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    failed = r.service.fail(r.actor, recovery, "lease_held", uncertain=True)["operation"]
    assert failed["hostAction"] == {"action": "reconcile", "status": "indeterminate"}
    assert r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN) is None
    assert failed["allowedActions"] == ["reconcile"]


def test_rotated_key_can_only_authorize_terminal_native_abandonment(rig):
    r = rig
    old, _ = build_claim(r)
    r.token[0] = "rotated-tool-operator-capability-" + "z" * 48
    current = r.service.inspect(r.actor, old["id"])["operation"]
    assert not current["recipe"]["approvalValid"] and current["allowedActions"] == ["reconcile"]
    action(r, current, "reconcile")
    claim = r.service.claim(r.actor, r.agent, "action", setup_token=r.token[0])
    assert claim["abandon_only"] is True
    r.service.validate_claim(r.actor, claim)
    with pytest.raises(StateMismatchError):
        r.service.validate_claim(r.actor, {**claim, "abandon_only": False})
    with pytest.raises(StateMismatchError):
        r.service.validate_claim(r.actor, old)
    with pytest.raises(ScopeDeniedError):
        r.service.request(
            r.actor, r.recipe_id, "c" * 40, "new-under-old-recipe", setup_token=r.token[0]
        )
    done = r.service.complete_action(
        r.actor, claim, {"id": str(old["id"]), "state": "aborted", "candidate_retained": True}
    )["operation"]
    assert done["phase"] == "cancelled" and not done["recipe"]["approvalValid"]


def test_running_host_action_can_be_fenced_without_replaying_its_old_claim(rig):
    r = rig
    build, _ = build_claim(r)
    current = r.service.fail(r.actor, build, "execution_unknown", uncertain=True)["operation"]
    action(r, current, "reconcile")
    old = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    current = r.service.inspect(r.actor, old["id"])["operation"]
    assert current["hostAction"]["status"] == "running" and current["allowedActions"] == [
        "reconcile"
    ]
    action(r, current, "reconcile")
    with pytest.raises(StateMismatchError):
        r.service.validate_claim(r.actor, old)
    with pytest.raises(StateMismatchError):
        r.service.complete_action(r.actor, old, {"id": str(old["id"]), "state": "aborted"})
    new = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    assert new["worker_token"] != old["worker_token"]
    with r.env.db.transaction(r.actor) as conn:
        r.service.validate_claim(r.actor, new, connection=conn)
    assert (
        r.service.complete_action(r.actor, new, {"id": str(new["id"]), "state": "aborted"})[
            "operation"
        ]["phase"]
        == "cancelled"
    )


def test_recovery_fences_s11_and_interrupted_postflight_cannot_be_replayed(rig):
    r = rig
    current = prepared(r)
    identity = UUID(current["id"])
    r.service.decide(
        r.actor,
        identity,
        current["revision"],
        current["evidenceSha256"],
        True,
        "promote",
        setup_token=TOKEN,
    )
    approvals = UiToolUpgradeStore(r.env.db)
    promotion = approvals.claim(r.actor, agent_id=r.agent, setup_token=TOKEN, operation_id=identity)
    approvals.validate_claim(r.actor, promotion, setup_token=TOKEN)
    current = r.service.inspect(r.actor, identity)["operation"]
    action(r, current, "reconcile")
    with pytest.raises(StateMismatchError):
        approvals.validate_claim(r.actor, promotion, setup_token=TOKEN)
    with pytest.raises(StateMismatchError):
        approvals.finish(
            r.actor, operation=identity, worker_token=promotion["worker_token"], active=True
        )
    recovery = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET state='awaiting_repin',release_phase='repinned',revision=revision+1 WHERE id=%s",
            (identity,),
        )
    r.service.complete_action(r.actor, recovery, {"id": str(identity), "state": "awaiting_repin"})
    current = r.service.inspect(r.actor, identity)["operation"]
    action(r, current, "postflight")
    old = r.service.claim(r.actor, r.agent, "action", setup_token=TOKEN)
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET release_phase='testing',postflight_token=%s,revision=revision+1 WHERE id=%s",
            (uuid4(), identity),
        )
    current = r.service.inspect(r.actor, identity)["operation"]
    assert current["allowedActions"] == ["rollback"]
    queued = action(r, current, "rollback")
    assert queued["releaseState"] == "indeterminate"
    with r.env.db.transaction(r.actor) as conn:
        assert (
            conn.execute(
                "SELECT postflight_token FROM ui_tool_upgrade_approvals WHERE id=%s", (identity,)
            ).fetchone()["postflight_token"]
            is None
        )
    with pytest.raises(StateMismatchError):
        r.service.validate_claim(r.actor, old)
    assert "postflight" not in queued["allowedActions"]


def test_populated_v40_migration_preserves_legacy_approval_history(app_env, tmp_path):
    schema, migrations = "tool_acquisition_upgrade", application_migrations()
    assert migrate(app_env.admin_dsn, schema, migrations[:40]) == tuple(range(1, 41))
    workspace = provision_workspace(
        app_env.admin_dsn, schema, "legacy tool upgrades", sandbox_root=tmp_path / "legacy"
    )
    role = urlsplit(app_env.db.dsn).username
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    db, identity, agent = Database(app_env.db.dsn, schema), uuid4(), uuid4()
    with db.transaction(actor) as conn:
        conn.execute(
            "INSERT INTO ui_tool_upgrade_approvals(workspace_id,id,principal_id,agent_id,slug,old_commit,new_commit,evidence_sha256,suite_sha256,passed_tests,tests) VALUES(%s,%s,%s,%s,'legacy-tool',%s,%s,%s,%s,2,%s)",
            (
                workspace,
                identity,
                actor.principal_id,
                agent,
                "a" * 40,
                "b" * 40,
                "c" * 64,
                "d" * 64,
                Jsonb(
                    [
                        {"name": "smoke", "kind": "smoke", "elapsedMs": 1, "passed": True},
                        {"name": "unit", "kind": "unit", "elapsedMs": 1, "passed": True},
                    ]
                ),
            ),
        )
        UiToolUpgradeStore._audit(
            conn, actor, identity, "published", "none", "awaiting_approval", 0, "c" * 64
        )
        before = dict(conn.execute("SELECT * FROM ui_tool_upgrade_approvals").fetchone())
        audit = conn.execute("SELECT * FROM ui_tool_upgrade_audit").fetchall()
    with admin_connection(app_env.admin_dsn, schema) as conn:
        ledger = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert migrate(app_env.admin_dsn, schema, migrations[:41]) == tuple(range(1, 42))
    grant_application_role(app_env.admin_dsn, schema, role)
    assert migrate(app_env.admin_dsn, schema, migrations[:41]) == tuple(range(1, 42))
    with db.transaction(actor) as conn:
        after = dict(conn.execute("SELECT * FROM ui_tool_upgrade_approvals").fetchone())
        assert all(after[key] == value for key, value in before.items())
        assert after["acquisition_phase"] == "none" and after["recipe_id"] is None
        assert conn.execute("SELECT * FROM ui_tool_upgrade_audit").fetchall() == audit
        assert conn.execute("SELECT * FROM ui_tool_acquisition_recipes").fetchall() == []
    with admin_connection(app_env.admin_dsn, schema) as conn:
        assert (
            conn.execute(
                "SELECT * FROM schema_migrations WHERE version<=40 ORDER BY version"
            ).fetchall()
            == ledger
        )
    legacy = UiToolUpgradeStore(db)
    assert legacy.inspect(actor, identity)["oldCommit"] == "a" * 40
    approved = legacy.decide(
        actor,
        identity,
        expected_revision=0,
        evidence_sha256="c" * 64,
        approve=True,
        setup_token=TOKEN,
    )
    assert approved["state"] == "approved"
    assert (
        legacy.claim(actor, agent_id=agent, setup_token=TOKEN, operation_id=identity)["id"]
        == identity
    )
