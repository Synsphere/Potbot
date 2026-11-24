"""Frozen policy, honest hypothetical selection and zero-dispatch shadow audits."""

from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_experiments import freeze_atomic_experiment
from potbot.evaluation.agentic_model_bench import (
    Generation,
    ModelTarget,
    RoleCase,
    RoleSuite,
    observe_case,
    vars_case,
)
from potbot.evaluation.agentic_shadow import evaluate_atomic_shadow, validate_shadow_policy

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "shadow_cli", ROOT / "scripts/benchmark_agentic_models.py"
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def fixture(count=200, repeat=1, variants=("generic",)):
    suite = RoleSuite(
        "shadow-fixture",
        tuple(
            RoleCase(
                f"c-{i}",
                "triage",
                f"Synthetic input {i}",
                "json_subset",
                {"ok": True},
                context={"atomic_kind": "check_exit_status", "index": i},
            )
            for i in range(count)
        ),
    )
    models = [
        ModelTarget(
            name,
            name,
            "loopback_openai",
            ("triage",),
            "fixed-revision",
            base_url="http://127.0.0.1:11434/v1",
        )
        for name in ("strong", "tiny")
    ]
    policy = {
        "schema": "potbot.atomic-shadow-policy/1",
        "rules": [
            {
                "atomic_kind": "check_exit_status",
                "role": "triage",
                "variant": variant,
                "baseline": "strong",
                "candidate": "tiny",
                "minimum_distinct_cases": 12,
                "minimum_accuracy_lower_95_bps": 9500,
                "maximum_accuracy_drop_bps": 200,
                "maximum_p95_latency_ms": 10,
                "maximum_mean_billed_microunits": 7,
                "maximum_mean_total_tokens": 12,
            }
            for variant in variants
        ],
    }
    evidence = {
        name: None
        for name in (
            "candidate_package",
            "gold_review",
            "dataset_card",
            "deployment_bindings",
            "review_bundle",
        )
    }
    evidence["shadow_policy"] = policy
    plan = freeze_atomic_experiment(
        suite, models, variants=variants, repeat=repeat, shuffle_seed=42, evidence=evidence
    )
    rows = []
    for i, case in enumerate(suite.cases):
        for model in models:
            text = '{"ok":true}' if model.id == "tiny" or i < count // 2 else '{"ok":false}'
            for variant in variants:
                for trial in range(1, repeat + 1):
                    rows.append(
                        observe_case(
                            model=model,
                            case=case,
                            variant=variant,
                            trial=trial,
                            generate=lambda *_, text=text: Generation(text, 4, 5, 6, 7),
                            retain_response=True,
                        ).to_dict()
                    )
    document = {
        "schema_version": 1,
        "suite_sha256": suite.identity,
        "model_targets_sha256": plan["targets_sha256"],
        "experiment_plan_sha256": plan["plan_sha256"],
        "results": rows,
        "qualified": False,
        "auto_promoted": False,
    }
    return suite, models, policy, evidence, plan, document


def evaluate(data):
    suite, models, policy, evidence, plan, document = data
    return evaluate_atomic_shadow(policy, plan, document, suite, models, evidence=evidence)


def test_complete_favorable_evidence_is_only_a_hypothetical_selection():
    result = evaluate(fixture())
    row = result["decisions"][0]
    assert row["offline_limits_met"] and row["hypothetical_selection"] == "tiny"
    assert row["candidate_accuracy_lower_95"] > 0.98
    assert row["candidate_mean_billed_microunits"] == 7
    assert row["candidate_mean_total_tokens"] == 11
    assert result["qualified"] is result["promotion_ready"] is row["promotion_ready"] is False
    assert result["model_requests"] == result["automatic_routing_changes"] == 0
    assert "qualified_baseline_and_rollback" in result["promotion_blockers"]
    assert "actual_server_and_worker_identity" in result["promotion_blockers"]
    assert "Synthetic input" not in canonical_json(result) and "127.0.0.1" not in canonical_json(
        result
    )


def test_too_few_cases_and_repeats_do_not_inflate_quality_evidence():
    result = evaluate(fixture(4, repeat=5))
    row = result["decisions"][0]
    assert "paired_holdout_not_ready" in row["blockers"]
    assert row["paired_comparison"]["paired_objective_cases"] == 4
    assert row["hypothetical_selection"] == "strong"


@pytest.mark.parametrize(
    "metric,blocker,value",
    [
        ("elapsed_ms", "tail_latency_limit", 11),
        ("billed_microunits", "billing_limit", 8),
        ("input_tokens", "token_limit", 7),
        ("billed_microunits", "billing_unknown", None),
        ("input_tokens", "tokens_unknown", None),
    ],
)
def test_expense_and_latency_limits_preserve_unknowns(metric, blocker, value):
    data = fixture()
    for row in data[-1]["results"]:
        if row["model_id"] == "tiny":
            row[metric] = value
    result = evaluate(data)
    row = result["decisions"][0]
    assert blocker in row["blockers"] and not row["offline_limits_met"]
    assert row["hypothetical_selection"] == "strong"
    if value is None:
        name = (
            "candidate_mean_billed_microunits"
            if metric == "billed_microunits"
            else "candidate_mean_total_tokens"
        )
        assert row[name] is None


