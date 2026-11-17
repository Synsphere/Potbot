"""Real PostgreSQL lifecycle, private ownership, replay and artifact verification.

These service tests use explicit worker-produced report fixtures. Actual worker
eligibility and zero-effect checks are covered separately by operation-check tests.
"""

from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot.controller.jobs import JobService
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.controller.tasks import TaskService
from potbot.domain.errors import DomainError, RevisionConflictError, ScopeDeniedError
from potbot.domain.jobs import CHECK_JOB
from potbot.domain.operation_workbench import CHECK_SCHEMA, request_digest
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import (
    Database,
    RepositoryStore,
    admin_connection,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.workspace_sandbox import load_workspace_sandbox
from tests.integration.test_hosted_gateway import hosted_contract

pytestmark = pytest.mark.postgres
TOKEN = "operation-setup-capability-1234567890-abcdefgh"


def rig(env):
    service = OperationWorkbench(env.db)
    tasks = TaskService(env.db)
    task = tasks.submit(env.context, env.repository_id, hosted_contract(), "workbench-task")
    task = tasks.start_generation(
        env.context,
        UUID(task["task_id"]),
        task["update_token"],
        "fixture admission",
        idempotency_key="workbench-start",
    )
    with env.db.transaction(env.context) as conn:
        conn.execute(
            "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) "
            "VALUES(%s,%s,'fixture-model','reasoning')",
            (env.context.workspace_id, uuid4()),
        )
    operation = next(
        row
        for row in service.catalogue.list(env.context)["items"]
        if row["reference"] == "model:fixture-model"
    )
    request = {
        "job_type": "model.generate",
        "episode_id": task["task_id"],
        "input": {
            "prompt": "private-workbench-prompt-canary",
            "max_output_tokens": 16,
            "timeout_seconds": 30,
        },
        "selected_models": [{"provider": "openai", "model": "fixture-model", "role": "reasoning"}],
    }
    return SimpleNamespace(
        env=env,
        service=service,
        jobs=service.jobs,
        task=task,
        request=request,
        operation=operation["id"],
    )


def peer(env):
    with env.other_db.transaction(env.other_context) as conn:
        role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    return provision_principal(env.admin_dsn, env.schema, role, env.context.workspace_id)


def completed_check(r, *, key="operation-check"):
    queued = r.service.check(r.env.context, r.operation, r.request, key, setup_token=TOKEN)
    lease = r.jobs.claim(r.env.context, "check-worker", kinds=(CHECK_JOB,), lease_seconds=300)
    assert lease is not None and str(lease.job_id) == queued["job"]["id"]
    now = datetime.now(UTC)
    bindings = {
        "requestSha256": request_digest(r.request),
        "contractRevisionId": str(lease.contract_revision_id),
        "policySha256": "b" * 64,
    }
    report = {
        "schema": CHECK_SCHEMA,
        "operationId": r.operation,
        "eligible": True,
        "checks": [{"code": "fixture_worker_report", "status": "passed"}],
        "bindings": bindings,
        "bindingSha256": content_hash(bindings),
        "checkedAt": now.isoformat(),
        "expiresAt": (now + timedelta(seconds=120)).isoformat(),
    }
    r.jobs.finish(r.env.context, lease, "succeeded", result={"operation_check": report})
    return lease.job_id, report


def test_workbench_contract_check_launch_result_and_exact_retry(app_env):
    r = rig(app_env)
    contract = r.service.describe(app_env.context, r.operation)
    assert contract["supported"] and contract["jobType"] == "model.generate"
    check_id, report = completed_check(r)
    checked = r.service.inspect(app_env.context, r.operation, check_id)["job"]
    assert checked["dispatched_at"] is None and checked["result"]["operation_check"] == report
    launched = r.service.launch(
        app_env.context, r.operation, check_id, r.request, "launch", setup_token=TOKEN
    )
    again = r.service.launch(
        app_env.context, r.operation, check_id, r.request, "launch", setup_token=TOKEN
    )
    assert launched == again
    with pytest.raises(ScopeDeniedError):
        r.service.launch(
            app_env.context, "f" * 64, check_id, r.request, "launch", setup_token=TOKEN
        )
    run_id = UUID(launched["job"]["id"])
    assert launched["job"]["operation_check_id"] == str(check_id)
    assert launched["job"]["owner_principal_id"] == str(app_env.context.principal_id)
    lease = r.jobs.claim(app_env.context, "fixture-model-worker", lease_seconds=300)
    assert lease.operation_check_id == check_id and lease.operation_authorization
    r.jobs.mark_dispatched(app_env.context, lease)
    r.jobs.output(
        app_env.context,
        lease,
        "owned result",
        provider="openai",
        model="fixture-model",
        role="reasoning",
    )
    r.jobs.finish(
        app_env.context,
        lease,
        "succeeded",
        result={"model": "fixture-model", "completed_models": 1},
    )
    result = r.service.result(app_env.context, r.operation, run_id)
    assert result["output"] == "owned result" and result["artifact"] is None
    public = json.dumps(
        [
            contract,
            launched,
            checked,
            result,
            r.service.events(app_env.context, r.operation, run_id),
        ]
    )
    assert TOKEN not in public and "private-workbench-prompt-canary" not in public
    assert "operation_authorization" not in public and '"mac"' not in public
    with pytest.raises(RevisionConflictError):
        r.service.launch(
            app_env.context, r.operation, check_id, r.request, "another-run", setup_token=TOKEN
        )


def test_generic_job_endpoint_cannot_submit_check_and_checks_never_dispatch(app_env):
    r = rig(app_env)
    queued = r.service.check(app_env.context, r.operation, r.request, "check", setup_token=TOKEN)
    with app_env.db.transaction(app_env.context) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id=%s", (UUID(queued["job"]["id"]),)
        ).fetchone()
    payload = {
        "job_type": CHECK_JOB,
        "episode_id": str(row["episode_id"]),
        "input": row["input"],
        "selected_models": [],
    }
    with pytest.raises(ScopeDeniedError):
        r.jobs.submit(app_env.context, payload, "bypass")
    lease = r.jobs.claim(app_env.context, "check-worker", kinds=(CHECK_JOB,))
    with pytest.raises(ScopeDeniedError):
        r.jobs.mark_dispatched(app_env.context, lease)
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM budget_reservations").fetchone()["n"] == 0
    assert r.jobs.inspect(app_env.context, lease.job_id)["dispatched_at"] is None


