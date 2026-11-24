"""Real restricted PostgreSQL/checked queue with an explicitly mocked provider.

No live model, protected holdout, independent gold or installed host is qualified.
"""

from uuid import UUID, uuid4

import pytest

from potbot.controller.model_route_outcomes import ModelRouteOutcomeService
from potbot.controller.model_routing import ModelRoutingService
from potbot.domain.errors import (
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from tests.integration.test_model_route_bindings import accept, allocate, check, launch, rig

pytestmark = pytest.mark.postgres


def services(r):
    return ModelRouteOutcomeService(r.env.db), ModelRouteOutcomeService(r.env.other_db)


def complete(r):
    # The existing mock streams "model output", deliberately invalid for JSON.
    d = allocate(r)
    checked = check(r, d)
    queued = launch(r, d, checked)
    result = r.runner.run_once(r.actor, "outcome-worker")
    assert result["id"] == queued["job"]["id"] and result["state"] == "succeeded"
    return UUID(d["id"]), result


def review(service, r, decision, verdict, review_id=None):
    return service.review(
        r.reviewer,
        decision,
        review_id or uuid4(),
        verdict=verdict,
        evidence_sha256="c" * 64,
        evaluator_revision="independent-fixture/v1",
    )


def test_capture_and_explicit_share_preserve_private_job_and_prompt_boundary(app_env):
    r = rig(app_env)
    owner, reviewer = services(r)
    try:
        d, result = complete(r)
        outcome = owner.capture(r.actor, d)
        assert outcome == owner.capture(r.actor, d)
        assert outcome["output_text"] == "model output" and outcome["job_id"] == result["id"]
        assert outcome["assessment"]["completion_confirmed"]
        assert outcome["assessment"]["literal_contract"]["syntax_valid"] is False
        with pytest.raises(ScopeDeniedError):
            reviewer.inspect(r.reviewer, d)
        with pytest.raises(StateMismatchError):
            owner.share(r.actor, d, expected_outcome_sha256="0" * 64)
        owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
        assert reviewer.inspect(r.reviewer, d) == outcome
        with app_env.other_db.transaction(r.reviewer) as conn:
            for table in (
                "jobs",
                "job_events",
                "hosted_requests",
                "hosted_invocations",
                "hosted_invocation_events",
                "model_route_compiled_prompts",
            ):
                assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0, (
                    table
                )
        assert "Find discriminating evidence" not in str(outcome)
        assert len(r.requests) == 1
    finally:
        r.http.close()


def test_review_is_designated_append_only_idempotent_and_never_format_repair(app_env):
    r = rig(app_env)
    owner, reviewer = services(r)
    try:
        d, _ = complete(r)
        outcome = owner.capture(r.actor, d)
        with pytest.raises(ScopeDeniedError):
            owner.review(
                r.actor,
                d,
                uuid4(),
                verdict="acceptable",
                evidence_sha256="c" * 64,
                evaluator_revision="owner",
            )
        owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
        with pytest.raises(StateMismatchError):
            review(reviewer, r, d, "acceptable")
        rid = uuid4()
        assert review(reviewer, r, d, "inconclusive", rid) == review(
            reviewer, r, d, "inconclusive", rid
        )
        with pytest.raises(StateMismatchError):
            review(reviewer, r, d, "regression", rid)
        for table in (
            "model_route_outcomes",
            "model_route_outcome_shares",
            "model_route_outcome_reviews",
        ):
            with pytest.raises((ScopeDeniedError, RevisionConflictError)):
                with app_env.db.transaction(r.actor) as conn:
                    conn.execute(f"DELETE FROM {table}")
    finally:
        r.http.close()


def test_regression_fences_queued_dispatch_even_after_new_baseline_acceptance(app_env):
    r = rig(app_env)
    owner, reviewer = services(r)
    try:
        d, _ = complete(r)
        waiting = allocate(r, "next-decision")
        checked = check(r, waiting)
        queued = launch(r, waiting, checked)
        outcome = owner.capture(r.actor, d)
        owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
        review(reviewer, r, d, "regression")
        assert r.routing.inspect(r.actor, r.policy_id)["baseline_review_id"] is None
        accept(r)  # Fresh baseline review cannot erase the outcome veto.
        assert r.routing.inspect(r.actor, r.policy_id)["baseline_review_id"] is None
        result = r.runner.run_once(r.actor, "fenced-worker")
        assert result["id"] == queued["job"]["id"] and result["state"] == "failed"
        assert result["dispatched_at"] is None and len(r.requests) == 1
        with pytest.raises((RevisionConflictError, StateMismatchError)):
            allocate(r, "blocked-new-allocation")
        summary = owner.summary(r.actor, r.policy_id)
        assert summary["allocations"] == 2 and summary["observed_decisions"] == 1
        assert summary["regression_fenced"] and summary["unobserved_decisions"] == 1
        assert (
            ModelRoutingService(app_env.db).inspect(r.actor, r.policy_id)["baseline_review_id"]
            is None
        )
        with pytest.raises(ScopeDeniedError):
            reviewer.summary(r.reviewer, r.policy_id)
    finally:
        r.http.close()


def test_terminal_capture_requires_the_original_job_and_cannot_be_forged_in_sql(app_env):
    from psycopg.types.json import Jsonb

    r = rig(app_env)
    owner, _ = services(r)
    try:
        waiting = allocate(r)
        with pytest.raises(StateMismatchError):
            owner.capture(r.actor, UUID(waiting["id"]))
        checked = check(r, waiting)
        queued = launch(r, waiting, checked)
        r.runner.run_once(r.actor, "original-worker")
        with app_env.db.transaction(r.actor) as conn:
            job = conn.execute(
                "SELECT * FROM jobs WHERE id=%s", (UUID(queued["job"]["id"]),)
            ).fetchone()
        with app_env.db.transaction(r.actor) as conn:
            original_receipts = conn.execute(
                "SELECT h.id,i.status,i.result->>'finish_reason' AS finish_reason FROM hosted_requests h JOIN hosted_invocations i ON i.workspace_id=h.workspace_id AND i.id=h.id WHERE h.job_id=%s ORDER BY h.ordinal",
                (job["id"],),
            ).fetchall()
        original_receipts = [{**dict(v), "id": str(v["id"])} for v in original_receipts]
        for text, state, receipts in [
            ("forged", job["state"], original_receipts),
            ("model output", "failed", original_receipts),
            (
                "model output",
                job["state"],
                [{"id": str(uuid4()), "status": "completed", "finish_reason": "stop"}],
            ),
        ]:
            with pytest.raises(RevisionConflictError):
                with app_env.db.transaction(r.actor) as conn:
                    conn.execute(
                        "INSERT INTO model_route_outcomes(workspace_id,decision_id,policy_id,principal_id,job_id,state,result,output_text,dispatched_at,finished_at,output_contract,outcome_sha256,provider_receipts) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            r.actor.workspace_id,
                            UUID(waiting["id"]),
                            r.policy_id,
                            r.actor.principal_id,
                            job["id"],
                            state,
                            Jsonb(job["result"]),
                            text,
                            job["dispatched_at"],
                            job["finished_at"],
                            "json_object_v1",
                            "a" * 64,
                            Jsonb(receipts),
                        ),
                    )
        assert owner.capture(r.actor, UUID(waiting["id"]))["output_text"] == "model output"
    finally:
        r.http.close()


