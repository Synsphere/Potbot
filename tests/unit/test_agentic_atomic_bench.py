"""Paired holdout evidence cannot silently become a deployment or route grant."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_atomic_bench import compare_atomic
from potbot.evaluation.agentic_model_bench import (
    Generation,
    ModelTarget,
    RoleCase,
    RoleSuite,
    observe_case,
)


def evidence(count=12, repeat=1):
    suite = RoleSuite(
        "atomic-fixture-v1",
        tuple(
            RoleCase(
                f"case-{i}",
                "status",
                f"Read process receipt {i}; report its status.",
                "json_subset",
                {"status": "passed"},
                context={"atomic_kind": "check_exit_status"},
            )
            for i in range(count)
        ),
    )
    targets = [
        ModelTarget(
            name,
            name,
            "loopback_openai",
            ("status",),
            "fixture-revision-1",
            base_url="http://127.0.0.1:11434/v1",
        )
        for name in ("baseline", "candidate")
    ]
    rows = [
        observe_case(
            model=model,
            case=case,
            variant="generic",
            trial=trial,
            generate=lambda *_: Generation('{"status":"passed"}', 10),
            retain_response=True,
        ).to_dict()
        for case in suite.cases
        for model in targets
        for trial in range(1, repeat + 1)
    ]
    return (
        suite,
        targets,
        {
            "schema_version": 1,
            "suite_sha256": suite.identity,
            "qualified": False,
            "auto_promoted": False,
            "results": rows,
        },
    )


def compare(data, suite, models, **kwargs):
    return compare_atomic(
        data,
        suite,
        models,
        baseline="baseline",
        candidate="candidate",
        variants=("generic",),
        **kwargs,
    )


def test_complete_paired_matrix_is_advisory_and_missing_usage_is_unknown():
    suite, models, data = evidence()
    report = compare(data, suite, models)
    row = report["comparisons"][0]
    assert row["comparison_ready"] is True
    assert row["paired_objective_cases"] == 12
    assert row["candidate_minus_baseline_accuracy"] == 0
    assert row["paired_interval_95"][0] < 0 < row["paired_interval_95"][1]
    assert row["candidate"]["p95_latency_ms"] == 10
    assert row["candidate"]["billed_microunits"] is None
    assert len(report["uncovered_atomic_kinds"]) == 40
    assert report["qualified"] is False
    assert report["automatic_routing_changes"] == 0
    assert "response" not in report


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_sha256", "0" * 64),
        ("input_sha256", "0" * 64),
        ("output_sha256", "0" * 64),
        ("score", 0),
        ("score", True),
        ("model", "substitute"),
        ("revision", "substitute"),
        ("backend", "hosted"),
        ("trial", True),
        ("trial", 2),
        ("role", "coding"),
        ("case_id", "invented"),
        ("input_tokens", -1),
        ("elapsed_ms", True),
        ("variant", "family"),
        ("response", None),
        ("status", "skipped"),
        ("grader", "json_shape"),
    ],
)
def test_tampered_receipts_and_unretained_outputs_are_refused(field, value):
    suite, models, data = evidence()
    data["results"][0][field] = value
    with pytest.raises(InvalidInputError):
        compare(data, suite, models)


def test_duplicate_receipt_is_rejected_and_missing_row_is_a_visible_hold():
    suite, models, data = evidence()
    duplicate = deepcopy(data)
    duplicate["results"].append(duplicate["results"][0])
    with pytest.raises(InvalidInputError):
        compare(duplicate, suite, models)
    data["results"].pop()
    row = compare(data, suite, models)["comparisons"][0]
    assert row["missing_attempts"] == 1
    assert row["paired_objective_cases"] == 11
    assert row["comparison_ready"] is False


def test_repeated_trials_do_not_turn_four_tasks_into_twelve_holdouts():
    suite, models, data = evidence(4, repeat=3)
    row = compare(data, suite, models, repeat=3)["comparisons"][0]
    assert row["paired_objective_cases"] == 4
    assert row["comparison_ready"] is False


def test_uncertain_attempts_are_not_scored_as_zero_or_replayed():
    suite, models, data = evidence()
    data["results"][0].update(
        {
            "status": "indeterminate",
            "score": None,
            "response": None,
            "output_sha256": None,
            "elapsed_ms": None,
        }
    )
    row = compare(data, suite, models)["comparisons"][0]
    assert row["indeterminate_attempts"] == 1
    assert row["paired_objective_cases"] == 11
    assert row["comparison_ready"] is False


def test_suite_aliases_and_duplicate_task_inputs_cannot_inflate_evidence():
    suite, models, data = evidence()
    duplicate = replace(suite.cases[0], id="development-alias", split="development")
    changed = RoleSuite(suite.id, suite.cases + (duplicate,))
    data["suite_sha256"] = changed.identity
    with pytest.raises(InvalidInputError, match="Repeated task"):
        compare(data, changed, models)
    with pytest.raises(InvalidInputError):
        compare(data, suite, models)


def test_unknown_atomic_kind_and_unknown_revision_do_not_acquire_readiness():
    suite, models, data = evidence()
    models[0] = replace(models[0], revision="unknown")
    for row in data["results"]:
        if row["model_id"] == "baseline":
            row["revision"] = "unknown"
    assert compare(data, suite, models)["comparisons"][0]["comparison_ready"] is False
    suite = replace(suite, cases=(replace(suite.cases[0], context={"atomic_kind": "invented"}),))
    data["suite_sha256"] = suite.identity
    with pytest.raises(InvalidInputError, match="existing atomic kind"):
        compare(data, suite, models)


def test_output_is_regraded_and_a_real_failure_changes_the_paired_difference():
    suite, models, data = evidence()
    first = observe_case(
        model=models[0],
        case=suite.cases[0],
        variant="generic",
        trial=1,
        generate=lambda *_: Generation('{"status":"failed"}', 20),
        retain_response=True,
    )
    data["results"][0] = first.to_dict()
    row = compare(data, suite, models)["comparisons"][0]
    assert row["candidate_minus_baseline_accuracy"] == pytest.approx(1 / 12)


def test_atomic_comparison_cli_is_offline_and_never_overwrites(monkeypatch, tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts/benchmark_agentic_models.py"
    spec = importlib.util.spec_from_file_location("atomic_cli_test", path)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    suite, models, data = evidence()
    import json

    for name, value in (
        ("suite", {"schema_version": 1, "id": suite.id, "cases": [asdict(c) for c in suite.cases]}),
        ("models", {"schema_version": 1, "models": [asdict(m) for m in models]}),
        ("results", data),
    ):
        if name == "models":
            for model in value["models"]:
                model["roles"] = list(model["roles"])
        (tmp_path / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")

    def refuse(*args, **kwargs):
        raise AssertionError("Comparison must not construct an inference transport")

    monkeypatch.setattr(cli, "BenchmarkTransport", refuse)
    output = tmp_path / "comparison.json"
    args = [
        "compare-atomic",
        "--suite",
        str(tmp_path / "suite.json"),
        "--models",
        str(tmp_path / "models.json"),
        "--results",
        str(tmp_path / "results.json"),
        "--baseline",
        "baseline",
        "--candidate",
        "candidate",
        "--variants",
        "generic",
        "--output",
        str(output),
    ]
    assert cli.main(args) == 0
    assert json.loads(output.read_text())["qualified"] is False
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
