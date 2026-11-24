"""Original checked jobs/SQL; model HTTP and native execution are explicit fixtures."""

from uuid import UUID

import httpx
import pytest

from potbot.controller.model_route_outcomes import ModelRouteOutcomeService
from potbot.controller.model_routing import ModelRoutingService
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from tests.integration import test_hosted_gateway as mock
from tests.integration.test_model_route_bindings import allocate, check, launch, rig

pytestmark = pytest.mark.postgres


def complete(r):
    decision = allocate(r)
    checked = check(r, decision)
    launch(r, decision, checked)
    result = r.runner.run_once(r.actor, "checked-reasoning-worker")
    return UUID(decision["id"]), result


def test_original_checked_reasoning_result_survives_route_block_without_replay(
    app_env, monkeypatch
):
    wire = mock.wire
    monkeypatch.setattr(mock, "wire", lambda: wire(('{"queries":["inspect original receipt"]}',)))
    r = rig(app_env)
    try:
        d, job = complete(r)
        result = r.routing.reasoning_result(r.actor, d)
        assert result["job_id"] == job["id"] and result["role"] == "investigator"
        assert result["parsed"] == {"queries": ["inspect original receipt"]}
        assert result["semantic_correctness"] == "not_evaluated" and not result["runtime_qualified"]
        r.routing.transition(r.actor, r.policy_id, expected_epoch=2, stage="blocked")
        assert ModelRoutingService(app_env.db).reasoning_result(r.actor, d) == result
        assert r.runner.run_once(r.actor, "restarted-worker") is None and len(r.requests) == 1
        with app_env.db.transaction(r.actor) as conn:
            assert conn.execute("SELECT count(*) AS n FROM budget_usage").fetchone()["n"] == 1
    finally:
        r.http.close()


@pytest.mark.parametrize(
    "text", ['{"answer":42}', '{"queries":[],"execute":"shell"}', '```json\n{"queries":[]}\n```']
)
def test_completion_and_valid_json_do_not_replace_the_reasoning_role_contract(
    app_env, monkeypatch, text
):
    wire = mock.wire
    monkeypatch.setattr(mock, "wire", lambda: wire((text,)))
    r = rig(app_env)
    try:
        d, job = complete(r)
        assert job["state"] == "succeeded"
        with pytest.raises(InvalidInputError):
            r.routing.reasoning_result(r.actor, d)
        assert len(r.requests) == 1
    finally:
        r.http.close()


def test_shared_output_does_not_grant_access_to_private_reasoning_input_or_result(
    app_env, monkeypatch
):
    wire = mock.wire
    monkeypatch.setattr(mock, "wire", lambda: wire(('{"queries":[]}',)))
    r = rig(app_env)
    try:
        d, _ = complete(r)
        owner = ModelRouteOutcomeService(app_env.db)
        outcome = owner.capture(r.actor, d)
        owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
        with pytest.raises(ScopeDeniedError):
            ModelRoutingService(app_env.other_db).reasoning_result(r.reviewer, d)
        assert ModelRouteOutcomeService(app_env.other_db).inspect(r.reviewer, d) == outcome
    finally:
        r.http.close()


def test_lost_reply_cannot_be_consumed_as_reasoning_or_replayed(app_env):
    r = rig(app_env)
    try:

        def lost(request):
            raise httpx.ReadError("Synthetic response lost", request=request)

        r.callbacks.before_response = lost
        d, job = complete(r)
        assert job["state"] == "indeterminate"
        with pytest.raises(StateMismatchError):
            r.routing.reasoning_result(r.actor, d)
        assert r.runner.run_once(r.actor, "recovery-worker") is None and len(r.requests) == 1
        with app_env.db.transaction(r.actor) as conn:
            assert {
                v["status"]
                for v in conn.execute("SELECT status FROM budget_reservations").fetchall()
            } == {"indeterminate"}
    finally:
        r.http.close()


