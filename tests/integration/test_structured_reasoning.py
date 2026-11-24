"""Use the existing worker and ledger with a structured provider wire fixture."""

import json
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator

from potbot.controller.budgets import BudgetLedger
from potbot.controller.model_route_bindings import checked_output_spec
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.models.reasoning_schema import ROLE_SCHEMA_VARIANT, ReasoningOutputSpec
from tests.integration import test_hosted_gateway as provider
from tests.integration.test_model_route_bindings import accept, check, launch, rig
from tests.integration.test_operation_checks import effects
from tests.unit.test_model_route_bindings import facts

pytestmark = pytest.mark.postgres


def _setup(env, role, packet):
    r = rig(env, configure=False, activate=False)
    r.policy_id = uuid4()
    r.recipe = r.recipe | {"role": role, "variant": ROLE_SCHEMA_VARIANT}
    r.routing.create(
        r.actor,
        r.policy_id,
        reviewer_id=r.reviewer.principal_id,
        role="reasoning",
        atomic_kind=role,
        baseline=r.target,
        candidate=r.target | {"runtime_identity_sha256": "b" * 64},
        prompt_contract_sha256=content_hash(r.recipe),
        evaluation_plan_sha256="e" * 64,
    )
    r.routing.configure_prompt(r.actor, r.policy_id, r.recipe)
    accept(r)
    r.routing.transition(r.actor, r.policy_id, expected_epoch=1, stage="shadow")
    decision = r.routing.allocate_prompted(r.actor, r.policy_id, "structured", r.base, packet)
    return r, decision


@pytest.mark.parametrize("duplicate_branch", [False, True])
def test_structured_check_binds_exact_schema_and_preserves_role_validation(
    app_env, monkeypatch, duplicate_branch
):
    packet = facts() | {"context": {"branches": [{"node_id": "a"}, {"node_id": "b"}]}}
    r, decision = _setup(app_env, "critic", packet)
    try:
        typed = ReasoningOutputSpec("critic", ("a", "b"))
        checked = check(r, decision)
        report = checked["result"]["operation_check"]
        assert (
            report["eligible"]
            and report["bindings"]["modelOutputSchemaSha256"] == typed.fingerprint
        ), report
        assert checked_output_spec(app_env.db, r.actor, decision_id=decision["id"]) == typed
        assert not r.requests and effects(r)["reservations"] == 0
        queued = launch(r, decision, checked)
        output = {
            "evaluations": [
                {
                    "node_id": branch,
                    "relevance": 2,
                    "evidence_alignment": 4,
                    "falsification_risk": 0,
                    "uncertainty": 1,
                    "recommendation": "keep",
                    "rationale": "Compare with the explicit evidence.",
                    "issues": [],
                    "tests": [],
                }
                for branch in ("a", "a" if duplicate_branch else "b")
            ]
        }
        wire = provider.wire

        def response():
            sent = json.loads(r.requests[-1].content)["response_format"]
            assert sent == {
                "type": "json_schema",
                "json_schema": {"name": typed.name, "strict": True, "schema": typed.schema()},
            }
            assert Draft202012Validator(sent["json_schema"]["schema"]).is_valid(output)
            return wire((json.dumps(output),))

        monkeypatch.setattr(provider, "wire", response)
        job = r.runner.run_once(r.actor, "structured-critic")
        assert job["id"] == queued["job"]["id"] and job["state"] == "succeeded", job
        assert checked_output_spec(app_env.db, r.actor, job_id=UUID(job["id"])) == typed
        if duplicate_branch:
            with pytest.raises(InvalidInputError):
                r.routing.reasoning_result(r.actor, UUID(decision["id"]))
        else:
            result = r.routing.reasoning_result(r.actor, UUID(decision["id"]))
            assert set(result["parsed"]) == {"a", "b"}
        ledger = BudgetLedger(app_env.db).inspect(r.actor, r.task_id)
        assert ledger["used"]["calls"] == 1 and ledger["held"] == {}
        assert r.runner.run_once(r.actor, "no-schema-fallback") is None
        assert len(r.requests) == effects(r)["usage"] == 1
    finally:
        r.http.close()
