"""Canary allocations use real PostgreSQL and the existing checked worker."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from potbot.controller.model_route_bindings import worker_admission_identity
from potbot.controller.model_route_canaries import ModelRouteCanaries
from potbot.controller.model_routing import ModelRoutingService
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from tests.integration.test_model_route_bindings import check
from tests.integration.test_operation_checks import TOKEN, effects, hosted
from tests.integration.test_operation_workbench import peer
from tests.unit.test_model_route_bindings import facts, recipe

pytestmark = pytest.mark.postgres


@pytest.fixture
def pair(app_env):
    r = hosted(app_env)
    r.routing = ModelRoutingService(app_env.db)
    r.canaries = ModelRouteCanaries(app_env.db)
    r.workbench = OperationWorkbench(app_env.db)
    r.reviewer = peer(app_env)
    r.reviews = ModelRoutingService(app_env.other_db)
    r.ids = [uuid4(), uuid4()]
    r.canary_id = uuid4()
    r.request = []
    r.targets = []
    r.recipes = []
    models = ["fixture-model", "fixture-candidate"]
    r.gateway.policy = replace(
        r.policy, rules=(r.policy.rules[0], replace(r.policy.rules[0], model=models[1]))
    )
    with app_env.db.transaction(r.actor) as conn:
        for model in models:
            conn.execute(
                "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) VALUES(%s,%s,%s,'reasoning')",
                (r.actor.workspace_id, uuid4(), model),
            )
    operations = {
        item["reference"]: item["id"] for item in r.workbench.catalogue.list(r.actor)["items"]
    }
    for model in models:
        req = deepcopy(r.spec)
        del req["input"]["prompt"]
        req["selected_models"][0]["model"] = model
        target = {
            "operation_id": operations[f"model:{model}"],
            "runtime_identity_sha256": "a" * 64,
            "executor": {
                "job_type": "model.generate",
                "selected_models": req["selected_models"],
                "input": {},
            },
        }
        target["runtime_identity_sha256"] = worker_admission_identity(
            target, {"policySha256": r.gateway.policy.fingerprint}
        )
        r.targets.append(target)
        r.recipes.append({**recipe(), "model": model})
        r.request.append(req)
    r.review_until = datetime.now(UTC) + timedelta(hours=1)
    for index, policy_id in enumerate(r.ids):
        r.routing.create(
            r.actor,
            policy_id,
            reviewer_id=r.reviewer.principal_id,
            role="reasoning",
            atomic_kind="evidence_queries",
            baseline=r.targets[index],
            candidate=r.targets[1],
            prompt_contract_sha256=content_hash(r.recipes[index]),
            evaluation_plan_sha256="e" * 64,
        )
        r.routing.configure_prompt(r.actor, policy_id, r.recipes[index])
    review(r, 0)
    r.routing.transition(r.actor, r.ids[0], expected_epoch=1, stage="shadow")
    try:
        yield r
    finally:
        r.http.close()


def review(r, target, *, accepted=True):
    r.reviews.review_baseline(
        r.reviewer,
        r.ids[target],
        uuid4(),
        accepted=accepted,
        evidence_sha256="f" * 64,
        valid_until=r.review_until,
    )


def start(r, maximum=2):
    return r.canaries.start(
        r.actor,
        r.canary_id,
        baseline_id=r.ids[0],
        candidate_id=r.ids[1],
        sample_bps=1000,
        max_candidate_allocations=maximum,
        valid_until=r.review_until - timedelta(minutes=5),
    )


def accept_and_start(r, maximum=2):
    review(r, 1)
    r.routing.transition(r.actor, r.ids[1], expected_epoch=1, stage="shadow")
    return start(r, maximum)


def selected_keys(r, n):
    keys = []
    for i in range(1000):
        key = f"operation-{i}"
        prefix = hashlib.sha256(f"{r.canary_id}/{key}".encode()).digest()[:4]
        if int.from_bytes(prefix, "big") % 10_000 < 1000:
            keys.append(key)
            if len(keys) == n:
                return keys
    raise AssertionError("fixture identities yielded too few candidate keys")


def allocate(r, key):
    return ModelRouteCanaries(r.env.db).allocate(
        r.actor,
        r.canary_id,
        key,
        baseline_request=r.request[0],
        candidate_request=r.request[1],
        facts=facts(),
    )


def queue(r, decision):
    checked = check(r, decision)
    assert checked["result"]["operation_check"]["eligible"] is True
    operation = r.targets[decision["selected"] == "candidate"]["operation_id"]
    return r.workbench.launch(
        r.actor,
        operation,
        UUID(checked["id"]),
        decision["request"],
        "launch-" + decision["id"],
        setup_token=TOKEN,
    )["job"]


def test_canary_requires_independent_current_review_and_fences_bypass(pair):
    r = pair
    with pytest.raises(RevisionConflictError):
        start(r)
    with pytest.raises(ScopeDeniedError):
        r.routing.review_baseline(
            r.actor,
            r.ids[1],
            uuid4(),
            accepted=True,
            evidence_sha256="f" * 64,
            valid_until=r.review_until,
        )
    original = accept_and_start(r)
    assert start(r) == original
    with pytest.raises(StateMismatchError):
        start(r, 3)
    with pytest.raises(RevisionConflictError):
        r.routing.allocate_prompted(r.actor, r.ids[1], "bypass", r.request[1], facts())
    decision = allocate(r, selected_keys(r, 1)[0])
    assert decision["selected"] == "candidate"
    assert decision["request"]["selected_models"][0]["model"] == "fixture-candidate"
    review(r, 1, accepted=False)
    with pytest.raises(RevisionConflictError):
        r.routing.check(r.actor, UUID(decision["id"]), "revoked", setup_token=TOKEN)
    assert r.requests == []


def test_canary_budget_is_an_atomic_cap_and_replay_recovers_one_decision(pair):
    r = pair
    accept_and_start(r, 1)
    keys = selected_keys(r, 4)
    with ThreadPoolExecutor(max_workers=3) as workers:
        choices = list(workers.map(lambda key: allocate(r, key), keys[:3]))
    assert [choice["selected"] for choice in choices].count("candidate") == 1
    assert len({choice["id"] for choice in choices}) == 3
    assert [allocate(r, key) for key in keys[:3]] == choices
    assert allocate(r, keys[3])["selected"] == "baseline"
    with r.env.db.transaction(r.actor) as conn:
        assert (
            conn.execute("SELECT count(*) AS count FROM model_route_canary_allocations").fetchone()[
                "count"
            ]
            == 4
        )
    assert r.requests == []


def test_rollback_fences_queued_candidate_and_keeps_lost_reply_indeterminate(pair):
    r = pair
    accept_and_start(r)
    first_key, second_key, fresh_key = selected_keys(r, 3)
    first, second = allocate(r, first_key), allocate(r, second_key)

    def drop_after_request(request):
        assert json.loads(request.content)["model"] == "fixture-candidate"
        raise httpx.ReadError("fixture withheld the reply", request=request)

    r.callbacks.before_response = drop_after_request
    first_job = queue(r, first)
    result = r.runner.run_once(r.actor, "canary-worker")
    assert result["id"] == first_job["id"] and result["dispatched_at"] is not None
    assert result["state"] == "indeterminate"
    assert len(r.requests) == 1
    before = effects(r)
    second_job = queue(r, second)
    stopped = r.canaries.rollback(r.actor, r.canary_id)
    assert stopped["rolled_back_at"] is not None
    assert r.canaries.rollback(r.actor, r.canary_id) == stopped
    fenced = r.runner.run_once(r.actor, "canary-worker")
    assert fenced["id"] == second_job["id"] and fenced["dispatched_at"] is None
    assert allocate(r, first_key)["job_id"] == first_job["id"]
    assert r.runner.run_once(r.actor, "canary-worker") is None
    assert len(r.requests) == 1 and effects(r) == before

    baseline = allocate(r, fresh_key)
    assert baseline["selected"] == "baseline"
    r.callbacks.before_response = None
    baseline_job = queue(r, baseline)
    result = r.runner.run_once(r.actor, "baseline-after-rollback")
    assert result["id"] == baseline_job["id"] and result["state"] == "succeeded"
    assert [json.loads(item.content)["model"] for item in r.requests] == [
        "fixture-candidate",
        "fixture-model",
    ]
    assert effects(r)["usage"] == before["usage"] + 1