def native_route(env, tmp_path, monkeypatch, *, corrupt=None):
    # Real restricted SQL, original queue, grant checks and verified artifact
    # storage. Native transport/engine are explicit synthetic fixture callbacks.
    from copy import deepcopy
    from dataclasses import replace
    from uuid import uuid4

    from potbot.cogs.contracts import TypedCogValue
    from potbot.controller.cog_capability_policy import (
        Capability,
        CogCapabilityGrant,
        installation_hash,
    )
    from potbot.controller.cog_grant_store import publish_cog_policy
    from potbot.controller.model_route_bindings import worker_admission_identity
    from potbot.controller.operation_workbench import OperationWorkbench
    from potbot.domain.serialization import canonical_json, content_hash
    from potbot.domain.syn import SynObject, SynRef, SynSnapshot
    from potbot.models.native_results import REQUEST, RESULT
    from potbot.models.registry import ModelRegistry
    from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
    from potbot.semantics.service import SynService
    from tests.integration.test_model_route_bindings import accept
    from tests.integration.test_operation_checks import enqueue_check, native
    from tests.integration.test_operation_workbench import peer
    from tests.unit.test_model_reasoning_results import deployment, native_output, request
    from tests.unit.test_model_route_bindings import recipe

    r = native(env, tmp_path, monkeypatch)
    r.env = env
    ModelRegistry(env.db).register(r.actor, deployment())
    previous = r.dispatcher.operations[r.reference]
    op = replace(
        previous, binding=replace(previous.binding, input_type_ref=REQUEST, output_type_ref=RESULT)
    )
    r.dispatcher.operations = {r.reference: op}
    r.executor.bind_context(env.db, r.actor, (op,))
    policy = replace(
        r.policy,
        grants=(CogCapabilityGrant(installation_hash(op), frozenset({Capability.READ}), 2**31),),
    )
    publish_cog_policy(env.admin_dsn, env.schema, policy, 1)
    r.spec["input"]["type_ref"] = REQUEST
    r.spec["input"]["value"] = request()

    async def execute(op, value, context, actor, agent, *, syn_authorization):
        r.mesh.consume(syn_authorization, op, value, context, actor, agent)
        r.executions.append(op.binding.operation_ref)
        payload = native_output(context.invocation_id, value.value)
        if corrupt == "job":
            payload["value"]["invocation_id"] = str(uuid4())
        if corrupt == "deployment":
            payload["value"]["deployment_sha256"] = "9" * 64
        if corrupt == "length":
            payload["value"]["finish_reason"] = "length"
        return TypedCogValue(payload["type_ref"], payload["value"])

    monkeypatch.setattr(r.executor, "execute", execute)
    reference = "syn://cog/fixture"
    record = SynObject(
        SynRef(reference),
        SynRef("syn://type/cog"),
        canonical_json({"id": reference, "typeRef": "syn://type/cog", "name": "Fixture"}),
    )
    SynService(
        env.db, ArtifactStore(env.db, LocalArtifactBackend(env.artifact_root))
    ).import_snapshot(
        r.actor,
        env.repository_id,
        "native-model-contract-fixture",
        SynSnapshot("Explicit native model fixture", "extend_builtin", (), (record,)),
    )
    r.workbench = OperationWorkbench(env.db)
    r.operation = next(
        i["id"] for i in r.workbench.catalogue.list(r.actor)["items"] if i["reference"] == reference
    )
    enqueue_check(r, key="native-installation-observation")
    observed = r.runner.run_once(r.actor, "installation-check")["result"]["operation_check"]
    assert observed["eligible"], observed
    r.target = {
        "operation_id": r.operation,
        "runtime_identity_sha256": "a" * 64,
        "executor": {
            "job_type": "cog.execute",
            "selected_models": [],
            "input": {
                "operation_ref": r.reference,
                "profile_sha256": r.dispatcher.profile_hash,
                "type_ref": REQUEST,
                "value": {"model": "fixture-model", "deployment_sha256": deployment().fingerprint},
            },
        },
    }
    r.target["runtime_identity_sha256"] = worker_admission_identity(r.target, observed["bindings"])
    r.routing = ModelRoutingService(env.db)
    r.reviewer = peer(env)
    r.review_service = ModelRoutingService(env.other_db)
    r.policy_id = uuid4()
    r.recipe = recipe()
    r.routing.create(
        r.actor,
        r.policy_id,
        reviewer_id=r.reviewer.principal_id,
        role="reasoning",
        atomic_kind="evidence_queries",
        baseline=r.target,
        candidate={**r.target, "runtime_identity_sha256": "b" * 64},
        prompt_contract_sha256=content_hash(r.recipe),
        evaluation_plan_sha256="e" * 64,
    )
    r.routing.configure_prompt(r.actor, r.policy_id, r.recipe)
    accept(r)
    r.routing.transition(r.actor, r.policy_id, expected_epoch=1, stage="shadow")
    r.base = deepcopy(r.spec)
    del r.base["input"]["value"]["prompt"]
    del r.base["input"]["value"]["system"]
    return r


def test_native_typed_artifact_is_bound_to_original_checked_job_and_recovers_without_dispatch(
    app_env, tmp_path, monkeypatch
):
    r = native_route(app_env, tmp_path, monkeypatch)
    d, job = complete(r)
    assert job["state"] == "succeeded"
    value = r.routing.reasoning_result(r.actor, d)
    assert value["job_id"] == job["id"] and value["parsed"] == {
        "queries": ["inspect original receipt"]
    }
    assert value["usage"]["total_tokens"] == 15 and value["runtime_qualified"] is False
    assert ModelRoutingService(app_env.db).reasoning_result(r.actor, d) == value
    assert r.runner.run_once(r.actor, "read-only-recovery") is None and len(r.executions) == 1
    assert r.mesh._grants == {}


@pytest.mark.parametrize("corrupt", ["job", "deployment", "length"])
def test_completed_native_process_cannot_supply_another_or_incomplete_model_result(
    app_env, tmp_path, monkeypatch, corrupt
):
    r = native_route(app_env, tmp_path, monkeypatch, corrupt=corrupt)
    d, job = complete(r)
    assert job["state"] == "succeeded"  # Process completion is not model-result acceptance.
    with pytest.raises(StateMismatchError):
        r.routing.reasoning_result(r.actor, d)
    assert len(r.executions) == 1 and r.runner.run_once(r.actor, "no-replay") is None