def test_reply_loss_stays_indeterminate_unreviewable_as_success_and_never_replayed(app_env):
    import httpx

    r = rig(app_env)
    owner, reviewer = services(r)
    try:
        decision = allocate(r)
        checked = check(r, decision)
        launch(r, decision, checked)

        def lost(request):
            raise httpx.ReadError("Synthetic reply lost after effect", request=request)

        r.callbacks.before_response = lost
        result = r.runner.run_once(r.actor, "reply-loss-worker")
        assert result["state"] == "indeterminate"
        d = UUID(decision["id"])
        outcome = owner.capture(r.actor, d)
        owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
        with pytest.raises(StateMismatchError):
            review(reviewer, r, d, "acceptable")
        # The database separately refuses an acceptable verdict on uncertainty,
        # even when the application assessment is deliberately bypassed.
        with pytest.raises(RevisionConflictError):
            with app_env.other_db.transaction(r.reviewer) as conn:
                conn.execute(
                    "INSERT INTO model_route_outcome_reviews(workspace_id,id,decision_id,policy_id,reviewer_id,verdict,outcome_sha256,evidence_sha256,evaluator_revision) VALUES(%s,%s,%s,%s,%s,'acceptable',%s,%s,'bypassed-service-fixture')",
                    (
                        r.actor.workspace_id,
                        uuid4(),
                        d,
                        r.policy_id,
                        r.reviewer.principal_id,
                        outcome["sha256"],
                        "c" * 64,
                    ),
                )
        review(reviewer, r, d, "inconclusive")
        assert ModelRouteOutcomeService(app_env.db).capture(r.actor, d) == outcome
        assert r.runner.run_once(r.actor, "restarted-worker") is None and len(r.requests) == 1
        with app_env.db.transaction(r.actor) as conn:
            states = conn.execute("SELECT status FROM budget_reservations").fetchall()
            assert {x["status"] for x in states} == {"indeterminate"}
        summary = owner.summary(r.actor, r.policy_id)
        assert summary["terminal_states"]["indeterminate"] == 1
        assert (
            summary["independent_reviews"]["inconclusive"] == 1 and not summary["holdout_qualified"]
        )
    finally:
        r.http.close()


