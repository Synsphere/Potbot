"""Exact offline bindings and stopped, accounted, non-replayed identity mismatches."""

from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_atomic_bench import compare_atomic, validated_atomic_observations
from potbot.evaluation.agentic_deployments import inspect_benchmark_deployments
from potbot.evaluation.agentic_model_bench import (
    Generation,
    ModelTarget,
    RoleCase,
    RoleSuite,
    observe_case,
    summarise,
)
from potbot.models import preflight

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load("deployment_cli_test", ROOT / "scripts/benchmark_agentic_models.py")
fixtures = load("deployment_preflight_fixtures", ROOT / "tests/unit/test_model_preflight.py")


def target(name="local", **changes):
    return replace(
        ModelTarget(
            name,
            "microsoft/Phi-4-mini-instruct",
            "loopback_openai",
            ("status",),
            "a" * 40,
            base_url="http://127.0.0.1:11434/v1",
            max_output_tokens=128,
        ),
        **changes,
    )


def suite(count=4):
    return RoleSuite(
        "binding-protocol-fixture",
        tuple(
            RoleCase(
                f"case-{index}",
                "status",
                f"Read fixture receipt {index}",
                "json_subset",
                {"status": "passed"},
                context={"atomic_kind": "check_exit_status"},
            )
            for index in range(count)
        ),
    )


def bindings(profile):
    return {
        "schema": "potbot.benchmark-deployment-bindings/1",
        "bindings": [{"model_id": "local", "host_profile": profile.to_dict()}],
    }


def args(**changes):
    return Namespace(
        **{
            "repeat": 1,
            "dataset_card": None,
            "review_bundle": None,
            "confirm_prompt_disclosure": True,
            "allow_paid": False,
            "max_paid_calls": 0,
            "max_cost_microunits": 0,
            "judge_derived": False,
            "max_judge_pairs": 8,
            "shuffle_seed": 42,
            "min_samples": 12,
            "store_text": True,
            **changes,
        }
    )


def forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected transport, credential or provider access")


def test_binding_reuses_exact_host_inventory_without_claiming_server_or_model_loading(tmp_path):
    profile = fixtures.host_profile(tmp_path)
    result = inspect_benchmark_deployments(
        bindings(profile),
        [target()],
        suite(),
        project_root=tmp_path,
        observe_host=lambda _: fixtures.observation(),
    )
    assert result["status"] == "ready_for_load_check"
    row = result["targets"][0]
    assert row["host_profile_sha256"] == profile.fingerprint
    assert row["deployment_sha256"] == profile.deployment.fingerprint
    assert row["preflight"]["checked_files"] == 5
    assert row["preflight"]["host_observation"]["origin"] == "fixture"
    for flag in (
        "qualified",
        "generation_tested",
        "loading_tested",
        "server_loaded_identity_authenticated",
        "licence_acceptance_authenticated",
    ):
        assert result[flag] is False


@pytest.mark.parametrize(
    "change",
    [
        {"model": "different/model"},
        {"revision": "b" * 40},
        {"max_output_tokens": 129},
        {"roles": ("coding",)},
    ],
)
def test_changed_target_cannot_borrow_an_existing_deployment(tmp_path, change):
    profile = fixtures.host_profile(tmp_path)
    tasks = (
        suite()
        if "roles" not in change
        else RoleSuite("code", (RoleCase("code", "coding", "Return code", "exact_text", "ok"),))
    )
    with pytest.raises(InvalidInputError, match="exact deployment"):
        inspect_benchmark_deployments(
            bindings(profile),
            [target(**change)],
            tasks,
            project_root=tmp_path,
            observe_host=lambda _: fixtures.observation(),
        )


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unknown", "hosted"])
def test_bindings_require_unique_complete_local_target_coverage(tmp_path, mutation):
    profile = fixtures.host_profile(tmp_path)
    document = bindings(profile)
    targets = [target()]
    if mutation == "duplicate":
        document["bindings"] *= 2
    elif mutation == "missing":
        targets.append(target("other"))
    elif mutation == "unknown":
        document["bindings"][0]["model_id"] = "unknown"
    else:
        targets = [replace(target(), backend="hosted", provider="openai", base_url=None)]
    with pytest.raises(InvalidInputError):
        inspect_benchmark_deployments(
            document,
            targets,
            suite(),
            project_root=tmp_path,
            observe_host=lambda _: fixtures.observation(),
        )


def test_drifted_files_block_before_constructing_any_transport_or_credential(tmp_path, monkeypatch):
    profile = fixtures.host_profile(tmp_path)
    (tmp_path / profile.model_directory / "model-01.safetensors").write_bytes(b"changed")
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(bindings(profile)))
    monkeypatch.setattr(preflight, "inspect_local_host", lambda _: fixtures.observation())
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    config = args(deployment_bindings=path, project_root=tmp_path)
    report = cli._deployment_admission(config, suite(), [target()])
    assert report["status"] == "blocked"
    assert report["targets"][0]["preflight"]["blockers"]
    with pytest.raises(InvalidInputError, match="blocked"):
        cli._execute(config, suite(), [target()], ["generic"], None)