def test_event_pages_bound_json_bytes_without_losing_output_or_cursor(app_env):
    r = rig(app_env)
    check_id, _ = completed_check(r)
    launched = r.service.launch(
        app_env.context, r.operation, check_id, r.request, "launch", setup_token=TOKEN
    )
    lease = r.jobs.claim(app_env.context, "fixture-worker", lease_seconds=300)
    r.jobs.mark_dispatched(app_env.context, lease)
    # Control characters require six encoded JSON bytes for each raw byte.
    chunks = ["\x01" * 65_536, "\x02" * 65_536, "\x03" * 65_536]
    for chunk in chunks:
        r.jobs.output(app_env.context, lease, chunk)
    r.jobs.finish(app_env.context, lease, "succeeded")
    identity = UUID(launched["job"]["id"])
    after, seen, output, pages = 0, [], [], 0
    while True:
        page = r.service.events(app_env.context, r.operation, identity, after=after)
        assert len(canonical_json(page).encode()) < 1_048_576
        assert sum(len(canonical_json(event).encode()) for event in page["items"]) <= 524_288
        for event in page["items"]:
            seen.append(event["sequence"])
            if event["event_type"] == "job.output":
                output.append(event["data"]["text"])
        pages += 1
        if not page["has_more"]:
            break
        assert page["next_cursor"] > after
        after = page["next_cursor"]
    assert pages >= 3 and len(seen) == len(set(seen))
    assert output == chunks
    result = r.service.result(app_env.context, r.operation, identity)
    assert result["truncated"] and len(result["output"].encode()) == 65_536


def test_all_job_paths_and_raw_rls_preserve_same_workspace_principal_privacy(app_env):
    r = rig(app_env)
    check_id, _ = completed_check(r)
    launched = r.service.launch(
        app_env.context, r.operation, check_id, r.request, "launch", setup_token=TOKEN
    )
    identity = UUID(launched["job"]["id"])
    other = peer(app_env)
    foreign = OperationWorkbench(app_env.other_db)
    foreign_jobs = foreign.jobs
    for read in (
        lambda: foreign_jobs.inspect(other, identity),
        lambda: foreign_jobs.events(other, identity),
        lambda: foreign_jobs.cancel(other, identity, "cancel"),
        lambda: foreign.inspect(other, r.operation, check_id),
        lambda: foreign.result(other, r.operation, identity),
        lambda: foreign.artifact(other, r.operation, identity),
        lambda: foreign.launch(other, r.operation, check_id, r.request, "steal", setup_token=TOKEN),
    ):
        with pytest.raises(ScopeDeniedError):
            read()
    assert foreign_jobs.list(other)["items"] == []
    assert foreign_jobs.events(other)["items"] == []
    assert foreign_jobs.queue(other)["total"] == 0
    assert foreign_jobs.claim(other, "foreign-worker", kinds=(CHECK_JOB, "model.generate")) is None
    with app_env.other_db.transaction(other) as conn:
        assert conn.execute("SELECT * FROM jobs").fetchall() == []
        assert conn.execute("SELECT * FROM job_events").fetchall() == []
    # Matching a workspace-scoped idempotency key cannot replay a peer result.
    with pytest.raises(ScopeDeniedError):
        foreign.check(other, r.operation, r.request, "operation-check", setup_token=TOKEN)
    with pytest.raises(ScopeDeniedError):
        foreign_jobs.inspect(app_env.other_context, identity)


