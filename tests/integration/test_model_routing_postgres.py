"""Persistent shadow routing on real unprivileged PostgreSQL connections.

Worker reports are explicit synthetic fixtures, not live model qualification.
These tests qualify the durable routing/queue boundary, never a model package.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from potbot.controller.model_routing import ModelRoutingService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.jobs import CHECK_JOB
from potbot.domain.operation_workbench import CHECK_SCHEMA, request_digest
from potbot.domain.serialization import content_hash
from tests.integration.test_operation_workbench import TOKEN, peer, rig

pytestmark = pytest.mark.postgres


def routes(env, *, activate=True):
    r = rig(env)
    r.routing = ModelRoutingService(env.db)
    r.reviewer = peer(env)
    r.review_service = ModelRoutingService(env.other_db)
    r.policy_id = uuid4()
    target = {
        "operation_id": r.operation,
        "runtime_identity_sha256": "b" * 64,
        "executor": {
            "job_type": r.request["job_type"],
            "selected_models": r.request["selected_models"],
            "input": {},
        },
    }
    candidate = {**target, "runtime_identity_sha256": "c" * 64}
    r.created = r.routing.create(
        env.context,
        r.policy_id,
        reviewer_id=r.reviewer.principal_id,
        role="reasoning",
        atomic_kind="fixture_kind",
        baseline=target,
        candidate=candidate,
        prompt_contract_sha256="d" * 64,
        evaluation_plan_sha256="e" * 64,
    )
    if activate:
        accept(r)
        r.routing.transition(env.context, r.policy_id, expected_epoch=1, stage="shadow")
    return r


def accept(r, *, accepted=True, seconds=3600):
    with r.env.other_db.transaction(r.reviewer) as conn:
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    return r.review_service.review_baseline(
        r.reviewer,
        r.policy_id,
        uuid4(),
        accepted=accepted,
        evidence_sha256="f" * 64,
        valid_until=now + timedelta(seconds=seconds),
    )


def allocation(r, key="case-1"):
    return r.routing.allocate(r.env.context, r.policy_id, key, r.request)


def check(r, decision):
    queued = r.routing.check(
        r.env.context, UUID(decision["id"]), "check-" + decision["id"], setup_token=TOKEN
    )
    lease = r.jobs.claim(
        r.env.context, "synthetic-check-worker", kinds=(CHECK_JOB,), lease_seconds=300
    )
    assert str(lease.job_id) == queued["job"]["id"]
    now = datetime.now(UTC)
    bindings = {
        "requestSha256": request_digest(r.request),
        "contractRevisionId": str(lease.contract_revision_id),
    }
    report = {
        "schema": CHECK_SCHEMA,
        "operationId": r.operation,
        "eligible": True,
        "checks": [{"code": "synthetic_fixture", "status": "passed"}],
        "bindings": bindings,
        "bindingSha256": content_hash(bindings),
        "checkedAt": now.isoformat(),
        "expiresAt": (now + timedelta(seconds=120)).isoformat(),
    }
    r.jobs.finish(r.env.context, lease, "succeeded", result={"operation_check": report})
    return lease.job_id


def launch(r, decision):
    check_id = check(r, decision)
    return r.service.launch(
        r.env.context,
        r.operation,
        check_id,
        r.request,
        "launch-" + decision["id"],
        setup_token=TOKEN,
    )


def test_restart_idempotency_and_shadow_does_not_invoke_candidate(app_env):
    r = routes(app_env)
    first = allocation(r)
    restarted = ModelRoutingService(app_env.db)
    assert restarted.allocate(app_env.context, r.policy_id, "case-1", r.request) == first
    assert first["selected"] == "baseline" and not first["candidate_invoked"]
    changed = {**r.request, "input": {**r.request["input"], "prompt": "another"}}
    with pytest.raises(StateMismatchError):
        restarted.allocate(app_env.context, r.policy_id, "case-1", changed)
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_independent_acceptance_is_required_and_cannot_be_forged(app_env):
    r = routes(app_env, activate=False)
    with pytest.raises(RevisionConflictError):
        r.routing.transition(app_env.context, r.policy_id, expected_epoch=1, stage="shadow")
    with pytest.raises(ScopeDeniedError):
        r.routing.review_baseline(
            app_env.context,
            r.policy_id,
            uuid4(),
            accepted=True,
            evidence_sha256="a" * 64,
            valid_until=datetime.now(UTC) + timedelta(hours=1),
        )
    forged = WorkspaceContext(app_env.context.workspace_id, r.reviewer.principal_id)
    with pytest.raises(ScopeDeniedError):
        r.routing.inspect(forged, r.policy_id)
    accept(r)
    assert (
        r.routing.transition(app_env.context, r.policy_id, expected_epoch=1, stage="shadow")[
            "epoch"
        ]
        == 2
    )


def test_reviewer_can_see_manifest_but_not_private_decisions_or_jobs(app_env):
    r = routes(app_env)
    d = allocation(r)
    launch(r, d)
    assert r.review_service.inspect(r.reviewer, r.policy_id)["manifest"] == r.created["manifest"]
    with pytest.raises(ScopeDeniedError):
        r.review_service.decision(r.reviewer, UUID(d["id"]))
    with app_env.other_db.transaction(r.reviewer) as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    with pytest.raises(ScopeDeniedError):
        r.review_service.inspect(app_env.other_context, r.policy_id)


@pytest.mark.parametrize("stage", ["canary", "promoted"])
def test_unimplemented_candidate_stages_are_database_blocked(app_env, stage):
    r = routes(app_env)
    with pytest.raises(InvalidInputError):
        r.routing.transition(app_env.context, r.policy_id, expected_epoch=2, stage=stage)
    with pytest.raises((InvalidInputError, RevisionConflictError)):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE model_route_policies SET stage=%s,epoch=epoch+1 WHERE id=%s",
                (stage, r.policy_id),
            )


def test_one_check_wins_concurrent_requests_and_lost_reply_reuses_it(app_env):
    r = routes(app_env)
    d = allocation(r)

    def submit(_):
        try:
            return r.routing.check(app_env.context, UUID(d["id"]), str(uuid4()), setup_token=TOKEN)[
                "job"
            ]["id"]
        except RevisionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(submit, range(2)))
    assert len([x for x in replies if x]) == 1
    retained = ModelRoutingService(app_env.db).decision(app_env.context, UUID(d["id"]))
    assert retained["check_job_id"] in replies
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1


def test_epoch_change_fences_checked_job_insertion_and_preserves_allocation(app_env):
    r = routes(app_env)
    d = allocation(r)
    r.routing.transition(app_env.context, r.policy_id, expected_epoch=2, stage="blocked")
    with pytest.raises(RevisionConflictError):
        r.routing.check(app_env.context, UUID(d["id"]), "stale", setup_token=TOKEN)
    r.routing.transition(app_env.context, r.policy_id, expected_epoch=3, stage="shadow")
    assert allocation(r)["id"] == d["id"]
    with pytest.raises(RevisionConflictError):
        r.routing.check(app_env.context, UUID(d["id"]), "still-stale", setup_token=TOKEN)


def test_revocation_fences_launch_and_reacceptance_does_not_revalidate_old_decision(app_env):
    r = routes(app_env)
    d = allocation(r)
    check_id = check(r, d)
    accept(r, accepted=False)
    with pytest.raises(RevisionConflictError):
        r.service.launch(
            app_env.context, r.operation, check_id, r.request, "revoked", setup_token=TOKEN
        )
    accept(r)
    with pytest.raises(RevisionConflictError):
        r.service.launch(
            app_env.context, r.operation, check_id, r.request, "superseded", setup_token=TOKEN
        )


@pytest.mark.parametrize("invalidate", ["revocation", "epoch", "expiry"])
def test_final_possible_dispatch_marker_is_fenced(app_env, invalidate):
    r = routes(app_env)
    if invalidate == "expiry":
        accept(r, seconds=10)
    d = allocation(r)
    launched = launch(r, d)
    lease = r.jobs.claim(app_env.context, "synthetic-model-worker", lease_seconds=300)
    assert str(lease.job_id) == launched["job"]["id"]
    if invalidate == "revocation":
        accept(r, accepted=False)
    elif invalidate == "epoch":
        r.routing.transition(app_env.context, r.policy_id, expected_epoch=2, stage="blocked")
    else:
        # Let the actual PostgreSQL clock expire the immutable review; no forged timestamps.
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "SELECT pg_sleep(greatest(0,extract(epoch FROM (valid_until-clock_timestamp())))+0.05) FROM model_route_reviews WHERE workspace_id=%s AND policy_id=%s ORDER BY revision DESC LIMIT 1",
                (app_env.context.workspace_id, r.policy_id),
            )
    with pytest.raises(RevisionConflictError):
        r.jobs.mark_dispatched(app_env.context, lease)
    assert r.jobs.inspect(app_env.context, lease.job_id)["dispatched_at"] is None


def test_revoking_inflight_work_does_not_replay_or_release_held_reservations(app_env):
    from psycopg.types.json import Jsonb

    r = routes(app_env)
    d = allocation(r)
    launched = launch(r, d)
    lease = r.jobs.claim(app_env.context, "synthetic-model-worker", lease_seconds=300)
    r.jobs.mark_dispatched(app_env.context, lease)
    reservation = uuid4()
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "INSERT INTO budget_reservations(workspace_id,id,episode_id,reservation_key,amounts,status) VALUES(%s,%s,%s,'synthetic-possible-effect',%s,'indeterminate')",
            (app_env.context.workspace_id, reservation, lease.episode_id, Jsonb({"calls": 1})),
        )
    accept(r, accepted=False)
    r.jobs.finish(app_env.context, lease, "indeterminate")
    again = allocation(r)
    assert again["id"] == d["id"] and again["job_id"] == launched["job"]["id"]
    current = ModelRoutingService(app_env.db).decision(app_env.context, UUID(d["id"]))
    assert current["job"]["state"] == "indeterminate"
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT status FROM budget_reservations WHERE id=%s", (reservation,)
            ).fetchone()["status"]
            == "indeterminate"
        )
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 2


def test_committing_revocation_wins_against_a_waiting_dispatch(app_env):
    from threading import Event

    r = routes(app_env)
    d = allocation(r)
    launch(r, d)
    lease = r.jobs.claim(app_env.context, "synthetic-model-worker", lease_seconds=300)
    started = Event()

    def dispatch():
        started.set()
        return r.jobs.mark_dispatched(app_env.context, lease)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with app_env.other_db.transaction(r.reviewer) as conn:
            conn.execute(
                "INSERT INTO model_route_reviews(workspace_id,id,policy_id,reviewer_id,revision,accepted,evidence_sha256,valid_until) VALUES(%s,%s,%s,%s,1,false,%s,clock_timestamp()+interval '1 hour')",
                (
                    app_env.context.workspace_id,
                    uuid4(),
                    r.policy_id,
                    r.reviewer.principal_id,
                    "f" * 64,
                ),
            )
            future = pool.submit(dispatch)
            assert started.wait(3)
            # The possible-dispatch update cannot commit past the held review lock.
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
        with pytest.raises(RevisionConflictError):
            future.result(timeout=5)
    assert r.jobs.inspect(app_env.context, lease.job_id)["dispatched_at"] is None