def test_literal_complete_answer_needs_independent_review_and_never_becomes_qualification(
    app_env, monkeypatch
):
    from tests.integration import test_hosted_gateway as mock

    original = mock.wire
    monkeypatch.setattr(mock, "wire", lambda: original((' {"answer":42} ',)))
    r = rig(app_env)
    owner, reviewer = services(r)
    try:
        d, _ = complete(r)
        outcome = owner.capture(r.actor, d)
        assert outcome["assessment"]["literal_contract"]["syntax_valid"]
        assert owner.summary(r.actor, r.policy_id)["independent_reviews"]["unreviewed"] == 1
        owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
        review(reviewer, r, d, "acceptable")
        summary = owner.summary(r.actor, r.policy_id)
        assert summary["independent_reviews"]["acceptable"] == 1
        assert not summary["runtime_qualified"] and not summary["holdout_qualified"]
        assert r.routing.inspect(r.actor, r.policy_id)["baseline_review_id"] is not None
    finally:
        r.http.close()


def test_final_database_dispatch_fence_rejects_a_regression_after_worker_check(app_env):
    r = rig(app_env)
    owner, reviewer = services(r)
    try:
        d, _ = complete(r)
        waiting = allocate(r, "last-moment")
        checked = check(r, waiting)
        launch(r, waiting, checked)
        lease = r.jobs.claim(r.actor, "ready-worker")
        assert lease is not None
        from potbot.controller.operation_checks import runner_checks

        runner_checks(r.runner).revalidate(r.actor, lease)
        outcome = owner.capture(r.actor, d)
        owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
        review(reviewer, r, d, "regression")
        with pytest.raises(RevisionConflictError):
            r.jobs.mark_dispatched(r.actor, lease)
        assert r.jobs.inspect(r.actor, lease.job_id)["dispatched_at"] is None
        finished = r.jobs.finish(r.actor, lease, "failed", error_code="state_mismatch")
        assert finished["state"] == "failed" and len(r.requests) == 1
    finally:
        r.http.close()
