"""Predeclared matrices, whole-cohort accounting and no-dispatch admission."""

from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_experiments import (
    admit_atomic_experiment,
    audit_atomic_experiment,
    freeze_atomic_experiment,
)
from potbot.evaluation.agentic_model_bench import (
    Generation,
    ModelTarget,
    RoleCase,
    RoleSuite,
    observe_case,
    vars_case,
)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "frozen_experiment_cli", ROOT / "scripts/benchmark_agentic_models.py"
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def inputs():
    suite = RoleSuite(
        "frozen-fixture",
        tuple(
            RoleCase(
                f"case-{i}",
                "triage",
                f"Synthetic check {i}",
                "json_subset",
                {"ok": True},
                context={"atomic_kind": "check_exit_status", "index": i},
            )
            for i in range(4)
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
        for name in ("tiny", "strong")
    ]
    return suite, models


def evidence():
    return {
        key: None
        for key in (
            "candidate_package",
            "gold_review",
            "dataset_card",
            "deployment_bindings",
            "review_bundle",
        )
    }


def freeze(suite=None, models=None, **kwargs):
    default_suite, default_models = inputs()
    return freeze_atomic_experiment(
        suite or default_suite,
        models or default_models,
        variants=("generic",),
        repeat=2,
        shuffle_seed=42,
        evidence=evidence(),
        **kwargs,
    )


def report():
    suite, models = inputs()
    plan = freeze()
    rows = [
        observe_case(
            model=model,
            case=case,
            variant="generic",
            trial=trial,
            generate=lambda *_: Generation('{"ok":true}', 4, 5, 6, 7),
            retain_response=True,
        ).to_dict()
        for case in suite.cases
        for model in models
        for trial in (1, 2)
    ]
    return {
        "schema_version": 1,
        "suite_sha256": suite.identity,
        "model_targets_sha256": plan["targets_sha256"],
        "results": rows,
        "experiment_plan_sha256": plan["plan_sha256"],
        "qualified": False,
        "auto_promoted": False,
    }


def audit(document=None, plan=None):
    suite, models = inputs()
    return audit_atomic_experiment(
        plan or freeze(),
        report() if document is None else document,
        suite,
        models,
        evidence=evidence(),
    )


def test_freeze_omits_gold_text_and_endpoint_and_detaches_the_inputs():
    suite, models = inputs()
    private = evidence()
    private["gold_review"] = {"rationale": "PRIVATE_GOLD_CANARY"}
    plan = freeze_atomic_experiment(
        suite, models, variants=("generic",), repeat=1, shuffle_seed=0, evidence=private
    )
    encoded = canonical_json(plan)
    assert "PRIVATE_GOLD_CANARY" not in encoded
    assert "127.0.0.1" not in encoded and "Synthetic check" not in encoded
    assert len(plan["cells"]) == 8
    private["gold_review"]["rationale"] = "changed"
    assert canonical_json(plan) == encoded


@pytest.mark.parametrize(
    "field",
    [
        "suite_sha256",
        "targets_sha256",
        "cells",
        "evidence_sha256",
        "shuffle_seed",
        "repeat",
        "variants",
        "schema",
        "plan_sha256",
    ],
)
def test_rehashed_edits_do_not_redefine_the_experiment(field):
    suite, models = inputs()
    plan = freeze()
    plan[field] = [] if field in {"cells", "variants"} else "changed"
    if field != "plan_sha256":
        plan["plan_sha256"] = content_hash({k: v for k, v in plan.items() if k != "plan_sha256"})
    with pytest.raises(InvalidInputError):
        admit_atomic_experiment(
            plan,
            suite,
            models,
            variants=("generic",),
            repeat=2,
            shuffle_seed=42,
            evidence=evidence(),
        )


def test_bool_cannot_impersonate_a_frozen_integer():
    suite, models = inputs()
    for field in ("repeat", "shuffle_seed"):
        plan = freeze()
        plan[field] = True
        with pytest.raises(InvalidInputError):
            audit_atomic_experiment(plan, report(), suite, models, evidence=evidence())


def test_role_filtering_cannot_silently_drop_cases_or_targets():
    suite, models = inputs()
    with pytest.raises(InvalidInputError, match="Every frozen case"):
        freeze(replace(suite, cases=(replace(suite.cases[0], role="coding"),)))
    with pytest.raises(InvalidInputError, match="unused target"):
        freeze(models=[models[0], replace(models[1], roles=("coding",))])


def test_reworded_structured_inputs_are_still_duplicates():
    suite, _ = inputs()
    cases = tuple(
        replace(
            case, context={"atomic_kind": "check_exit_status", "reference_input": {"exit_code": 0}}
        )
        for case in suite.cases
    )
    with pytest.raises(InvalidInputError, match="distinct task inputs"):
        freeze(replace(suite, cases=cases))


def test_whole_matrix_bound_is_checked_before_prompt_building(monkeypatch):
    suite, models = inputs()
    suite = replace(
        suite,
        cases=tuple(replace(suite.cases[0], id=f"case-{i}", task=f"Case {i}") for i in range(200)),
    )
    models = [replace(models[0], id=f"target-{i}") for i in range(16)]
    from potbot.evaluation import agentic_experiments

    monkeypatch.setattr(
        agentic_experiments, "prompt_for_case", lambda *_: pytest.fail("no prompt allocation")
    )
    with pytest.raises(InvalidInputError, match="1600"):
        freeze(suite, models)


def test_complete_audit_uses_distinct_tasks_and_retains_unqualified_state():
    result = audit()
    assert result["planned_attempts"] == result["completed_attempts"] == 16
    assert result["total_billed_microunits"] == 112
    assert result["complete_for_analysis"] is True
    assert all(
        row["distinct_cases"] == 4 and row["all_repeats_passed_cases"] == 4
        for row in result["cohorts"]
    )
    assert result["qualified"] is result["prior_registration_authenticated"] is False
    assert len(result["uncovered_atomic_kinds"]) == 40
    assert result["automatic_routing_changes"] == 0


def test_missing_uncertain_mismatch_and_failed_outputs_keep_their_denominators():
    document = report()
    document["results"].pop()
    uncertain = document["results"][0]
    uncertain.update(
        status="indeterminate",
        response=None,
        output_sha256=None,
        score=None,
        elapsed_ms=None,
        input_tokens=None,
        output_tokens=None,
        billed_microunits=None,
    )
    mismatch = document["results"][1]
    mismatch.update(
        status="identity_mismatch",
        returned_model="unexpected",
        model_identity="mismatch",
        score=None,
    )
    failed = document["results"][2]
    failed.update(response='{"ok":false}', score=0)
    import hashlib

    failed["output_sha256"] = hashlib.sha256(failed["response"].encode()).hexdigest()
    result = audit(document)
    assert result["planned_attempts"] == 16
    assert (
        result["missing_attempts"]
        == result["indeterminate_attempts"]
        == result["identity_mismatch_attempts"]
        == result["failed_rubric_attempts"]
        == 1
    )
    assert result["complete_for_analysis"] is False
    assert result["total_billed_microunits"] is None
    assert result["observed_billed_subtotal_microunits"] == 98
    assert sum(row["distinct_cases"] for row in result["cohorts"]) == 8


@pytest.mark.parametrize(
    "edit", ["duplicate", "unknown", "wrong_plan", "no_outputs", "changed_target", "forged_score"]
)
def test_invalid_report_is_not_silently_filtered(edit):
    document = report()
    if edit == "duplicate":
        document["results"].append(copy.deepcopy(document["results"][0]))
    elif edit == "unknown":
        document["results"][0]["case_id"] = "not-planned"
    elif edit == "wrong_plan":
        document["experiment_plan_sha256"] = "0" * 64
    elif edit == "no_outputs":
        document["results"][0]["response"] = None
    elif edit == "changed_target":
        document["model_targets_sha256"] = "0" * 64
    else:
        document["results"][0]["score"] = 0
    with pytest.raises(InvalidInputError):
        audit(document)


def files(tmp_path):
    suite, models = inputs()
    for name, value in {
        "suite": {
            "id": suite.id,
            "schema_version": 1,
            "cases": [vars_case(case) for case in suite.cases],
        },
        "models": {"schema_version": 1, "models": [asdict(model) for model in models]},
    }.items():
        (tmp_path / f"{name}.json").write_text(canonical_json(value))
    return ["--suite", str(tmp_path / "suite.json"), "--models", str(tmp_path / "models.json")]


def forbidden(*_args, **_kwargs):
    pytest.fail("No transport or vault may be constructed")


def test_cli_freezes_and_audits_exclusively_without_a_transport(tmp_path, monkeypatch):
    flags = files(tmp_path)
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    plan_path, result_path = tmp_path / "plan.json", tmp_path / "results.json"
    prepare = [
        "prepare-experiment",
        *flags,
        "--variants",
        "generic",
        "--repeat",
        "2",
        "--output",
        str(plan_path),
    ]
    assert cli.main(prepare) == 0
    with pytest.raises(SystemExit):
        cli.main(prepare)
    result_path.write_text(canonical_json(report()))
    assert (
        cli.main(
            [
                "audit-experiment",
                *flags,
                "--experiment-plan",
                str(plan_path),
                "--results",
                str(result_path),
                "--output",
                str(tmp_path / "audit.json"),
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "flags", [["--repeat", "1"], ["--judge-derived"], ["--shuffle-seed", "7"], []]
)
def test_run_refuses_changed_design_unplanned_calls_or_missing_text_before_dispatch(
    tmp_path, monkeypatch, flags
):
    common = files(tmp_path)
    path = tmp_path / "plan.json"
    path.write_text(canonical_json(freeze()))
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    args = [
        "run",
        *common,
        "--experiment-plan",
        str(path),
        "--variants",
        "generic",
        "--repeat",
        "2",
        "--output-dir",
        str(tmp_path / "runs"),
        "--confirm-prompt-disclosure",
    ]
    if flags:
        args += ["--store-text", *flags]
    with pytest.raises(SystemExit):
        cli.main(args)
    assert not (tmp_path / "runs").exists()


def test_run_retains_plan_before_first_dispatch_and_offline_audit_never_replays(
    tmp_path, monkeypatch, capsys
):
    common = files(tmp_path)
    path = tmp_path / "plan.json"
    path.write_text(canonical_json(freeze()))
    calls = []

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def request(self, model, packet):
            runs = list((tmp_path / "runs").iterdir())
            assert len(runs) == 1
            saved = json.loads((runs[0] / "experiment-plan.json").read_text())
            assert saved == freeze()
            events = (runs[0] / "attempts.jsonl").read_text().splitlines()
            assert json.loads(events[-1])["phase"] == "possibly_dispatched"
            calls.append(model.id)
            if len(calls) == 1:
                raise OSError("lost synthetic reply")
            return Generation('{"ok":true}', 3)

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    assert (
        cli.main(
            [
                "run",
                *common,
                "--experiment-plan",
                str(path),
                "--variants",
                "generic",
                "--repeat",
                "2",
                "--output-dir",
                str(tmp_path / "runs"),
                "--store-text",
                "--confirm-prompt-disclosure",
            ]
        )
        == 0
    )
    run_path = Path(json.loads(capsys.readouterr().out)["report_dir"])
    assert len(calls) == 16
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    assert (
        cli.main(
            [
                "audit-experiment",
                *common,
                "--experiment-plan",
                str(path),
                "--results",
                str(run_path / "report.json"),
                "--output",
                str(tmp_path / "audit.json"),
            ]
        )
        == 2
    )
    result = json.loads((tmp_path / "audit.json").read_text())
    assert result["indeterminate_attempts"] == 1 and result["planned_attempts"] == 16
    assert result["total_billed_microunits"] is None
    assert len(calls) == 16