@pytest.mark.parametrize("kind", ["missing", "uncertain", "identity_mismatch"])
def test_lost_or_misattributed_reply_blocks_selection_without_replay(kind):
    data = fixture()
    row = next(r for r in data[-1]["results"] if r["model_id"] == "tiny")
    if kind == "missing":
        data[-1]["results"].remove(row)
    elif kind == "identity_mismatch":
        row.update(
            status="identity_mismatch",
            returned_model="wrong",
            model_identity="mismatch",
            score=None,
        )
    else:
        row.update(
            status="indeterminate",
            response=None,
            output_sha256=None,
            score=None,
            elapsed_ms=None,
            input_tokens=None,
            output_tokens=None,
            billed_microunits=None,
        )
    result = evaluate(data)
    assert "incomplete_experiment" in result["decisions"][0]["blockers"]
    assert result["model_requests"] == 0 and result["experiment_audit"]["planned_attempts"] == 400


def test_correctness_and_paired_margin_fail_even_with_complete_fast_outputs():
    data = fixture()
    for row in data[-1]["results"]:
        if row["model_id"] == "tiny":
            import hashlib

            row.update(response='{"ok":false}', score=0)
            row["output_sha256"] = hashlib.sha256(row["response"].encode()).hexdigest()
    row = evaluate(data)["decisions"][0]
    assert {"correctness_floor", "paired_quality_margin"} <= set(row["blockers"])


def test_structural_only_rubrics_cannot_become_quality_evidence():
    data = list(fixture(12))
    suite = replace(
        data[0], cases=tuple(replace(c, grader="json_shape", expected={}) for c in data[0].cases)
    )
    with pytest.raises(InvalidInputError):
        evaluate_atomic_shadow(data[2], data[4], data[5], suite, data[1], evidence=data[3])
    # A consistently re-frozen structural suite still cannot pass paired acceptance.
    data[0] = suite
    data[4] = freeze_atomic_experiment(
        suite, data[1], variants=("generic",), repeat=1, shuffle_seed=42, evidence=data[3]
    )
    data[5].update(suite_sha256=suite.identity, experiment_plan_sha256=data[4]["plan_sha256"])
    for row in data[5]["results"]:
        row.update(grader="json_shape", score=1)
    assert "paired_holdout_not_ready" in evaluate(data)["decisions"][0]["blockers"]


@pytest.mark.parametrize(
    "mutation",
    [
        "bool",
        "negative",
        "extra",
        "duplicate",
        "missing_scope",
        "unknown_target",
        "alias",
        "unplanned_variant",
    ],
)
def test_invalid_policy_is_rejected_during_freezing(mutation):
    suite, models, policy, evidence, _, _ = fixture(4, variants=("generic", "family"))
    rule = policy["rules"][0]
    if mutation == "bool":
        rule["minimum_distinct_cases"] = True
    elif mutation == "negative":
        rule["maximum_p95_latency_ms"] = -1
    elif mutation == "extra":
        rule["qualified"] = True
    elif mutation == "duplicate":
        policy["rules"].append(copy.deepcopy(rule))
    elif mutation == "missing_scope":
        policy["rules"].pop()
    elif mutation == "unknown_target":
        rule["candidate"] = "absent"
    elif mutation == "alias":
        models[1] = replace(models[1], model=models[0].model)
    else:
        rule["variant"] = "model_role"
    with pytest.raises(InvalidInputError):
        freeze_atomic_experiment(
            suite,
            models,
            variants=("generic", "family"),
            repeat=1,
            shuffle_seed=42,
            evidence=evidence,
        )


def test_policy_is_detached_and_changed_criteria_cannot_reinterpret_an_old_run():
    data = fixture(12)
    detached = validate_shadow_policy(data[2], data[0], data[1], ("generic",))
    data[2]["rules"][0]["maximum_p95_latency_ms"] = 999
    assert detached["rules"][0]["maximum_p95_latency_ms"] == 10
    with pytest.raises(InvalidInputError, match="before execution"):
        evaluate(data)
    data[4]["evidence_sha256"]["shadow_policy"] = content_hash(data[2])
    data[4]["plan_sha256"] = content_hash({k: v for k, v in data[4].items() if k != "plan_sha256"})
    with pytest.raises(InvalidInputError, match="not bound"):
        evaluate(data)


def forbidden(*_args, **_kwargs):
    pytest.fail("Shadow analysis may construct no transport or vault")


