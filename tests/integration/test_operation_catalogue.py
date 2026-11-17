"""Real PostgreSQL/RLS catalogue joins; provider calls use explicit HTTP mocks."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from potbot.context.catalogue import RuntimeObject, register_runtime_objects
from potbot.context.rpc_contract import SkillRequest
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.operation_catalogue import OperationCatalogue
from potbot.controller.reasoning_runtime import PostgresReasoningStore
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.domain.execution_events import ExecutionEventInput
from potbot.persistence.database import provision_principal
from tests.integration.test_context_skills import CONTENT
from tests.integration.test_context_skills import create as create_skill
from tests.integration.test_context_skills import publish as publish_skill
from tests.integration.test_context_skills import setup as setup_skills
from tests.integration.test_hosted_gateway import KEY, generate, rig
from tests.integration.test_reasoning_budget_postgres import _prepare, _task

pytestmark = pytest.mark.postgres


def sibling(app_env):
    with app_env.other_db.transaction(app_env.other_context) as conn:
        role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    return provision_principal(
        app_env.admin_dsn, app_env.schema, role, app_env.context.workspace_id
    )


def register(db, actor, reference, *, kind="tool"):
    with db.transaction(actor) as conn:
        return register_runtime_objects(conn, actor, (RuntimeObject(kind, reference, "a" * 64),))[0]


def model(app_env, name):
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) VALUES(%s,%s,%s,'reasoning')",
            (app_env.context.workspace_id, uuid4(), name),
        )


def test_catalogue_enforces_same_workspace_principal_context_and_workspace_rls(app_env):
    actor = app_env.context
    other = sibling(app_env)
    own = register(app_env.db, actor, "own.operation")
    foreign = register(app_env.other_db, other, "foreign.operation")
    remote = register(app_env.other_db, app_env.other_context, "cross-workspace.operation")
    model(app_env, "shared-model")
    service = OperationCatalogue(app_env.db)
    listed = service.list(actor)
    references = {row["reference"] for row in listed["items"]}
    assert {"own.operation", "model:shared-model"} <= references
    assert references.isdisjoint({"foreign.operation", "cross-workspace.operation"})
    assert any(row["contextObjectId"] == str(own) for row in listed["items"])
    assert str(foreign) not in json.dumps(listed)
    assert str(remote) not in json.dumps(listed)
    others = OperationCatalogue(app_env.other_db).list(other)
    foreign_id = next(
        row["id"] for row in others["items"] if row["reference"] == "foreign.operation"
    )
    with pytest.raises(ScopeDeniedError):
        service.inspect(actor, foreign_id)
    remote_references = {
        row["reference"]
        for row in OperationCatalogue(app_env.other_db).list(app_env.other_context)["items"]
    }
    assert "cross-workspace.operation" in remote_references
    assert remote_references.isdisjoint(
        {"own.operation", "foreign.operation", "model:shared-model"}
    )


def test_catalogue_records_release_blockers_without_treating_db_mac_as_runtime_admission(app_env):
    actor = app_env.context
    register(app_env.db, actor, "shared-tool:example")
    operation = uuid4()
    with app_env.db.transaction(actor) as conn:
        conn.execute(
            "INSERT INTO ui_tool_upgrade_approvals(workspace_id,id,principal_id,agent_id,slug,old_commit,new_commit,"
            "evidence_sha256,suite_sha256,passed_tests,tests,state,release_phase,release_generation,release_binding,release_evidence,release_mac) "
            "VALUES(%s,%s,%s,%s,'example',%s,%s,%s,%s,2,'[{},{}]'::jsonb,'active','released',1,"
            "'{\"private_path\":\"/etc/catalogue-secret-canary\"}'::jsonb,'{}'::jsonb,%s)",
            (
                actor.workspace_id,
                operation,
                actor.principal_id,
                uuid4(),
                "a" * 40,
                "b" * 40,
                "c" * 64,
                "d" * 64,
                "e" * 64,
            ),
        )
    service = OperationCatalogue(app_env.db)
    released = next(
        row for row in service.list(actor)["items"] if row["reference"] == "shared-tool:example"
    )
    assert released["state"] == "installed_unqualified"
    assert released["release"]["releaseState"] == "released"
    assert "catalogue-secret-canary" not in json.dumps(released)
    with app_env.db.transaction(actor) as conn:
        conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET state='awaiting_repin',release_phase='failed',"
            "release_mac=NULL,release_evidence=NULL,revision=revision+1 WHERE id=%s",
            (operation,),
        )
    held = service.inspect(actor, released["id"])["item"]
    assert held["state"] == "degraded" and "postflight_failed" in held["reasons"]
    assert not any(
        row["reference"] == "shared-tool:example"
        for row in OperationCatalogue(app_env.other_db).list(sibling(app_env))["items"]
    )


def test_catalogue_pagination_uses_one_scope_snapshot_and_expires_on_visible_change(app_env):
    for name in ("alpha", "beta", "gamma"):
        register(app_env.db, app_env.context, name)
    model(app_env, "other-model")
    service = OperationCatalogue(app_env.db)
    first = service.list(app_env.context, kind="tool", limit=1)
    register(app_env.other_db, sibling(app_env), "foreign.private-change")
    second = service.list(app_env.context, kind="tool", limit=1, after=first["nextCursor"])
    assert second["snapshotSha256"] == first["snapshotSha256"]
    assert "foreign.private-change" not in json.dumps(second)
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert (
        service.inspect(app_env.context, first["items"][0]["id"])["snapshotSha256"]
        == first["snapshotSha256"]
    )
    assert service.list(app_env.context, query="alpha")["snapshotSha256"] == first["snapshotSha256"]
    with pytest.raises(RevisionConflictError):
        service.list(app_env.context, kind="model", after=first["nextCursor"])
    with app_env.db.transaction(app_env.context) as conn:
        register_runtime_objects(conn, app_env.context, (RuntimeObject("tool", "alpha", "b" * 64),))
    with pytest.raises(RevisionConflictError):
        service.list(app_env.context, kind="tool", after=first["nextCursor"])


def test_catalogue_joins_real_hosted_descriptor_usage_once_and_never_writes(app_env):
    value = rig(app_env, prompt="catalogue-private-prompt-canary")
    try:
        outcome = generate(value)
        assert outcome["status"] == "completed"
        model(app_env, "fixture-model")
        service = OperationCatalogue(app_env.db)
        with app_env.db.transaction(app_env.context) as conn:
            before = conn.execute(
                "SELECT (SELECT count(*) FROM budget_usage) AS usage,"
                "(SELECT count(*) FROM command_results) AS commands,"
                "(SELECT count(*) FROM workspace_context_documents) AS documents,"
                "(SELECT count(*) FROM syn_execution_events) AS events"
            ).fetchone()
        listed = service.list(app_env.context)
        entry = next(row for row in listed["items"] if row["reference"] == "model:fixture-model")
        assert len(entry["recentInvocations"]) == 1
        assert entry["recentInvocations"][0]["source"] == "hosted"
        assert entry["accounting"]["settledCount"] == 1
        assert (
            next(row for row in entry["accounting"]["totals"] if row["unit"] == "tokens")["value"]
            == "15"
        )
        service.inspect(app_env.context, entry["id"])
        service.list(app_env.context)
        with app_env.db.transaction(app_env.context) as conn:
            after = conn.execute(
                "SELECT (SELECT count(*) FROM budget_usage) AS usage,"
                "(SELECT count(*) FROM command_results) AS commands,"
                "(SELECT count(*) FROM workspace_context_documents) AS documents,"
                "(SELECT count(*) FROM syn_execution_events) AS events"
            ).fetchone()
        assert before == after
        assert KEY not in json.dumps(
            listed
        ) and "catalogue-private-prompt-canary" not in json.dumps(listed)
        other = OperationCatalogue(app_env.other_db).list(sibling(app_env))["items"]
        foreign_view = next(row for row in other if row["reference"] == "model:fixture-model")
        assert (
            foreign_view["recentInvocations"] == [] and foreign_view["accounting"]["totals"] == []
        )
    finally:
        value.http.close()


def test_catalogue_reasoning_usage_has_direct_owner_and_no_tool_name_collision(app_env):
    task = _task(app_env)
    run = uuid4().hex
    token = PostgresReasoningStore(app_env.db).start(app_env.context, run)
    journal, ticket = _prepare(app_env, task, run, token)
    journal.dispatch(app_env.context, ticket, token)
    journal.observe(
        app_env.context,
        ticket,
        {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        elapsed_ms=220,
        rate_provider="openai",
        rate_model="reasoner-test",
    )
    journal.settle_observed(app_env.context, ticket.identity)
    model(app_env, "reasoner-test")
    register(app_env.db, app_env.context, "reasoner-test")
    listed = OperationCatalogue(app_env.db).list(app_env.context)["items"]
    tool = next(row for row in listed if row["reference"] == "reasoner-test")
    selected = next(row for row in listed if row["reference"] == "model:reasoner-test")
    assert tool["recentInvocations"] == [] and tool["accounting"]["totals"] == []
    assert selected["recentInvocations"][0]["id"] == str(ticket.identity)
    assert selected["accounting"]["settledCount"] == 1
    other = next(
        row
        for row in OperationCatalogue(app_env.other_db).list(sibling(app_env))["items"]
        if row["reference"] == "model:reasoner-test"
    )
    assert other["recentInvocations"] == [] and other["accounting"]["totals"] == []


def test_catalogue_cog_history_keeps_latest_attempt_observation_and_principal_scope(app_env):
    actor, other = app_env.context, sibling(app_env)
    reference = "workflow.example"
    register(app_env.db, actor, reference, kind="workflow")
    register(app_env.other_db, other, reference, kind="workflow")
    own_run, foreign_run = uuid4(), uuid4()
    for db, selected, run in ((app_env.db, actor, own_run), (app_env.other_db, other, foreign_run)):
        events = ExecutionEventService(db)
        previous = None
        for stage in ("started", "completed"):
            previous = events.append(
                selected,
                ExecutionEventInput(
                    event_type_ref="synny://type/workflow-execution",
                    event_type_version="1",
                    subject_type_ref="syn://type/example-cog",
                    subject_version="1",
                    run_id=run,
                    trigger_kind="api_request" if previous is None else "parent_event",
                    trigger_ref="catalogue-fixture" if previous is None else previous["event_ref"],
                    trigger_input_sha256="a" * 64,
                    emitter_run_id=run,
                    stage=stage,
                    parent_event_id=None if previous is None else UUID(previous["id"]),
                    parent_run_id=None if previous is None else run,
                    telemetry_json=json.dumps(
                        {
                            "implementation": {"kind": "workflow", "operation_ref": reference},
                            "private_prompt": "catalogue-telemetry-canary",
                        }
                    ),
                ),
            )
    item = next(
        row
        for row in OperationCatalogue(app_env.db).list(actor)["items"]
        if row["reference"] == reference
    )
    assert len(item["recentInvocations"]) == 1
    assert item["recentInvocations"][0]["id"] == str(own_run)
    assert item["recentInvocations"][0]["state"] == "completed"
    assert str(foreign_run) not in json.dumps(item)
    assert "catalogue-telemetry-canary" not in json.dumps(item)


def test_catalogue_published_skill_uses_release_revision_and_ignores_unpublished_draft(app_env):
    _, store, session = setup_skills(app_env)
    _, skill = create_skill(store, session)
    released = publish_skill(store, session, skill)
    service = OperationCatalogue(app_env.db)
    before = service.list(app_env.context, kind="skill")
    draft = store.execute(
        session,
        SkillRequest(
            uuid4(),
            "skills.save",
            skill_id=UUID(skill["skill_id"]),
            content=CONTENT + "\nPrivate unpublished-draft-canary.\n",
            expected_revision=1,
        ),
    )
    assert draft["revision"] == 2
    after = service.list(app_env.context, kind="skill")
    assert after == before
    entry = after["items"][0]
    assert entry["state"] == "source_only"
    assert entry["fingerprintSha256"] == released["sha256"]
    assert entry["sources"] == [
        {
            "family": "published_skill",
            "recordId": skill["skill_id"],
            "revision": 1,
            "sha256": released["sha256"],
        }
    ]
    shared_view = OperationCatalogue(app_env.other_db).list(sibling(app_env), kind="skill")
    assert shared_view["items"] == after["items"]
    assert "unpublished-draft-canary" not in json.dumps(shared_view)
