"""Reference source pins, gold separation, fixture leakage and actual CLI receipts."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_dataset import audit_atomic_dataset
from potbot.evaluation.agentic_model_bench import (
    ModelTarget,
    RoleCase,
    RoleSuite,
    observe_case,
    prompt_for_case,
    summarise,
)
from potbot.evaluation.agentic_model_transport import BenchmarkTransport
from potbot.evaluation.agentic_reference import (
    REFERENCE_MODEL,
    REFERENCE_ROLES,
    evaluate_reference,
    reference_identity,
)

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "examples/agentic-benchmark"
SPEC = importlib.util.spec_from_file_location(
    "reference_benchmark_cli", ROOT / "scripts/benchmark_agentic_models.py"
)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)


def target():
    return ModelTarget(
        "reference", REFERENCE_MODEL, "deterministic", REFERENCE_ROLES, reference_identity()
    )


def dataset():
    suite = RoleSuite.from_dict(
        json.loads((DATA / "atomic-reference-suite.json").read_text(encoding="utf-8"))
    )
    card = json.loads((DATA / "atomic-reference-card.json").read_text(encoding="utf-8"))
    return suite, card


def forbidden(*_args, **_kwargs):
    raise AssertionError("pure references must never open providers or credentials")


def test_all_literal_fixtures_run_without_network_or_gold_input():
    suite, card = dataset()
    audit = audit_atomic_dataset(suite, card)
    assert audit["case_count"] == 56
    assert len(audit["uncovered_atomic_kinds"]) == 37
    assert all(count == {"development": 2, "holdout": 12} for count in audit["coverage"].values())
    assert audit["independently_reviewed"] is audit["secret_holdout"] is audit["qualified"] is False
    transport = BenchmarkTransport(client_factory=forbidden, http_client=object())
    rows = []
    for case in suite.cases:
        for variant in ("generic", "family", "model_role"):
            row = observe_case(
                model=target(),
                case=case,
                variant=variant,
                trial=1,
                generate=transport.request,
                retain_response=True,
            )
            assert row.status == "completed" and row.score == 1, case.id
            assert row.backend == "deterministic" and row.billed_microunits == 0
            assert row.input_tokens is row.output_tokens is None
            rows.append(row)
    summary = summarise(rows)
    assert all(not row["eligible_for_advisory_routing"] for row in summary["rows"])
    assert all(
        row["decision"] == "insufficient_evidence" for row in summary["recommendations"].values()
    )


def test_changed_gold_changes_score_but_never_reference_output_or_prompt():
    case = RoleCase(
        "gold",
        "evidence",
        "Classify JSON syntax",
        "json_subset",
        {"valid": True},
        context={"atomic_kind": "parse_json", "reference_input": {"text": "{}"}},
    )
    wrong = replace(case, expected={"valid": False})
    assert prompt_for_case(target().model, case, "generic") == prompt_for_case(
        target().model, wrong, "generic"
    )
    transport = BenchmarkTransport(client_factory=forbidden, http_client=object())
    first = observe_case(
        model=target(),
        case=case,
        variant="generic",
        trial=1,
        generate=transport.request,
        retain_response=True,
    )
    second = observe_case(
        model=target(),
        case=wrong,
        variant="generic",
        trial=1,
        generate=transport.request,
        retain_response=True,
    )
    assert first.response == second.response
    assert (first.score, second.score) == (1, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"revision": "0" * 64},
        {"model": "arbitrary/python.py"},
        {"provider": "openai"},
        {"base_url": "http://127.0.0.1:1234/v1"},
        {"roles": ("coding",)},
    ],
)
def test_reference_target_refuses_stale_code_or_arbitrary_execution(change):
    with pytest.raises(InvalidInputError):
        replace(target(), **change)


@pytest.mark.parametrize(
    ("kind", "inputs"),
    [
        ("run_registered_tool", {"command": "echo forbidden"}),
        ("extract_field", {"document": {"x": 1}, "key": "x", "expected": 1}),
        ("extract_field", {"document": [], "key": "x"}),
        ("parse_json", {"text": 42}),
        ("check_exit_status", {"state": "running", "exit_code": 0}),
        ("check_exit_status", {"state": "completed", "exit_code": False}),
        ("check_exit_status", {"state": "completed", "exit_code": 256}),
        ("check_hash", {"text": "x", "sha256": "AB" * 32}),
        ("check_hash", {"text": "x", "sha256": "file:///private"}),
        ("parse_json", {"text": "x" * 16385}),
    ],
)
def test_reference_contracts_refuse_ambiguous_or_executable_inputs(kind, inputs):
    with pytest.raises(InvalidInputError):
        evaluate_reference(kind, inputs)


def test_unsupported_case_is_indeterminate_not_a_measured_failure():
    case = RoleCase(
        "unsupported",
        "status",
        "Read a job ledger",
        "json_subset",
        {"status": "ok"},
        context={"atomic_kind": "check_job_status", "reference_input": {}},
    )
    row = observe_case(
        model=target(), case=case, variant="generic", trial=1, generate=BenchmarkTransport().request
    )
    assert row.status == "indeterminate"
    assert row.score is row.elapsed_ms is row.billed_microunits is None


@pytest.mark.parametrize(
    "change",
    [
        {"suite_sha256": "0" * 64},
        {"exposure": "secret"},
        {"gold_review": "approved"},
        {"author": ""},
        {"extra": "field"},
    ],
)
def test_dataset_card_cannot_upgrade_public_gold_or_change_suite(change):
    suite, card = dataset()
    with pytest.raises(InvalidInputError):
        audit_atomic_dataset(suite, {**card, **change})


def test_dataset_audit_rejects_reworded_duplicate_inputs_across_splits():
    suite, _ = dataset()
    first = suite.cases[0]
    leaked = replace(first, id="leaked", split="holdout", task="Reworded prompt")
    changed = RoleSuite("leaked", (first, leaked))
    _, card = dataset()
    with pytest.raises(InvalidInputError, match="duplicate"):
        audit_atomic_dataset(changed, {**card, "suite_sha256": changed.identity})


def test_cli_reads_at_most_limit_plus_one_bytes(monkeypatch):
    amounts = []

    class Reader(io.BytesIO):
        def read(self, amount=-1):
            amounts.append(amount)
            return super().read(amount)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: Reader(b" " * 100))
    with pytest.raises(InvalidInputError, match="size limit"):
        cli._read(Path("input.json"), limit=10)
    assert amounts == [11]


def test_cli_runs_real_reference_matrix_and_retains_unqualified_audit(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    config = tmp_path / "targets.json"
    model = target()
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "id": model.id,
                        "model": model.model,
                        "backend": model.backend,
                        "roles": list(model.roles),
                        "revision": model.revision,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    shared = [
        "--suite",
        str(DATA / "atomic-reference-suite.json"),
        "--dataset-card",
        str(DATA / "atomic-reference-card.json"),
    ]
    assert cli.main(["preflight", "--models", str(config), *shared]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["matrix_attempts"] == plan["deterministic_attempts"] == 168
    assert plan["possible_hosted_calls"] == plan["possible_local_calls"] == 0
    assert (
        cli.main(
            [
                "run",
                "--models",
                str(config),
                *shared,
                "--output-dir",
                str(tmp_path / "runs"),
                "--store-text",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    report_dir = Path(result["report_dir"])
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "measured_reference_algorithms"
    assert report["hosted_attempted_calls"] == 0
    assert report["qualified"] is report["auto_promoted"] is False
    assert report["dataset_audit"]["independently_reviewed"] is False
    assert report["reference_runtime"]["source_sha256"] == model.revision
    assert report["reference_runtime"]["peak_ram_bytes"] is None
    assert all(row["score"] == 1 for row in report["results"])
    events = [json.loads(line) for line in (report_dir / "attempts.jsonl").read_text().splitlines()]
    assert len(events) == 336
    audit_path = tmp_path / "audit.json"
    assert cli.main(["audit-atomic-suite", *shared, "--output", str(audit_path)]) == 0
    assert json.loads(audit_path.read_text())["secret_holdout"] is False


def test_cli_exports_current_pin_and_refuses_overwrite(tmp_path, capsys):
    output = tmp_path / "reference-models.json"
    assert cli.main(["pin-reference", "--output", str(output)]) == 0
    model = cli._targets(output)[0]
    assert model.backend == "deterministic" and model.revision == reference_identity()
    assert model.provider is model.base_url is None
    before = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        cli.main(["pin-reference", "--output", str(output)])
    assert error.value.code == 2
    assert output.read_bytes() == before


def test_reference_refuses_source_drift_after_import(monkeypatch):
    from potbot.evaluation import agentic_reference

    path = Path(agentic_reference.__file__)
    original = Path.read_text

    def changed_source(current, *args, **kwargs):
        text = original(current, *args, **kwargs)
        return text + "\n# changed after import\n" if current == path else text

    monkeypatch.setattr(Path, "read_text", changed_source)
    with pytest.raises(InvalidInputError, match="changed after import"):
        reference_identity()