def test_cli_freeze_run_and_shadow_bind_policy_before_dispatch(tmp_path, monkeypatch, capsys):
    suite, models, policy, _, _, _ = fixture(12)
    values = {
        "suite": {
            "id": suite.id,
            "schema_version": 1,
            "cases": [vars_case(c) for c in suite.cases],
        },
        "models": {"schema_version": 1, "models": [asdict(m) for m in models]},
        "policy": policy,
    }
    for name, value in values.items():
        (tmp_path / f"{name}.json").write_text(canonical_json(value))
    common = [
        "--suite",
        str(tmp_path / "suite.json"),
        "--models",
        str(tmp_path / "models.json"),
        "--shadow-policy",
        str(tmp_path / "policy.json"),
    ]
    plan = tmp_path / "plan.json"
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    assert (
        cli.main(["prepare-experiment", *common, "--variants", "generic", "--output", str(plan)])
        == 0
    )
    calls = []

    class Transport:
        def __init__(self, **kwargs):
            pass

        def request(self, model, packet):
            folder = next((tmp_path / "runs").iterdir())
            retained = json.loads((folder / "experiment-plan.json").read_text())
            assert retained["evidence_sha256"]["shadow_policy"] == content_hash(policy)
            calls.append(model.id)
            if len(calls) == 1:
                raise OSError("synthetic reply lost")
            return Generation('{"ok":true}', 4, 5, 6, 7)

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    assert (
        cli.main(
            [
                "run",
                *common,
                "--experiment-plan",
                str(plan),
                "--variants",
                "generic",
                "--output-dir",
                str(tmp_path / "runs"),
                "--store-text",
                "--confirm-prompt-disclosure",
            ]
        )
        == 0
    )
    capsys.readouterr()
    report = next((tmp_path / "runs").glob("*/report.json"))
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    output = tmp_path / "shadow.json"
    flags = [
        "shadow-routing",
        *common,
        "--experiment-plan",
        str(plan),
        "--results",
        str(report),
        "--output",
        str(output),
    ]
    assert cli.main(flags) == 2
    assert len(calls) == 24 and json.loads(output.read_text())["model_requests"] == 0
    with pytest.raises(SystemExit):
        cli.main(flags)
    assert len(calls) == 24


def test_preflight_refuses_changed_policy_without_transport(tmp_path, monkeypatch):
    suite, models, policy, _, plan, _ = fixture(12)
    values = {
        "suite": {
            "id": suite.id,
            "schema_version": 1,
            "cases": [vars_case(c) for c in suite.cases],
        },
        "models": {"schema_version": 1, "models": [asdict(m) for m in models]},
        "policy": policy,
        "plan": plan,
    }
    policy["rules"][0]["maximum_p95_latency_ms"] = 100
    for name, value in values.items():
        (tmp_path / f"{name}.json").write_text(canonical_json(value))
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "preflight",
                "--suite",
                str(tmp_path / "suite.json"),
                "--models",
                str(tmp_path / "models.json"),
                "--shadow-policy",
                str(tmp_path / "policy.json"),
                "--experiment-plan",
                str(tmp_path / "plan.json"),
                "--variants",
                "generic",
            ]
        )


@pytest.mark.parametrize("command", ["preflight", "run"])
def test_shadow_criteria_without_a_plan_are_refused_before_dispatch(tmp_path, monkeypatch, command):
    suite, models, policy, _, _, _ = fixture(12)
    values = {
        "suite": {
            "id": suite.id,
            "schema_version": 1,
            "cases": [vars_case(c) for c in suite.cases],
        },
        "models": {"schema_version": 1, "models": [asdict(m) for m in models]},
        "policy": policy,
    }
    for name, value in values.items():
        (tmp_path / f"{name}.json").write_text(canonical_json(value))
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    flags = [
        command,
        "--suite",
        str(tmp_path / "suite.json"),
        "--models",
        str(tmp_path / "models.json"),
        "--shadow-policy",
        str(tmp_path / "policy.json"),
        "--variants",
        "generic",
    ]
    if command == "run":
        flags += ["--output-dir", str(tmp_path / "runs")]
    with pytest.raises(SystemExit):
        cli.main(flags)
    assert not (tmp_path / "runs").exists()


def test_cli_persists_a_favorable_synthetic_audit_without_granting_a_route(tmp_path, monkeypatch):
    suite, models, policy, _, plan, document = fixture()
    values = {
        "suite": {
            "id": suite.id,
            "schema_version": 1,
            "cases": [vars_case(c) for c in suite.cases],
        },
        "models": {"schema_version": 1, "models": [asdict(m) for m in models]},
        "policy": policy,
        "plan": plan,
        "results": document,
    }
    for name, value in values.items():
        (tmp_path / f"{name}.json").write_text(canonical_json(value))
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    output = tmp_path / "audit.json"
    assert (
        cli.main(
            [
                "shadow-routing",
                "--suite",
                str(tmp_path / "suite.json"),
                "--models",
                str(tmp_path / "models.json"),
                "--shadow-policy",
                str(tmp_path / "policy.json"),
                "--experiment-plan",
                str(tmp_path / "plan.json"),
                "--results",
                str(tmp_path / "results.json"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    audit = json.loads(output.read_text())
    assert audit["all_offline_limits_met"] and audit["promotion_ready"] is False
    assert audit["model_requests"] == audit["automatic_routing_changes"] == 0
