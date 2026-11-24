"""Compiled route admission through the normal worker; HTTP is a fixture.

Actual PostgreSQL, signed workbench checks, controller and budget settlement.
No live provider, protected holdout or installed model qualification is claimed.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from potbot.controller.model_route_bindings import checked_route_binding, worker_admission_identity
from potbot.controller.model_routing import ModelRoutingService
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from tests.integration.test_operation_checks import TOKEN, effects, hosted
from tests.integration.test_operation_workbench import peer
from tests.unit.test_model_route_bindings import facts, recipe

pytestmark = pytest.mark.postgres


def rig(env, *, configure=True, activate=True):
    r = hosted(env)
    r.routing = ModelRoutingService(env.db)
    r.workbench = OperationWorkbench(env.db)
    r.reviewer = peer(env)
    r.review_service = ModelRoutingService(env.other_db)
    r.policy_id = uuid4()
    r.recipe = recipe()
    with env.db.transaction(env.context) as conn:
        conn.execute(
            "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) VALUES(%s,%s,'fixture-model','reasoning')",
            (env.context.workspace_id, uuid4()),
        )
    r.operation = next(
        i["id"]
        for i in r.workbench.catalogue.list(env.context)["items"]
        if i["reference"] == "model:fixture-model"
    )
    r.base = deepcopy(r.spec)
    del r.base["input"]["prompt"]
    r.target = {
        "operation_id": r.operation,
        "runtime_identity_sha256": "a" * 64,
        "executor": {
            "job_type": "model.generate",
            "selected_models": r.spec["selected_models"],
            "input": {},
        },
    }
    r.target["runtime_identity_sha256"] = worker_admission_identity(
        r.target, {"policySha256": r.policy.fingerprint}
    )
    r.routing.create(
        env.context,
        r.policy_id,
        reviewer_id=r.reviewer.principal_id,
        role="reasoning",
        atomic_kind="evidence_queries",
        baseline=r.target,
        candidate={**r.target, "runtime_identity_sha256": "b" * 64},
        prompt_contract_sha256=content_hash(r.recipe),
        evaluation_plan_sha256="e" * 64,
    )
    if configure:
        r.routing.configure_prompt(env.context, r.policy_id, r.recipe)
    if activate:
        accept(r)
        r.routing.transition(env.context, r.policy_id, expected_epoch=1, stage="shadow")
    return r


def accept(r):
    with r.env.other_db.transaction(r.reviewer) as conn:
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    return r.review_service.review_baseline(
        r.reviewer,
        r.policy_id,
        uuid4(),
        accepted=True,
        evidence_sha256="f" * 64,
        valid_until=now + timedelta(hours=1),
    )


def allocate(r, key="case"):
    return r.routing.allocate_prompted(r.actor, r.policy_id, key, r.base, facts())


def check(r, d):
    queued = r.routing.check(r.actor, UUID(d["id"]), "check-" + d["id"], setup_token=TOKEN)
    checked = r.runner.run_once(r.actor, "check-worker")
    assert checked["id"] == queued["job"]["id"] and checked["state"] == "succeeded"
    return checked


def launch(r, d, checked):
    return r.workbench.launch(
        r.actor,
        r.operation,
        UUID(checked["id"]),
        d["request"],
        "launch-" + d["id"],
        setup_token=TOKEN,
    )


def test_compiled_prompt_reaches_existing_worker_once_and_settles_existing_budget(app_env):
    r = rig(app_env)
    try:
        d = allocate(r)
        assert allocate(r) == d
        before = effects(r)
        checked = check(r, d)
        assert effects(r) == before and r.requests == []
        report = checked["result"]["operation_check"]
        assert report["eligible"] is True
        assert report["bindings"]["modelRuntimeSha256"] == r.target["runtime_identity_sha256"]
        assert len(report["bindings"]["modelPromptSha256"]) == 64
        assert "Find discriminating evidence" not in str(report)
        launched = launch(r, d, checked)
        result = r.runner.run_once(r.actor, "existing-model-worker")
        assert result["id"] == launched["job"]["id"] and result["state"] == "succeeded"
        assert len(r.requests) == 1 and effects(r)["usage"] == 1
        assert r.runner.run_once(r.actor, "existing-model-worker") is None
        retained = ModelRoutingService(app_env.db).decision(r.actor, UUID(d["id"]))
        assert retained["job_id"] == result["id"]
    finally:
        r.http.close()


def test_changed_worker_policy_is_refused_before_provider_effect(app_env):
    r = rig(app_env)
    try:
        d = allocate(r)
        checked = check(r, d)
        launch(r, d, checked)
        r.gateway.policy = replace(r.policy, max_output_tokens=16)
        result = r.runner.run_once(r.actor, "changed-worker")
        assert result["state"] == "failed" and result["dispatched_at"] is None
        assert r.requests == [] and effects(r)["reservations"] == 0
    finally:
        r.http.close()


def test_binding_is_recomputed_not_trusted_from_a_digest_or_mutable_fact(app_env):
    r = rig(app_env)
    try:
        d = allocate(r)
        with pytest.raises(StateMismatchError):
            changed = deepcopy(d["request"])
            changed["input"]["prompt"] += " changed"
            checked_route_binding(
                app_env.db, r.actor, d["id"], changed, {"policySha256": r.policy.fingerprint}
            )
        with pytest.raises(StateMismatchError):
            checked_route_binding(
                app_env.db, r.actor, d["id"], d["request"], {"policySha256": "0" * 64}
            )
        assert (
            checked_route_binding(
                app_env.db, r.actor, d["id"], d["request"], {"policySha256": r.policy.fingerprint}
            )["modelRuntimeSha256"]
            == r.target["runtime_identity_sha256"]
        )
        with pytest.raises(ScopeDeniedError):
            checked_route_binding(
                app_env.other_db,
                r.reviewer,
                d["id"],
                d["request"],
                {"policySha256": r.policy.fingerprint},
            )
        with app_env.other_db.transaction(r.reviewer) as conn:
            assert (
                conn.execute("SELECT count(*) AS n FROM model_route_compiled_prompts").fetchone()[
                    "n"
                ]
                == 0
            )
    finally:
        r.http.close()


def test_recipe_cannot_be_attached_after_review_or_changed_after_freeze(app_env):
    r = rig(app_env, configure=False, activate=False)
    try:
        accept(r)
        with pytest.raises(RevisionConflictError):
            r.routing.configure_prompt(r.actor, r.policy_id, r.recipe)
    finally:
        r.http.close()


def test_raw_allocations_cannot_bypass_a_frozen_prompt_recipe(app_env):
    r = rig(app_env)
    try:
        with pytest.raises(InvalidInputError):
            r.routing.allocate(r.actor, r.policy_id, "raw", r.spec)
        with app_env.db.transaction(r.actor) as conn:
            assert (
                conn.execute("SELECT count(*) AS n FROM model_route_decisions").fetchone()["n"] == 0
            )
    finally:
        r.http.close()


def test_compiler_facts_and_recipe_receipts_are_append_only(app_env):
    r = rig(app_env)
    try:
        d = allocate(r)
        for table, column, identity in [
            ("model_route_compiled_prompts", "decision_id", UUID(d["id"])),
            ("model_route_prompt_recipes", "policy_id", r.policy_id),
        ]:
            with pytest.raises((ScopeDeniedError, RevisionConflictError)):
                with app_env.db.transaction(r.actor) as conn:
                    conn.execute(
                        f"UPDATE {table} SET principal_id=%s WHERE {column}=%s",
                        (r.reviewer.principal_id, identity),
                    )
    finally:
        r.http.close()


@pytest.mark.parametrize("field", ["modelPromptSha256", "modelRuntimeSha256", "modelRoutingSha256"])
def test_eligible_check_without_exact_route_proof_cannot_insert_execution(app_env, field):
    from psycopg.types.json import Jsonb

    r = rig(app_env)
    try:
        d = allocate(r)
        checked = check(r, d)
        forged = deepcopy(checked["result"])
        del forged["operation_check"]["bindings"][field]
        forged["operation_check"]["bindingSha256"] = content_hash(
            forged["operation_check"]["bindings"]
        )
        # Explicit negative receipt fixture: shape/eligibility alone is insufficient.
        with app_env.db.transaction(r.actor) as conn:
            conn.execute(
                "UPDATE jobs SET result=%s WHERE workspace_id=%s AND id=%s",
                (Jsonb(forged), r.actor.workspace_id, UUID(checked["id"])),
            )
        with pytest.raises(RevisionConflictError):
            launch(r, d, checked)
        assert r.requests == [] and r.routing.decision(r.actor, UUID(d["id"]))["job_id"] is None
    finally:
        r.http.close()


def test_sql_inserted_allocation_without_compilation_cannot_claim_a_check(app_env):
    r = rig(app_env)
    try:
        d = allocate(r)
        incomplete = uuid4()
        with app_env.db.transaction(r.actor) as conn:
            conn.execute(
                "INSERT INTO model_route_decisions(workspace_id,id,policy_id,principal_id,epoch,review_id,allocation_key,request,episode_id,contract_revision_id,expires_at) SELECT workspace_id,%s,policy_id,principal_id,epoch,review_id,'incomplete-sql-fixture',request,episode_id,contract_revision_id,expires_at FROM model_route_decisions WHERE workspace_id=%s AND id=%s",
                (incomplete, r.actor.workspace_id, UUID(d["id"])),
            )
        with pytest.raises(RevisionConflictError):
            r.routing.check(r.actor, incomplete, "incomplete-check", setup_token=TOKEN)
        assert r.requests == []
    finally:
        r.http.close()


def test_lost_reply_keeps_original_compiled_job_and_indeterminate_reservation(app_env):
    import httpx

    r = rig(app_env)
    effects_observed = []
    try:
        d = allocate(r)
        checked = check(r, d)
        launched = launch(r, d, checked)

        def lose_reply(request):
            effects_observed.append("synthetic-effect")
            raise httpx.ReadError(
                "Synthetic response lost after the fixture effect", request=request
            )

        r.callbacks.before_response = lose_reply
        result = r.runner.run_once(r.actor, "reply-loss-worker")
        assert result["state"] == "indeterminate" and result["dispatched_at"] is not None
        recovered = ModelRoutingService(app_env.db).allocate_prompted(
            r.actor, r.policy_id, "case", r.base, facts()
        )
        assert recovered["id"] == d["id"] and recovered["job_id"] == launched["job"]["id"]
        assert r.runner.run_once(r.actor, "reply-loss-worker") is None
        assert effects_observed == ["synthetic-effect"] and len(r.requests) == 1
        with app_env.db.transaction(r.actor) as conn:
            statuses = conn.execute(
                "SELECT status FROM budget_reservations WHERE workspace_id=%s",
                (r.actor.workspace_id,),
            ).fetchall()
            assert statuses and {row["status"] for row in statuses} == {"indeterminate"}
    finally:
        r.http.close()