def test_expired_check_blocks_new_launch_but_committed_retry_still_returns_original(app_env):
    from psycopg.types.json import Jsonb

    r = rig(app_env)
    check_id, report = completed_check(r)
    launched = r.service.launch(
        app_env.context, r.operation, check_id, r.request, "launch", setup_token=TOKEN
    )
    report["checkedAt"] = (datetime.now(UTC) - timedelta(seconds=180)).isoformat()
    report["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        conn.execute(
            "UPDATE jobs SET result=%s WHERE id=%s", (Jsonb({"operation_check": report}), check_id)
        )
    assert (
        r.service.launch(
            app_env.context, r.operation, check_id, r.request, "launch", setup_token=TOKEN
        )
        == launched
    )
    with pytest.raises(RevisionConflictError):
        r.service.launch(
            app_env.context, r.operation, check_id, r.request, "new-launch", setup_token=TOKEN
        )
    changed = {**r.request, "input": {**r.request["input"], "prompt": "changed"}}
    with pytest.raises(RevisionConflictError):
        r.service.launch(
            app_env.context, r.operation, check_id, changed, "launch", setup_token=TOKEN
        )


def test_rotated_setup_capability_and_changed_request_cannot_launch(app_env):
    r = rig(app_env)
    check_id, _ = completed_check(r)
    with pytest.raises(ScopeDeniedError):
        r.service.launch(
            app_env.context,
            r.operation,
            check_id,
            r.request,
            "rotated",
            setup_token="rotated-setup-capability-1234567890-abcdefgh",
        )
    with pytest.raises(RevisionConflictError):
        r.service.launch(
            app_env.context,
            r.operation,
            check_id,
            {**r.request, "input": {"prompt": "different"}},
            "changed",
            setup_token=TOKEN,
        )
    with pytest.raises(ScopeDeniedError):
        r.service.check(
            app_env.context,
            r.operation,
            {
                **r.request,
                "selected_models": [
                    {"provider": "openai", "model": "another-model", "role": "reasoning"}
                ],
            },
            "other-model",
            setup_token=TOKEN,
        )
    assert r.jobs.queue(app_env.context)["queued"] == 0


def test_concurrent_same_key_launches_have_one_durable_run(app_env):
    r = rig(app_env)
    check_id, _ = completed_check(r)

    def submit(_):
        return r.service.launch(
            app_env.context, r.operation, check_id, r.request, "same-key", setup_token=TOKEN
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(submit, range(4)))
    assert len({value["job"]["id"] for value in outcomes}) == 1
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE operation_check_id=%s", (check_id,)
            ).fetchone()["n"]
            == 1
        )


def test_cancelled_queued_check_remains_owned_and_cannot_launch(app_env):
    r = rig(app_env)
    queued = r.service.check(app_env.context, r.operation, r.request, "check", setup_token=TOKEN)
    identity = UUID(queued["job"]["id"])
    cancelled = r.service.cancel(app_env.context, r.operation, identity, "cancel")
    assert cancelled["job"]["state"] == "cancelled"
    assert r.service.cancel(app_env.context, r.operation, identity, "cancel") == cancelled
    assert r.jobs.claim(app_env.context, "check-worker", kinds=(CHECK_JOB,)) is None
    with pytest.raises(ScopeDeniedError):
        r.service.launch(
            app_env.context, r.operation, identity, r.request, "launch", setup_token=TOKEN
        )


