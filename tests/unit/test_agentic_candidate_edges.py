"""Strict metadata and CLI integration edges for declared candidate review."""

from __future__ import annotations

import importlib.util
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, SchemaViolationError
from potbot.domain.serialization import canonical_json
from potbot.evaluation.agentic_candidate_dataset import (
    audit_candidate_review,
    prepare_candidate_package,
)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "candidate_edge_helpers", ROOT / "tests/unit/test_agentic_candidate_dataset.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
cli = helpers.cli


def test_boolean_suite_schema_cannot_be_normalized_to_version_one():
    suite, provenance, controls = helpers.inputs()
    with pytest.raises(InvalidInputError, match="schema must be exact"):
        prepare_candidate_package(replace(suite, schema_version=True), provenance, controls)


def test_bool_and_integer_gold_are_a_conflict_not_python_equality():
    suite, provenance, controls = helpers.inputs()
    suite = replace(suite, cases=(replace(suite.cases[0], expected={"count": 1}), *suite.cases[1:]))
    controls["cases"][0].update(positive_response='{"count":1}', negative_responses=['{"count":0}'])
    package = prepare_candidate_package(suite, provenance, controls)
    review = helpers.review(package)
    review["answers"][0]["expected"] = {"count": True}
    result = audit_candidate_review(package, review)
    assert result["status"] == "conflicting_gold"
    assert result["conflicting_case_ids"] == ["case-0"]


def test_coverage_exposes_all_uncovered_atomic_kinds():
    report = audit_candidate_review(helpers.package())
    assert report["coverage"] == {"check_exit_status": 4}
    assert len(report["uncovered_atomic_kinds"]) == 40
    assert "check_exit_status" not in report["uncovered_atomic_kinds"]


def test_strict_candidate_reader_stops_at_limit_plus_one(monkeypatch):
    amounts = []

    class Reader(io.BytesIO):
        def read(self, amount=-1):
            amounts.append(amount)
            return super().read(amount)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: Reader(b" " * 100))
    with pytest.raises(SchemaViolationError):
        cli._strict_read(Path("candidate.json"), limit=10)
    assert amounts == [11]


def test_duplicate_metadata_keys_have_a_sanitized_error_without_export(
    tmp_path, monkeypatch, capsys
):
    helpers.cli_files(tmp_path)
    (tmp_path / "provenance.json").write_text(
        '{"author_id":"PRIVATE_CANARY","author_id":"duplicate"}'
    )
    destination = tmp_path / "should-not-exist"
    monkeypatch.setattr(cli, "BenchmarkTransport", helpers.forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "prepare-atomic-dataset",
                "--suite",
                str(tmp_path / "suite.json"),
                "--provenance",
                str(tmp_path / "provenance.json"),
                "--controls",
                str(tmp_path / "controls.json"),
                "--output-dir",
                str(destination),
            ]
        )
    assert error.value.code == 2
    assert "PRIVATE_CANARY" not in capsys.readouterr().err
    assert not destination.exists()


def test_preflight_exports_review_hold_then_agreement_without_constructing_transport(
    tmp_path, monkeypatch, capsys
):
    helpers.cli_files(tmp_path)
    frozen = helpers.package()
    full, reviewed = tmp_path / "candidate.json", tmp_path / "review.json"
    full.write_text(canonical_json(frozen))
    reviewed.write_text(canonical_json(helpers.review(frozen)))
    monkeypatch.setattr(cli, "BenchmarkTransport", helpers.forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", helpers.forbidden)
    command = [
        "preflight",
        "--suite",
        str(tmp_path / "suite.json"),
        "--models",
        str(tmp_path / "models.json"),
        "--candidate-package",
        str(full),
    ]
    assert cli.main(command) == 2
    plan = json.loads(capsys.readouterr().out)
    assert plan["candidate_dataset_review"]["status"] == "pending_review"
    assert plan["network_started"] is False
    assert cli.main([*command, "--gold-review", str(reviewed)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["candidate_dataset_review"]["status"] == "declared_agreement"
    assert plan["candidate_dataset_review"]["independently_reviewed"] is False


def test_package_bound_includes_the_hash_envelope_and_export_newline(monkeypatch):
    from potbot.evaluation import agentic_candidate_dataset as candidates

    frozen = helpers.package()
    size = len(canonical_json(frozen).encode())
    monkeypatch.setattr(candidates, "_MAX_PACKAGE_BYTES", size)
    with pytest.raises(InvalidInputError, match="exceeds"):
        helpers.package()
    monkeypatch.setattr(candidates, "_MAX_PACKAGE_BYTES", size + 1)
    assert helpers.package() == frozen
