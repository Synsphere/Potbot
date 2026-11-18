"""Real selected-operation SQL/queue/worker admission, synthetic provider only.

The hosted transport is the existing explicit MockTransport fixture. These
checks establish lifecycle, provenance and accounting behavior, not model
quality, live network service availability or deployed native qualification.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from potbot.controller.task_discovery import TaskDiscoveryService
from potbot.controller.task_discovery_ranking import normalize_constraints
from potbot.controller.task_execution import TaskExecutionService
from potbot.domain.errors import DomainError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.domain.tasks import TaskStatus
from potbot.persistence.database import admin_connection, provision_principal
from tests.integration.test_operation_checks import TOKEN, effects, hosted
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres


def selected_hosted(app_env, tmp_path, monkeypatch, *, acceptance=None):
    value = hosted(app_env)
    _, artifacts, _, _, state, _ = setup(app_env, tmp_path)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        # This disposable workspace seeds many model identities with the same
        # generation descriptor. Isolate one owner so top-ten SHA ties cannot
        # hide the intended fixture; provider/model names remain unranked.
        connection.execute("ALTER TABLE model_catalog DISABLE TRIGGER USER")
        connection.execute(
            "DELETE FROM model_catalog WHERE workspace_id=%s", (value.actor.workspace_id,)
        )
        connection.execute("ALTER TABLE model_catalog ENABLE TRIGGER USER")
    with app_env.db.transaction(value.actor) as connection:
        connection.execute(
            "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) VALUES(%s,%s,'fixture-model','reasoning')",
            (value.actor.workspace_id, uuid4()),
        )
    value.artifacts, value.state = artifacts, state
    value.token = [TOKEN]
    value.runner.operation_setup_token = lambda: value.token[0]
    monkeypatch.setattr(
        "potbot.controller.task_execution.native_skill_setup_token", lambda: value.token[0]
    )
    value.discovery = TaskDiscoveryService(
        app_env.db, setup_token=lambda: value.token[0], artifacts=artifacts
    )
    current = value.discovery.context(value.actor, value.task_id, state["id"])["context"]
    search = value.discovery.create(
        value.actor,
        {
            "episodeId": str(value.task_id),
            "stateId": str(state["id"]),
            "contextSha256": current["contextSha256"],
            "query": "generate text response",
            "constraints": normalize_constraints(),
            "mode": "lexical",
            "limit": 10,
            "embedding": None,
            "confirm": True,
        },
        "selected-search",
        setup_token=TOKEN,
    )["search"]
    result = value.discovery.result(value.actor, UUID(search["resultId"]))["result"]
    identity = content_hash(
        {"workspace": str(value.actor.workspace_id), "kind": "model", "reference": value.reference}
    )
    candidate = next((item for item in result["items"] if item["ownerId"] == identity), None)
    assert candidate is not None, result
    value.selection = value.discovery.select(
        value.actor,
        UUID(search["id"]),
        search["revision"],
        search["bindingSha256"],
        candidate["id"],
        "select-model",
        setup_token=TOKEN,
    )["selection"]
    value.selection_id = UUID(value.selection["id"])
    value.service = TaskExecutionService(
        app_env.db, setup_token=lambda: value.token[0], artifacts=artifacts
    )
    value.acceptance = acceptance or {
        "schema": "potbot-task-result-oracle/v1",
        "assertions": [{"op": "equals", "pointer": "/text", "value": "model output"}],
    }
    return value


def queue_check(value, *, key="selected-check", acceptance=None):
    return value.service.check(
        value.actor,
        value.selection_id,
        value.selection["sha256"],
        value.spec,
        value.acceptance if acceptance is None else acceptance,
        key,
        setup_token=value.token[0],
    )


def checked(value, *, key="selected-check"):
    queued = queue_check(value, key=key)
    result = value.runner.run_once(value.actor, "selected-check-worker")
    assert result["id"] == queued["job"]["id"]
    assert result["state"] == "succeeded", result
    assert result["result"]["operation_check"]["eligible"] is True, result
    return queued


def launch_checked(value, check, *, key="selected-launch"):
    return value.service.launch(
        value.actor,
        value.selection_id,
        UUID(check["job"]["id"]),
        check["binding"]["sha256"],
        key,
        setup_token=value.token[0],
    )


def pause(value):
    task = value.tasks.inspect(value.actor, value.task_id)
    return value.tasks.transition(
        value.actor,
        value.task_id,
        task["update_token"],
        TaskStatus.PAUSED,
        "fixture pause",
        idempotency_key="selected-pause",
    )


def test_complete_selected_hosted_check_launch_and_exact_replay(app_env, tmp_path, monkeypatch):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    try:
        before = effects(value)
        context = value.service.context(value.actor, value.selection_id)
        assert context["supported"] and context["current"]
        assert value.service.jobs(value.actor, value.selection_id)["items"] == []
        assert effects(value) == before and value.requests == []
        check = checked(value)
        assert effects(value) == before and value.requests == []
        launched = launch_checked(value, check)
        complete = value.runner.run_once(value.actor, "selected-run-worker")
        assert complete["id"] == launched["job"]["id"] and complete["state"] == "succeeded", (
            complete
        )
        assert len(value.requests) == 1
        assert (
            value.service.execution_binding(value.actor, UUID(complete["id"])) == check["binding"]
        )
        pause(value)
        assert queue_check(value) == check
        assert launch_checked(value, check) == launched
        assert len(value.requests) == 1
        page = value.service.jobs(value.actor, value.selection_id, limit=1)
        assert page["hasMore"] and page["nextCursor"]
        second = value.service.jobs(
            value.actor, value.selection_id, limit=1, after=page["nextCursor"]
        )
        assert not second["hasMore"] and second["nextCursor"] is None
        assert {page["items"][0]["job"]["id"], second["items"][0]["job"]["id"]} == {
            check["job"]["id"],
            complete["id"],
        }
    finally:
        value.http.close()


def test_changed_acceptance_same_key_conflicts_and_two_launches_cannot_duplicate(
    app_env, tmp_path, monkeypatch
):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    try:
        check = checked(value)
        oracle = deepcopy(value.acceptance)
        oracle["assertions"][0]["value"] = "changed"
        with pytest.raises(RevisionConflictError):
            queue_check(value, acceptance=oracle)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(launch_checked, value, check, key="launch-" + str(index))
                for index in range(2)
            ]
            successes, conflicts = [], []
            for future in futures:
                try:
                    successes.append(future.result())
                except RevisionConflictError as error:
                    conflicts.append(error)
        assert len(successes) == len(conflicts) == 1
        assert value.requests == []
        result = value.runner.run_once(value.actor, "one-selected-run")
        assert result["state"] == "succeeded" and len(value.requests) == 1
        assert value.runner.run_once(value.actor, "no-second-selected-run") is None
    finally:
        value.http.close()


@pytest.mark.parametrize("change", ["task", "catalogue", "setup"])
def test_authority_drift_before_launch_worker_dispatches_nothing(
    app_env, tmp_path, monkeypatch, change
):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    try:
        check = checked(value)
        launch_checked(value, check)
        if change == "task":
            pause(value)
        elif change == "catalogue":
            with app_env.db.transaction(value.actor) as connection:
                connection.execute(
                    "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) VALUES(%s,%s,'another-model','reasoning')",
                    (value.actor.workspace_id, uuid4()),
                )
        else:
            value.token[0] += "rotated"
        before = effects(value)
        result = value.runner.run_once(value.actor, "stale-selected-run")
        assert result["state"] in {"failed", "cancelled"}, result
        assert result["dispatched_at"] is None and value.requests == []
        assert effects(value) == before
    finally:
        value.http.close()


def test_final_dispatch_rechecks_after_worker_eligibility_and_budget_reservation(
    app_env, tmp_path, monkeypatch
):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    try:
        check = checked(value)
        launch_checked(value, check)
        reserve = value.gateway.budgets.reserve

        def late_change(*args, **kwargs):
            receipt = reserve(*args, **kwargs)
            pause(value)
            return receipt

        monkeypatch.setattr(value.gateway.budgets, "reserve", late_change)
        result = value.runner.run_once(value.actor, "dispatch-fence")
        assert result["state"] in {"failed", "cancelled"}, result
        assert result["dispatched_at"] is None and value.requests == []
    finally:
        value.http.close()


def test_historical_reads_and_cancel_survive_rotated_setup_and_paused_task(
    app_env, tmp_path, monkeypatch
):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    try:
        check = checked(value)
        launch = launch_checked(value, check)
        value.token[0] = None
        pause(value)
        current = value.service.context(value.actor, value.selection_id)
        assert not current["current"]
        assert (
            value.service.inspect(value.actor, value.selection_id, UUID(launch["job"]["id"]))[
                "job"
            ]["state"]
            == "queued"
        )
        with pytest.raises(ScopeDeniedError):
            value.service.execution_binding(value.actor, UUID(launch["job"]["id"]))
        cancelled = value.service.cancel(
            value.actor, value.selection_id, UUID(launch["job"]["id"]), "safe-cancel"
        )
        assert cancelled["job"]["state"] == "cancelled" and value.requests == []
        assert (
            value.service.cancel(
                value.actor, value.selection_id, UUID(launch["job"]["id"]), "safe-cancel"
            )
            == cancelled
        )
    finally:
        value.http.close()


def test_selection_job_binding_is_private_immutable_and_cannot_be_cross_joined(
    app_env, tmp_path, monkeypatch
):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    try:
        check = queue_check(value)
        with app_env.other_db.transaction(app_env.other_context) as connection:
            role = connection.execute("SELECT session_user AS role").fetchone()["role"]
        peer = provision_principal(
            app_env.admin_dsn, app_env.schema, role, value.actor.workspace_id
        )
        other = TaskExecutionService(app_env.other_db, setup_token=lambda: TOKEN)
        with pytest.raises(ScopeDeniedError):
            other.inspect(peer, value.selection_id, UUID(check["job"]["id"]))
        with app_env.other_db.transaction(peer) as connection:
            assert (
                connection.execute(
                    "SELECT id FROM jobs WHERE id=%s", (UUID(check["job"]["id"]),)
                ).fetchone()
                is None
            )
        for statement in (
            "UPDATE jobs SET task_selection_id=NULL,task_execution_binding=NULL WHERE id=%s",
            "UPDATE jobs SET task_execution_binding=jsonb_set(task_execution_binding,'{acceptanceSha256}',to_jsonb(repeat('0',64))) WHERE id=%s",
        ):
            with pytest.raises(DomainError):
                with app_env.db.transaction(value.actor) as connection:
                    connection.execute(statement, (UUID(check["job"]["id"]),))
        with pytest.raises(ScopeDeniedError):
            value.service.inspect(value.actor, uuid4(), UUID(check["job"]["id"]))
        with pytest.raises(RevisionConflictError):
            value.service.launch(
                value.actor,
                value.selection_id,
                UUID(check["job"]["id"]),
                "0" * 64,
                "bad-binding",
                setup_token=TOKEN,
            )
    finally:
        value.http.close()


def selected_native(app_env, tmp_path, monkeypatch):
    from potbot.controller.tasks import TaskService
    from potbot.domain.syn import SynRef, SynType, parse_type_ref
    from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
    from potbot.semantics.service import SynService
    from tests.integration.test_operation_checks import native
    from tests.unit.test_operation_discovery_metadata import cog_snapshot

    value = native(app_env, tmp_path, monkeypatch)
    value.env = app_env
    value.tasks = TaskService(app_env.db)
    value.token = [TOKEN]
    value.runner.operation_setup_token = lambda: value.token[0]
    value.artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    value.jobs.task_execution_artifacts = value.artifacts
    monkeypatch.setattr(
        "potbot.controller.task_execution.native_skill_setup_token", lambda: value.token[0]
    )
    ports = tuple(
        SynType(SynRef(ref), label, (parse_type_ref("synny://type/potbot-data-record"),), None, ())
        for ref, label in (("syn://type/in", "Fixture input"), ("syn://type/out", "Fixture output"))
    )
    snapshot = cog_snapshot(
        reference="syn://cog/fixture",
        input_type="syn://type/in",
        output_type="syn://type/out",
        extra_types=ports,
    )
    SynService(app_env.db, value.artifacts).import_snapshot(
        value.actor, app_env.repository_id, "selected-native", snapshot
    )
    value.discovery = TaskDiscoveryService(
        app_env.db, setup_token=lambda: TOKEN, artifacts=value.artifacts
    )
    current = value.discovery.context(value.actor, value.task_id, value.state["id"])["context"]
    search = value.discovery.create(
        value.actor,
        {
            "episodeId": str(value.task_id),
            "stateId": str(value.state["id"]),
            "contextSha256": current["contextSha256"],
            "query": "inspect retained repository files",
            "constraints": normalize_constraints(),
            "mode": "lexical",
            "limit": 10,
            "embedding": None,
            "confirm": True,
        },
        "native-selected-search",
        setup_token=TOKEN,
    )["search"]
    result = value.discovery.result(value.actor, UUID(search["resultId"]))["result"]
    identity = content_hash(
        {
            "workspace": str(value.actor.workspace_id),
            "kind": "cog",
            "reference": "syn://cog/fixture",
        }
    )
    candidate = next(item for item in result["items"] if item["ownerId"] == identity)
    value.selection = value.discovery.select(
        value.actor,
        UUID(search["id"]),
        search["revision"],
        search["bindingSha256"],
        candidate["id"],
        "native-select",
        setup_token=TOKEN,
    )["selection"]
    value.selection_id = UUID(value.selection["id"])
    value.service = TaskExecutionService(
        app_env.db, setup_token=lambda: value.token[0], artifacts=value.artifacts
    )
    value.acceptance = {
        "schema": "potbot-task-result-oracle/v1",
        "assertions": [{"op": "equals", "pointer": "/value/checked", "value": True}],
    }
    return value


@pytest.mark.parametrize("alias", [False, True])
def test_native_selected_ports_real_grants_queue_and_verified_typed_result(
    app_env, tmp_path, monkeypatch, alias
):
    value = selected_native(app_env, tmp_path, monkeypatch)
    if alias:
        value.spec["input"]["type_ref"] = "synny://type/in"
    check = checked(value)
    assert value.executions == [] and value.mesh._grants == {}
    assert check["binding"]["capabilities"]["outputTypes"] == ["syn://type/out"]
    launched = launch_checked(value, check)
    finished = value.runner.run_once(value.actor, "selected-native-run")
    assert finished["state"] == "succeeded", finished
    assert value.executions == ["syn://thing/fixture"]
    report = value.service.result(value.actor, value.selection_id, UUID(launched["job"]["id"]))
    assert report["verification"]["status"] == "passed", report["verification"]
    assert report["verification"]["resultKind"] == "typed_cog"
    assert report["verification"]["outputSha256"] and report["verification"]["evidenceSha256"]


def test_native_selected_wrong_declared_output_fails_before_grant_or_budget(
    app_env, tmp_path, monkeypatch
):
    from dataclasses import replace

    value = selected_native(app_env, tmp_path, monkeypatch)
    operation = value.dispatcher.operations["syn://thing/fixture"]
    value.dispatcher.operations["syn://thing/fixture"] = replace(
        operation, binding=replace(operation.binding, output_type_ref="syn://type/in")
    )
    queued = queue_check(value)
    before = effects(value)
    result = value.runner.run_once(value.actor, "wrong-selected-port")
    assert result["state"] == "succeeded", result
    assert result["result"]["operation_check"]["eligible"] is False
    assert {"code": "selected_canonical_ports", "status": "blocked"} in result["result"][
        "operation_check"
    ]["checks"]
    assert value.executions == [] and value.mesh._grants == {} and effects(value) == before
    with pytest.raises(RevisionConflictError):
        launch_checked(value, queued)


def test_consumer_upgrade_fences_new_launch_but_exact_check_replay_and_cancel_remain(
    app_env, tmp_path, monkeypatch
):
    value = selected_hosted(app_env, tmp_path, monkeypatch)
    try:
        check = checked(value)
        monkeypatch.setattr(
            "potbot.controller.task_execution.execution_consumer_sha256", lambda: "0" * 64
        )
        assert queue_check(value) == check
        with pytest.raises(DomainError):
            value.service.execution_binding(value.actor, UUID(check["job"]["id"]))
        with pytest.raises(RevisionConflictError):
            launch_checked(value, check)
        assert (
            value.service.cancel(
                value.actor, value.selection_id, UUID(check["job"]["id"]), "cancel-old-consumer"
            )["job"]["state"]
            == "succeeded"
        )
        assert value.requests == []
    finally:
        value.http.close()


def test_large_private_acceptance_pages_continue_within_response_bound(
    app_env, tmp_path, monkeypatch
):
    from potbot.domain.serialization import canonical_json

    acceptance = {
        "schema": "potbot-task-result-oracle/v1",
        "assertions": [
            {"op": "contains", "pointer": "/text", "value": str(index) + "x" * 3900}
            for index in range(16)
        ],
    }
    value = selected_hosted(app_env, tmp_path, monkeypatch, acceptance=acceptance)
    try:
        identities = {
            queue_check(value, key="large-check-" + str(index))["job"]["id"] for index in range(17)
        }
        first = value.service.jobs(value.actor, value.selection_id, limit=25)
        assert first["hasMore"] and 0 < len(first["items"]) < 17
        assert len(canonical_json(first).encode()) < 1_100_000
        second = value.service.jobs(
            value.actor, value.selection_id, limit=25, after=first["nextCursor"]
        )
        assert not second["hasMore"]
        assert {item["job"]["id"] for item in first["items"] + second["items"]} == identities
        assert value.requests == []
    finally:
        value.http.close()


def test_migration_45_to_46_preserves_owned_legacy_jobs_without_making_selection_grants(app_env):
    from potbot.controller.jobs import JobService
    from potbot.controller.tasks import TaskService
    from potbot.persistence.database import Database, RepositoryStore, migrate, provision_workspace
    from potbot.persistence.schema import application_migrations
    from tests.integration.test_hosted_gateway import hosted_contract

    schema = "before_task_execution"
    revisions = application_migrations()[:46]
    assert revisions[-1].version == 46
    migrate(app_env.admin_dsn, schema, tuple(item for item in revisions if item.version <= 45))
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    workspace = provision_workspace(app_env.admin_dsn, schema, "legacy owned execution")
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    db = Database(app_env.db.dsn, schema)
    repository = RepositoryStore(db).create(actor, name="legacy", source_uri="fixture://legacy")
    task = TaskService(db).submit(actor, repository["id"], hosted_contract(), "legacy-task")
    identity = uuid4()
    with admin_connection(app_env.admin_dsn, schema) as connection:
        revision = connection.execute(
            "SELECT active_contract_id FROM episodes WHERE id=%s", (UUID(task["task_id"]),)
        ).fetchone()["active_contract_id"]
        connection.execute(
            "INSERT INTO jobs(workspace_id,id,episode_id,contract_revision_id,job_type,input,selected_models,owner_principal_id) VALUES(%s,%s,%s,%s,'model.generate','{}'::jsonb,'[]'::jsonb,%s)",
            (workspace, identity, UUID(task["task_id"]), revision, actor.principal_id),
        )
    assert migrate(app_env.admin_dsn, schema, revisions)[-1] == 46
    assert migrate(app_env.admin_dsn, schema, revisions)[-1] == 46
    public = JobService(db).inspect(actor, identity)
    assert public["id"] == str(identity) and public["state"] == "queued"
    with db.transaction(actor) as connection:
        row = connection.execute(
            "SELECT task_selection_id,task_execution_binding FROM jobs WHERE id=%s", (identity,)
        ).fetchone()
        assert row == {"task_selection_id": None, "task_execution_binding": None}
    with pytest.raises(DomainError):
        TaskExecutionService(db, setup_token=lambda: TOKEN).execution_binding(actor, identity)