def test_native_result_artifact_is_owned_verified_and_pageable(app_env):
    r = rig(app_env)
    check_id, _ = completed_check(r)
    launched = r.service.launch(
        app_env.context, r.operation, check_id, r.request, "launch", setup_token=TOKEN
    )
    lease = r.jobs.claim(app_env.context, "fixture-worker", lease_seconds=300)
    sandbox = load_workspace_sandbox(app_env.db, app_env.context)
    store = ArtifactStore(app_env.db, LocalArtifactBackend(sandbox.path("artifacts")))
    content = json.dumps({"type_ref": "syn://type/example", "value": "abc" * 100_000}).encode()
    artifact = store.put(app_env.context, content, "application/json")
    r.jobs.mark_dispatched(app_env.context, lease)
    r.jobs.finish(app_env.context, lease, "succeeded", result={"artifact_id": str(artifact["id"])})
    identity = UUID(launched["job"]["id"])
    result = r.service.result(app_env.context, r.operation, identity)
    assert result["artifactTruncated"] and result["artifactText"].startswith('{"type_ref"')
    pages, offset = [], 0
    while True:
        page = r.service.artifact(
            app_env.context, r.operation, identity, offset=offset, length=65_536
        )
        pages.append(base64.b64decode(page["dataBase64"]))
        if page["nextOffset"] is None:
            break
        offset = page["nextOffset"]
    assert b"".join(pages) == content
    assert result["artifact"]["sha256"] == artifact["digest"]
    other = peer(app_env)
    with pytest.raises(ScopeDeniedError):
        OperationWorkbench(app_env.other_db).artifact(other, r.operation, identity)
    with pytest.raises(DomainError):
        r.service.artifact(app_env.context, r.operation, identity, offset=-1)
    # The normal artifact verifier must reject bytes changed after publication.
    path = sandbox.path("artifacts") / artifact["locator"]
    path.write_bytes(b"x" * len(content))
    with pytest.raises(DomainError):
        r.service.artifact(app_env.context, r.operation, identity)


def test_migration_backfills_only_unambiguous_owner_and_freezes_new_bindings(app_env):
    from psycopg.types.json import Jsonb

    schema = "before_operation_workbench"
    # This is the populated 38 -> 39 backfill gate. Later additive migrations
    # have their own upgrade tests and must not change this historical fixture.
    revisions = application_migrations()[:39]
    assert revisions[-1].version == 39
    migrate(app_env.admin_dsn, schema, revisions[:38])
    with app_env.db.transaction(app_env.context) as conn:
        role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    with app_env.other_db.transaction(app_env.other_context) as conn:
        other_role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    workspace = provision_workspace(app_env.admin_dsn, schema, "legacy jobs")
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    other = provision_principal(app_env.admin_dsn, schema, other_role, workspace)
    db = Database(app_env.db.dsn, schema)
    repo = RepositoryStore(db).create(actor, name="fixture", source_uri="file:///fixture")
    task = TaskService(db).submit(actor, repo["id"], hosted_contract(), "task")
    known, unknown, ambiguous = uuid4(), uuid4(), uuid4()
    with admin_connection(app_env.admin_dsn, schema) as conn:
        revision = conn.execute(
            "SELECT active_contract_id FROM episodes WHERE id=%s", (UUID(task["task_id"]),)
        ).fetchone()["active_contract_id"]
        for identity in (known, unknown, ambiguous):
            conn.execute(
                "INSERT INTO jobs(workspace_id,id,episode_id,contract_revision_id,job_type,input,selected_models) "
                "VALUES(%s,%s,%s,%s,'model.generate','{}'::jsonb,'[]'::jsonb)",
                (workspace, identity, UUID(task["task_id"]), revision),
            )
        for identity, principal, key in (
            (known, actor.principal_id, "known"),
            (ambiguous, actor.principal_id, "ambiguous-a"),
            (ambiguous, other.principal_id, "ambiguous-b"),
        ):
            conn.execute(
                "INSERT INTO command_results(workspace_id,id,operation,idempotency_key,request_hash,result,principal_id) "
                "VALUES(%s,%s,'submit_api_job',%s,%s,%s,%s)",
                (workspace, uuid4(), key, "a" * 64, Jsonb({"id": str(identity)}), principal),
            )
    assert migrate(app_env.admin_dsn, schema, revisions)[-1] == 39
    assert migrate(app_env.admin_dsn, schema, revisions)[-1] == 39
    with admin_connection(app_env.admin_dsn, schema) as conn:
        owners = {
            row["id"]: row["owner_principal_id"]
            for row in conn.execute("SELECT id,owner_principal_id FROM jobs").fetchall()
        }
    assert owners == {known: actor.principal_id, unknown: None, ambiguous: None}
    assert [row["id"] for row in JobService(db).list(actor)["items"]] == [str(known)]
    for identity in (unknown, ambiguous):
        with pytest.raises(ScopeDeniedError):
            JobService(db).inspect(actor, identity)
