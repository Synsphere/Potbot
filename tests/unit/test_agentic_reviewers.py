"""Synthetic protocol receipts for blind, paired reviewer calibration."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.evaluation.agentic_model_bench import (
    Generation,
    ModelTarget,
    RoleCase,
    RoleSuite,
    observe_case,
    prompt_for_case,
)
from potbot.evaluation.agentic_reviewers import (
    build_reviewer_bundle,
    calibrate_reviewers,
    reviewer_suite,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "reviewer_benchmark_cli", ROOT / "scripts/benchmark_agentic_models.py"
)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)


def model(name, *, roles=("evidence",), revision="source-revision-1"):
    return ModelTarget(
        name, name, "loopback_openai", roles, revision, base_url="http://127.0.0.1:11434/v1"
    )


def source(count=12, repeat=1):
    targets = [model("source-first"), model("source-second")]
    suite = RoleSuite(
        "synthetic-source",
        tuple(
            RoleCase(
                f"case-{index}",
                "evidence",
                f"Return value {index}",
                "json_subset",
                {"value": index},
                context={"atomic_kind": "extract_field", "fixture_index": index},
            )
            for index in range(count)
        ),
    )
    rows = []
    for case in suite.cases:
        for target in targets:
            for trial in range(1, repeat + 1):
                # Deliberately synthetic observations, never an inference claim.
                answer = case.expected if target == targets[0] else {"value": -1}
                rows.append(
                    observe_case(
                        model=target,
                        case=case,
                        variant="generic",
                        trial=trial,
                        generate=lambda _m, _p, answer=answer: Generation(json.dumps(answer), 7),
                        retain_response=True,
                    ).to_dict()
                )
    report = {
        "schema_version": 1,
        "suite_sha256": suite.identity,
        "results": rows,
        "qualified": False,
        "auto_promoted": False,
    }
    return suite, targets, report


def bundle_from(suite, targets, report, **kwargs):
    return build_reviewer_bundle(
        report,
        suite,
        targets,
        first=targets[0].id,
        second=targets[1].id,
        variants=("generic",),
        **kwargs,
    )


def frozen(count=12):
    return bundle_from(*source(count))


def reviewer_report(bundle, *, choice="gold", repeat=1, target=None):
    suite, _, _ = reviewer_suite(bundle)
    target = target or model("reviewer", roles=("judge",), revision="reviewer-revision-2")
    rows = []
    for case in suite.cases:
        for trial in range(1, repeat + 1):
            text = json.dumps(case.expected) if choice == "gold" else choice
            rows.append(
                observe_case(
                    model=target,
                    case=case,
                    variant="generic",
                    trial=trial,
                    generate=lambda _m, _p, text=text: Generation(text, 11),
                    retain_response=True,
                ).to_dict()
            )
    return target, {
        "schema_version": 1,
        "suite_sha256": suite.identity,
        "results": rows,
        "review_bundle_sha256": bundle["bundle_sha256"],
        "qualified": False,
        "auto_promoted": False,
    }


def calibrate(bundle, report, targets, **kwargs):
    return calibrate_reviewers(bundle, report, targets, variants=("generic",), **kwargs)


def resign(bundle):
    bundle["bundle_sha256"] = content_hash(
        {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    )
    return bundle


def test_frozen_suite_is_blind_and_gold_never_enters_request_packets():
    bundle = frozen()
    suite, pairs, subjects = reviewer_suite(json.loads(json.dumps(bundle)))
    assert len(suite.cases) == 24 and len(pairs) == 12
    for case in suite.cases:
        prompt = prompt_for_case("reviewer", case, "generic")
        assert all(subject.model not in prompt.user for subject in subjects)
        assert "reference_winner" not in prompt.user and "expected" not in prompt.user
        assert set(case.context) == {"atomic_kind", "review_pair_sha256", "order"}
    assert suite.cases[0].expected == {"choice": "A"}
    assert suite.cases[1].expected == {"choice": "B"}


def test_perfect_synthetic_reviewer_counts_sources_once_despite_orders_and_repeats():
    bundle = frozen()
    target, report = reviewer_report(bundle, repeat=3)
    result = calibrate(bundle, report, [target], repeat=3)
    row = result["reviewers"][0]
    assert row["declared_source_tasks"] == row["complete_valid_source_tasks"] == 12
    assert row["symmetric_correct_source_tasks"] == 12
    assert row["observed_valid_verdicts"] == 72
    assert row["symmetric_reference_accuracy"] == 1
    assert row["first_position_choice_rate_observed"] == 0.5
    assert row["wrong_answer_approval_rate_observed"] == 0
    assert row["comparison_ready"] is True
    assert row["measurements"]["input_tokens"] is None
    assert (
        result["gold_review_authenticated"]
        is result["deployment_qualified"]
        is result["qualified"]
        is False
    )
    assert result["automatic_routing_changes"] == 0


def test_always_first_position_exposes_wrong_answer_approval_and_inconsistent_pairs():
    bundle = frozen()
    target, report = reviewer_report(bundle, choice='{"choice":"A"}')
    row = calibrate(bundle, report, [target])["reviewers"][0]
    assert row["symmetric_reference_accuracy"] == 0
    assert row["observed_wrong_answer_approvals"] == 12
    assert row["wrong_answer_approval_rate_observed"] == 0.5
    assert row["position_inconsistent_source_tasks"] == 12
    assert row["first_position_choice_rate_observed"] == 1


@pytest.mark.parametrize("count", [1, 3, 11])
def test_repeated_verdicts_never_satisfy_distinct_task_minimum(count):
    bundle = frozen(count)
    target, report = reviewer_report(bundle, repeat=5)
    row = calibrate(bundle, report, [target], repeat=5)["reviewers"][0]
    assert row["complete_valid_source_tasks"] == count
    assert row["comparison_ready"] is False


@pytest.mark.parametrize("change", ["missing", "indeterminate"])
def test_incomplete_matrix_remains_visible_without_inventing_a_failure_score(change):
    bundle = frozen()
    target, report = reviewer_report(bundle)
    if change == "missing":
        report["results"].pop()
    else:
        row = report["results"][-1]
        row["status"] = "indeterminate"
        for field in (
            "response",
            "score",
            "output_sha256",
            "elapsed_ms",
            "input_tokens",
            "output_tokens",
            "billed_microunits",
        ):
            row[field] = None
    row = calibrate(bundle, report, [target])["reviewers"][0]
    assert row["complete_valid_source_tasks"] == 11
    assert row["symmetric_reference_accuracy"] == 1
    assert row["comparison_ready"] is False
    assert row["missing_attempts" if change == "missing" else "indeterminate_attempts"] == 1


@pytest.mark.parametrize(
    "verdict",
    [
        '{"choice":"A","reason":"extra field"}',
        '{"choice":"B","choice":"A"}',
        '{"choice":"a"}',
        '```json\n{"choice":"A"}\n```',
        "[]",
        '{"choice":[]}',
    ],
)
def test_malformed_or_ambiguous_verdict_is_not_a_valid_approval(verdict):
    bundle = frozen()
    target, report = reviewer_report(bundle, choice=verdict)
    row = calibrate(bundle, report, [target])["reviewers"][0]
    assert row["malformed_verdicts"] == 24
    assert row["complete_valid_source_tasks"] == row["observed_valid_verdicts"] == 0
    assert row["symmetric_reference_accuracy"] is row["wrong_answer_approval_rate_observed"] is None
    assert row["comparison_ready"] is False


def test_source_alias_cannot_judge_itself_even_at_a_different_endpoint():
    bundle = frozen()
    _, _, subjects = reviewer_suite(bundle)
    alias = replace(subjects[0], id="alias", roles=("judge",), base_url="http://127.0.0.1:9000/v1")
    _, report = reviewer_report(bundle, target=alias)
    with pytest.raises(InvalidInputError, match="own source answer"):
        calibrate(bundle, report, [alias])


def test_same_weights_at_another_backend_cannot_be_an_independent_reviewer():
    bundle = frozen()
    _, _, subjects = reviewer_suite(bundle)
    alias = ModelTarget(
        "hosted-alias",
        subjects[0].model,
        "hosted",
        ("judge",),
        subjects[0].revision,
        provider="openai",
    )
    _, report = reviewer_report(bundle, target=alias)
    with pytest.raises(InvalidInputError, match="own source answer"):
        calibrate(bundle, report, [alias])


@pytest.mark.parametrize(
    "mutation", ["duplicate", "score", "output", "revision", "prompt", "identity_type"]
)
def test_reviewer_receipt_tampering_is_rejected(mutation):
    bundle = frozen()
    target, report = reviewer_report(bundle)
    row = report["results"][0]
    if mutation == "duplicate":
        report["results"].append(copy.deepcopy(row))
    elif mutation == "score":
        row["score"] = 0
    elif mutation == "output":
        row["response"] = '{"choice":"B"}'
    elif mutation == "revision":
        row["revision"] = "changed"
    elif mutation == "prompt":
        row["prompt_sha256"] = "0" * 64
    else:
        row["case_id"] = []
    with pytest.raises(InvalidInputError):
        calibrate(bundle, report, [target])


@pytest.mark.parametrize(
    "mutation", ["digest", "gold", "suite", "source_input", "duplicate_source"]
)
def test_frozen_bundle_cannot_silently_change_references_or_inflate_sources(mutation):
    bundle = frozen()
    if mutation == "digest":
        bundle["bundle_sha256"] = "0" * 64
    elif mutation == "gold":
        bundle["gold_review_authenticated"] = True
        resign(bundle)
    elif mutation == "suite":
        bundle["suite"]["cases"][0]["expected"] = {"choice": "B"}
        resign(bundle)
    elif mutation == "source_input":
        bundle["pairs"][0]["source_input_sha256"] = "0" * 64
        from potbot.evaluation.agentic_reviewers import _pair_identity

        bundle["pairs"][0]["pair_id"] = _pair_identity(bundle["pairs"][0])
        resign(bundle)
    else:
        bundle["pairs"].append(copy.deepcopy(bundle["pairs"][0]))
        resign(bundle)
    with pytest.raises(InvalidInputError):
        reviewer_suite(bundle)


def test_fixed_source_cell_never_searches_for_a_better_trial():
    suite, targets, report = source(repeat=2)
    for row in report["results"]:
        if row["trial"] == 2:
            # Mark both outputs correct by reusing correctly regraded observations.
            if row["model_id"] == targets[1].id:
                case = next(case for case in suite.cases if case.id == row["case_id"])
                replacement = observe_case(
                    model=targets[1],
                    case=case,
                    variant="generic",
                    trial=2,
                    generate=lambda _m, _p, answer=case.expected: Generation(json.dumps(answer), 1),
                    retain_response=True,
                )
                row.update(replacement.to_dict())
    first = bundle_from(suite, targets, report, repeat=2, source_trial=1)
    assert len(first["pairs"]) == 12
    with pytest.raises(InvalidInputError, match="eligible source tasks"):
        bundle_from(suite, targets, report, repeat=2, source_trial=2)


def test_builder_refuses_to_truncate_more_than_100_eligible_sources():
    with pytest.raises(InvalidInputError, match="no truncation"):
        bundle_from(*source(101))


def test_unknown_reviewer_revision_withholds_comparison_readiness():
    bundle = frozen()
    target = model("reviewer", roles=("judge",), revision="unknown")
    _, report = reviewer_report(bundle, target=target)
    row = calibrate(bundle, report, [target])["reviewers"][0]
    assert row["symmetric_reference_accuracy"] == 1
    assert row["comparison_ready"] is False


def _args(**overrides):
    from argparse import Namespace

    return Namespace(
        **{
            "repeat": 1,
            "confirm_prompt_disclosure": True,
            "allow_paid": False,
            "max_paid_calls": 0,
            "max_cost_microunits": 0,
            "shuffle_seed": 42,
            "judge_derived": False,
            "max_judge_pairs": 4,
            "min_samples": 12,
            "store_text": True,
            **overrides,
        }
    )


def _blocked(*_args, **_kwargs):
    raise AssertionError("network and credential construction is forbidden in this test")


def test_cli_builds_and_calibrates_private_frozen_files_without_transport(
    monkeypatch, tmp_path, capsys
):
    from dataclasses import asdict

    from potbot.evaluation.agentic_model_bench import vars_case

    suite, targets, report = source()
    suite_file, models_file, report_file = [
        tmp_path / name
        for name in ("source-suite.json", "source-models.json", "source-report.json")
    ]
    suite_file.write_text(
        json.dumps(
            {"schema_version": 1, "id": suite.id, "cases": [vars_case(c) for c in suite.cases]}
        )
    )
    models_file.write_text(
        json.dumps({"schema_version": 1, "models": [asdict(m) for m in targets]})
    )
    report_file.write_text(json.dumps(report))
    monkeypatch.setattr(cli, "BenchmarkTransport", _blocked)
    monkeypatch.setattr(cli, "ServiceCredentials", _blocked)
    folder = tmp_path / "frozen"
    argv = [
        "build-review-suite",
        "--suite",
        str(suite_file),
        "--models",
        str(models_file),
        "--results",
        str(report_file),
        "--first",
        targets[0].id,
        "--second",
        targets[1].id,
        "--variants",
        "generic",
        "--output-dir",
        str(folder),
    ]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["source_tasks"] == 12
    bundle_path = folder / "review-bundle.json"
    frozen_bundle = json.loads(bundle_path.read_text())
    checked, _, _ = reviewer_suite(frozen_bundle)
    assert RoleSuite.from_dict(json.loads((folder / "review-suite.json").read_text())) == checked
    before = bundle_path.read_bytes()
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2 and bundle_path.read_bytes() == before
    reviewer, rows = reviewer_report(frozen_bundle)
    reviewer_models = tmp_path / "reviewer-models.json"
    reviewer_models.write_text(json.dumps({"schema_version": 1, "models": [asdict(reviewer)]}))
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(json.dumps(rows))
    output = tmp_path / "calibration.json"
    assert (
        cli.main(
            [
                "calibrate-reviewers",
                "--review-bundle",
                str(bundle_path),
                "--results",
                str(verdicts),
                "--models",
                str(reviewer_models),
                "--variants",
                "generic",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["reviewers"][0]["complete_valid_source_tasks"] == 12


def test_runner_refuses_self_review_before_transport_construction(monkeypatch, tmp_path):
    bundle = frozen()
    suite, _, subjects = reviewer_suite(bundle)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    alias = replace(subjects[0], id="reviewer-alias", roles=("judge",))
    monkeypatch.setattr(cli, "BenchmarkTransport", _blocked)
    monkeypatch.setattr(cli, "ServiceCredentials", _blocked)
    with pytest.raises(InvalidInputError, match="own source answer"):
        cli._execute(_args(review_bundle=path), suite, [alias], ["generic"], None)
    with pytest.raises(InvalidInputError, match="exact provenance bundle"):
        cli._execute(_args(), suite, [model("reviewer", roles=("judge",))], ["generic"], None)


def test_runner_retains_exact_bundle_binding_for_subsequent_calibration(monkeypatch, tmp_path):
    bundle = frozen()
    suite, _, _ = reviewer_suite(bundle)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    calls = []

    class FakeTransport:
        def __init__(self, **kwargs):
            pass

        def request(self, target, prompt):
            calls.append(target.id)
            return Generation('{"choice":"A"}', 1)

    monkeypatch.setattr(cli, "BenchmarkTransport", FakeTransport)
    target = model("reviewer", roles=("judge",))
    report = cli._execute(_args(review_bundle=path), suite, [target], ["generic"], None)
    assert len(calls) == 24
    assert report["review_bundle_sha256"] == bundle["bundle_sha256"]
    assert (
        calibrate(bundle, report, [target])["reviewers"][0]["position_inconsistent_source_tasks"]
        == 12
    )
    report["review_bundle_sha256"] = "0" * 64
    with pytest.raises(InvalidInputError, match="bundle identity"):
        calibrate(bundle, report, [target])


def test_legacy_derived_mode_also_refuses_a_subject_under_another_target_id(monkeypatch):
    suite, targets, _ = source(1)
    alias = replace(targets[0], id="alias", roles=("judge",))
    calls = []

    class FakeTransport:
        def __init__(self, **kwargs):
            pass

        def request(self, target, prompt):
            calls.append(target.id)
            return Generation('{"value":0}' if target == targets[0] else '{"value":-1}', 1)

    monkeypatch.setattr(cli, "BenchmarkTransport", FakeTransport)
    report = cli._execute(_args(judge_derived=True), suite, [*targets, alias], ["generic"], None)
    assert alias.id not in calls
    assert len(report["results"]) == 2


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("development", "development_case"),
        ("structural", "structural_only_reference"),
        ("missing", "missing_source_attempt"),
        ("uncertain", "indeterminate_source_attempt"),
        ("agreement", "no_objective_pass_fail_pair"),
        ("oversized", "bounded_reviewer_packet_exceeded"),
    ],
)
def test_excluded_source_cases_have_explicit_dispositions(change, reason):
    suite, targets, report = source()
    cases = list(suite.cases)
    if change == "development":
        cases[0] = replace(cases[0], split="development")
    elif change == "structural":
        cases[0] = replace(cases[0], grader="json_shape", expected={"value": "integer"})
    elif change == "oversized":
        cases[0] = replace(cases[0], task="x" * 12500)
    suite = replace(suite, cases=tuple(cases))
    report["suite_sha256"] = suite.identity
    rebuilt = []
    for raw in report["results"]:
        case = next(case for case in cases if case.id == raw["case_id"])
        target = next(target for target in targets if target.id == raw["model_id"])
        text = '{"value":0}' if change == "agreement" and case == cases[0] else raw["response"]
        rebuilt.append(
            observe_case(
                model=target,
                case=case,
                variant="generic",
                trial=1,
                generate=lambda _m, _p, text=text: Generation(text, 7),
                retain_response=True,
            ).to_dict()
        )
    report["results"] = rebuilt
    if change == "missing":
        report["results"].pop(0)
    elif change == "uncertain":
        row = report["results"][0]
        row["status"] = "indeterminate"
        for field in (
            "response",
            "score",
            "output_sha256",
            "elapsed_ms",
            "input_tokens",
            "output_tokens",
            "billed_microunits",
        ):
            row[field] = None
    bundle = bundle_from(suite, targets, report)
    assert len(bundle["pairs"]) == 11
    assert bundle["excluded"] == [{"case_id": cases[0].id, "reason": reason}]
    reviewer_suite(bundle)


def test_builder_rejects_repeated_task_inputs_before_choosing_pairs():
    suite, targets, report = source()
    cases = list(suite.cases)
    cases[1] = replace(cases[1], task=cases[0].task, context=cases[0].context)
    suite = replace(suite, cases=tuple(cases))
    report["suite_sha256"] = suite.identity
    with pytest.raises(InvalidInputError, match="Repeated task inputs"):
        bundle_from(suite, targets, report)
