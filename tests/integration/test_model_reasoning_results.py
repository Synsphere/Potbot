"""Original checked jobs/SQL; model HTTP and native execution are explicit fixtures."""

from uuid import UUID, uuid4

import httpx
import pytest

from potbot.controller.model_route_outcomes import ModelRouteOutcomeService
from potbot.controller.model_routing import ModelRoutingService
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
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


def native_route(
    env, tmp_path, monkeypatch, *, corrupt=None, installed_model=False, model_prompt_version=None
):
    # Real restricted SQL, original queue, grant checks and verified artifact
    # storage. Native transport/engine are explicit synthetic fixture callbacks.
    from copy import deepcopy
    from dataclasses import replace
    from types import SimpleNamespace

    from potbot.cogs.contracts import TypedCogValue
    from potbot.controller.budgets import BudgetLedger
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
    from potbot.tools import native_model_worker
    from tests.integration.test_model_route_bindings import accept
    from tests.integration.test_operation_checks import enqueue_check, native
    from tests.integration.test_operation_workbench import peer
    from tests.unit.test_model_reasoning_results import deployment, native_output, request
    from tests.unit.test_model_route_bindings import recipe

    r = native(
        env,
        tmp_path,
        monkeypatch,
        installed_model=installed_model,
        model_prompt_version=model_prompt_version,
    )
    r.env = env
    model = r.model_deployment or deployment()
    ModelRegistry(env.db).register(r.actor, model)
    if not installed_model:
        previous = r.dispatcher.operations[r.reference]
        op = replace(
            previous,
            binding=replace(previous.binding, input_type_ref=REQUEST, output_type_ref=RESULT),
        )
        r.dispatcher.operations = {r.reference: op}
        r.executor.bind_context(env.db, r.actor, (op,))
        policy = replace(
            r.policy,
            grants=(
                CogCapabilityGrant(installation_hash(op), frozenset({Capability.READ}), 2**31),
            ),
        )
        publish_cog_policy(env.admin_dsn, env.schema, policy, 1)
    r.spec["input"]["type_ref"] = REQUEST
    r.spec["input"]["value"] = request() | {"deployment_sha256": model.fingerprint}
    r.inference_holds = []

    def fake_inference(selected, deployment_document, engine_sha256):
        assert selected["deployment_sha256"] == model.fingerprint
        assert deployment_document == model.to_dict()
        assert engine_sha256 == r.dispatcher.operations[r.reference].argv[2]
        r.inference_holds.append(BudgetLedger(env.db).inspect(r.actor, r.task_id)["held"])
        return (
            '{"queries":["inspect original receipt"]}',
            "stop",
            {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        )

    if installed_model:
        monkeypatch.setattr(native_model_worker, "run_inference", fake_inference)

    async def execute(op, value, context, actor, agent, *, syn_authorization):
        r.mesh.consume(syn_authorization, op, value, context, actor, agent)
        r.executions.append(op.binding.operation_ref)
        if installed_model:
            replies = []
            port = SimpleNamespace(
                work_input=lambda: {
                    "schema_version": 1,
                    "invocation_id": str(context.invocation_id),
                    "input": {"type_ref": value.type_ref, "value": value.value},
                },
                finish_work=lambda type_ref, value, **_: replies.append(
                    TypedCogValue(type_ref, value)
                ),
            )
            native_model_worker.run_work(port, model.to_dict(), op.argv[2])
            if corrupt == "lost":
                raise IndeterminateExecutionError("Synthetic reply lost after native inference")
            (reply,) = replies
            return reply
        if corrupt == "lost":
            raise IndeterminateExecutionError("Synthetic native reply lost after dispatch")
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
                "value": {"model": model.canonical_model, "deployment_sha256": model.fingerprint},
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


def test_native_outcome_can_be_shared_and_reviewed_without_replaying(
    app_env, tmp_path, monkeypatch
):
    r = native_route(app_env, tmp_path, monkeypatch)
    d, job = complete(r)
    r.routing.transition(r.actor, r.policy_id, expected_epoch=2, stage="blocked")
    owner = ModelRouteOutcomeService(app_env.db)
    reviewer = ModelRouteOutcomeService(app_env.other_db)
    outcome = owner.capture(r.actor, d)
    receipt = outcome["native_result"]
    assert outcome["job_id"] == job["id"] and outcome["provider_receipts"] == []
    assert outcome["output_text"] == '{"queries":["inspect original receipt"]}'
    assert (
        outcome["assessment"]["completion_confirmed"]
        and outcome["assessment"]["literal_contract"]["syntax_valid"]
    )
    assert receipt["artifact_id"] == job["result"]["artifact_id"]
    assert receipt["usage_origin"] == "worker_reported"
    assert receipt["usage"]["total_tokens"] == 15
    assert receipt["finish_reason"] == "stop"
    with pytest.raises(ScopeDeniedError):
        reviewer.inspect(r.reviewer, d)
    with pytest.raises(StateMismatchError):
        owner.share(r.actor, d, expected_outcome_sha256="0" * 64)
    owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
    assert reviewer.inspect(r.reviewer, d) == outcome
    with app_env.other_db.transaction(r.reviewer) as conn:
        assert (
            conn.execute("SELECT native_evidence FROM model_route_outcomes").fetchone()[
                "native_evidence"
            ]
            == receipt
        )
        for table in ("jobs", "job_events", "model_route_compiled_prompts"):
            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
    rid = uuid4()
    kwargs = {
        "verdict": "acceptable",
        "evidence_sha256": "c" * 64,
        "evaluator_revision": "native/1",
    }
    assert reviewer.review(r.reviewer, d, rid, **kwargs) == reviewer.review(
        r.reviewer, d, rid, **kwargs
    )
    assert owner.capture(r.actor, d) == outcome
    summary = owner.summary(r.actor, r.policy_id)
    assert summary["independent_reviews"]["acceptable"] == 1
    assert not summary["runtime_qualified"] and not summary["holdout_qualified"]
    assert r.runner.run_once(r.actor, "no-replay-after-review") is None and len(r.executions) == 1


@pytest.mark.parametrize(("fault", "state"), [("length", "succeeded"), ("lost", "indeterminate")])
def test_native_outcome_preserves_noncompletion_and_refuses_acceptable_review(
    app_env,
    tmp_path,
    monkeypatch,
    fault,
    state,
):
    r = native_route(app_env, tmp_path, monkeypatch, corrupt=fault)
    d, job = complete(r)
    owner = ModelRouteOutcomeService(app_env.db)
    reviewer = ModelRouteOutcomeService(app_env.other_db)
    outcome = owner.capture(r.actor, d)
    assert outcome["state"] == job["state"] == state
    assert not outcome["assessment"]["completion_confirmed"]
    if fault == "lost":
        assert outcome["native_result"] is None and outcome["output_text"] == ""
    else:
        assert outcome["native_result"]["finish_reason"] == "length"
    owner.share(r.actor, d, expected_outcome_sha256=outcome["sha256"])
    with pytest.raises(StateMismatchError):
        reviewer.review(
            r.reviewer,
            d,
            uuid4(),
            verdict="acceptable",
            evidence_sha256="c" * 64,
            evaluator_revision="native/1",
        )
    with pytest.raises(RevisionConflictError), app_env.other_db.transaction(r.reviewer) as conn:
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
    reviewer.review(
        r.reviewer,
        d,
        uuid4(),
        verdict="inconclusive",
        evidence_sha256="c" * 64,
        evaluator_revision="native/1",
    )
    assert owner.summary(r.actor, r.policy_id)["terminal_states"][state] == 1
    assert (
        r.runner.run_once(r.actor, "no-replay-after-incomplete") is None and len(r.executions) == 1
    )


def test_native_outcome_refuses_wrong_artifact_provenance(app_env, tmp_path, monkeypatch):
    r = native_route(app_env, tmp_path, monkeypatch, corrupt="deployment")
    d, _ = complete(r)
    owner = ModelRouteOutcomeService(app_env.db)
    with pytest.raises(StateMismatchError):
        owner.capture(r.actor, d)
    assert owner.summary(r.actor, r.policy_id)["unobserved_decisions"] == 1
    assert r.runner.run_once(r.actor, "no-replay-after-invalid-result") is None


@pytest.mark.parametrize("lost", [False, True])
def test_checked_native_reasoning_recovers_same_sealed_job_and_budget(
    app_env,
    tmp_path,
    monkeypatch,
    lost,
):
    from potbot.controller.budgets import BudgetLedger
    from potbot.controller.checked_reasoning import CheckedReasoning
    from tests.integration.test_checked_reasoning import begin
    from tests.integration.test_operation_checks import TOKEN, effects

    r = native_route(
        app_env, tmp_path, monkeypatch, installed_model=True, corrupt="lost" if lost else None
    )
    checked_reasoning = CheckedReasoning(app_env.db)
    pending = begin(checked_reasoning, r)
    decision = UUID(pending["decision_id"])
    assert pending["state"] == "checking" and pending["job_id"] is None
    r.runner.run_once(r.actor, "installed-model-check")
    assert checked_reasoning.status(r.actor, decision)["state"] == "checked"
    assert effects(r)["reservations"] == 0 and r.executions == []
    queued = checked_reasoning.launch(r.actor, decision, setup_token=TOKEN)
    job = r.runner.run_once(r.actor, "installed-model")
    assert job["id"] == queued["job_id"]
    recovered = CheckedReasoning(app_env.db)
    status = recovered.status(r.actor, decision)
    assert status["job_id"] == queued["job_id"]
    assert recovered.launch(r.actor, decision, setup_token=TOKEN) == status
    observed = ModelRouteOutcomeService(app_env.db).capture(r.actor, decision)
    hold = {
        "tokens": r.model_deployment.usable_context_tokens,
        "calls": 1,
        "tool_ms": 1000,
        "concurrency": 1,
    }
    assert r.inference_holds == [hold]
    budget = BudgetLedger(app_env.db).inspect(r.actor, r.task_id)
    if lost:
        assert status["state"] == observed["state"] == "indeterminate"
        assert observed["native_result"] is None
        assert not observed["assessment"]["completion_confirmed"]
        assert budget["held"] == hold and budget["used"] == {} and effects(r)["usage"] == 0
        with pytest.raises(StateMismatchError):
            recovered.result(r.actor, decision)
    else:
        assert status["state"] == observed["state"] == "succeeded"
        result = recovered.result(r.actor, decision)
        assert result["job_id"] == job["id"] and result["parsed"] == {
            "queries": ["inspect original receipt"]
        }
        assert observed["native_result"]["usage"] == result["usage"]
        assert observed["native_result"]["artifact_id"] == job["result"]["artifact_id"]
        assert budget["held"] == {} and budget["used"] == {
            "tokens": 15,
            "calls": 1,
            "tool_ms": 1000,
        }
        assert effects(r)["usage"] == 1
    assert r.runner.run_once(r.actor, "installed-model-restart") is None
    assert len(r.executions) == 1 and r.mesh._grants == {}


def test_checked_native_reasoning_blocks_a_deployment_with_the_wrong_prompt_version(
    app_env,
    tmp_path,
    monkeypatch,
):
    from potbot.controller.checked_reasoning import CheckedReasoning
    from tests.integration.test_checked_reasoning import begin
    from tests.integration.test_operation_checks import TOKEN, effects

    r = native_route(
        app_env,
        tmp_path,
        monkeypatch,
        installed_model=True,
        model_prompt_version="unrelated-prompt-v1",
    )
    before = effects(r)
    controller = CheckedReasoning(app_env.db)
    pending = begin(controller, r)
    decision = UUID(pending["decision_id"])
    report = r.runner.run_once(r.actor, "native-model-recipe-check")
    assert report["id"] == pending["check_job_id"]
    checked = report["result"]["operation_check"]
    assert checked["eligible"] is False
    assert checked["checks"][-1] == {"code": "model_route_binding", "status": "blocked"}
    assert controller.status(r.actor, decision)["state"] == "blocked"
    with pytest.raises(StateMismatchError):
        controller.launch(r.actor, decision, setup_token=TOKEN)
    assert r.runner.run_once(r.actor, "no-eligible-native-model") is None
    assert effects(r) == before and not r.executions and not r.inference_holds