def test_deployment_file_and_owning_root_are_required_together(tmp_path):
    assert cli._deployment_admission(args(), suite(), [target()])["status"] == "unbound"
    for extra in ({"project_root": tmp_path}, {"deployment_bindings": tmp_path / "missing"}):
        with pytest.raises(InvalidInputError, match="required together"):
            cli._deployment_admission(args(**extra), suite(), [target()])


@pytest.mark.parametrize(
    "returned,identity,status,score",
    [
        (None, "unknown", "completed", 1),
        ("microsoft/Phi-4-mini-instruct", "matched", "completed", 1),
        ("substituted/model", "mismatch", "identity_mismatch", None),
    ],
)
def test_output_identity_retains_observed_bytes_timing_and_cost(returned, identity, status, score):
    row = observe_case(
        model=target(),
        case=suite().cases[0],
        variant="generic",
        trial=1,
        generate=lambda *_: Generation(
            '{"status":"passed"}',
            17,
            8,
            3,
            9,
            returned_model=returned,
            origin="synthetic_transport",
        ),
        retain_response=True,
    )
    assert (row.model_identity, row.status, row.score) == (identity, status, score)
    assert row.returned_model == returned
    assert row.generation_origin == "synthetic_transport"
    assert (row.elapsed_ms, row.input_tokens, row.output_tokens, row.billed_microunits) == (
        17,
        8,
        3,
        9,
    )
    assert row.response == '{"status":"passed"}' and row.output_sha256
    summary = summarise([row])
    assert summary["rows"][0]["measured_billed_microunits"] == 9
    assert summary["rows"][0]["mismatched_model_identity_attempts"] == (identity == "mismatch")


def test_mismatch_stops_the_matrix_before_any_judge_or_second_dispatch(monkeypatch):
    calls, events = [], []

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def request(self, model, prompt):
            calls.append(model.id)
            return Generation('{"status":"passed"}', 17, 8, 3, 9, "substitute")

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    report = cli._execute(
        args(judge_derived=True), suite(), [target()], ["generic"], None, on_event=events.append
    )
    assert calls == ["local"]
    assert [event["phase"] for event in events] == ["possibly_dispatched", "terminal"]
    assert events[-1]["result"]["billed_microunits"] == 9
    assert report["execution"]["status"] == "stopped_model_identity_mismatch"
    assert report["execution"]["unattempted_primary_attempts"] == 3
    assert report["execution"]["no_automatic_replay"] is True
    assert report["qualified"] is report["auto_promoted"] is False


def test_cli_persists_partial_report_and_journal_then_returns_failure(
    tmp_path, monkeypatch, capsys
):
    class Transport:
        def __init__(self, **_kwargs):
            pass

        def request(self, *_args):
            return Generation('{"status":"passed"}', 17, 8, 3, 9, "substitute")

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    (tmp_path / "models.json").write_text(
        json.dumps({"schema_version": 1, "models": [asdict(target())]})
    )
    tasks = suite()
    (tmp_path / "suite.json").write_text(
        json.dumps({"schema_version": 1, "id": tasks.id, "cases": [asdict(c) for c in tasks.cases]})
    )
    result = cli.main(
        [
            "run",
            "--models",
            str(tmp_path / "models.json"),
            "--suite",
            str(tmp_path / "suite.json"),
            "--output-dir",
            str(tmp_path / "runs"),
            "--variants",
            "generic",
            "--store-text",
            "--confirm-prompt-disclosure",
        ]
    )
    assert result == 2
    location = Path(json.loads(capsys.readouterr().out)["report_dir"])
    report = json.loads((location / "report.json").read_text())
    assert report["execution"]["status"] == "stopped_model_identity_mismatch"
    assert report["results"][0]["billed_microunits"] == 9
    assert len((location / "attempts.jsonl").read_text().splitlines()) == 2
    assert (location / "comparison.html").is_file()


def test_paired_analysis_accepts_mismatch_receipt_but_withholds_quality_and_readiness():
    tasks = suite()
    models = [target(), target("other", model="other/model")]
    rows = [
        observe_case(
            model=model,
            case=case,
            variant="generic",
            trial=1,
            generate=lambda m, _: Generation(
                '{"status":"passed"}',
                17,
                8,
                3,
                9,
                returned_model="substitute" if m.id == "local" else m.model,
            ),
            retain_response=True,
        ).to_dict()
        for case in tasks.cases
        for model in models
    ]
    document = {
        "schema_version": 1,
        "suite_sha256": tasks.identity,
        "qualified": False,
        "auto_promoted": False,
        "results": rows,
    }
    result = compare_atomic(
        document,
        tasks,
        models,
        baseline="local",
        candidate="other",
        variants=("generic",),
        minimum_cases=4,
    )
    row = result["comparisons"][0]
    assert row["comparison_ready"] is False
    assert row["paired_objective_cases"] == 0
    assert row["indeterminate_attempts"] == 4
    assert rows[0]["billed_microunits"] == 9
    document["results"][0]["status"] = "completed"
    with pytest.raises(InvalidInputError, match="completed attribution"):
        validated_atomic_observations(document, tasks, models, variants=("generic",), repeat=1)
