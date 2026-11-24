"""Blind review protocol fixtures do not authenticate gold or a protected holdout."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
from argparse import Namespace
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_candidate_dataset import (
    audit_candidate_review,
    candidate_package,
    gold_review_request,
    prepare_candidate_package,
)
from potbot.evaluation.agentic_model_bench import Generation, ModelTarget, RoleCase, RoleSuite
from potbot.evaluation.agentic_reference import evaluate_reference

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "candidate_dataset_cli_test", ROOT / "scripts/benchmark_agentic_models.py"
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def inputs():
    receipts = [("completed", 0), ("completed", 1), ("running", None), ("lost_reply", None)]
    labels = ["succeeded", "failed", "running", "indeterminate"]
    suite = RoleSuite(
        "private-candidate-protocol-fixture",
        tuple(
            RoleCase(
                f"case-{index}",
                "status",
                f"Classify supplied process receipt {index}",
                "json_subset",
                {"status": labels[index]},
                split="development" if index == 0 else "holdout",
                context={
                    "atomic_kind": "check_exit_status",
                    "reference_input": {"state": state, "exit_code": code},
                },
            )
            for index, (state, code) in enumerate(receipts)
        ),
    )
    provenance = {
        "author_id": "candidate-author",
        "licence": "synthetic unit fixture",
        "source": "No actual private dataset or independent human review",
        "exposure": "declared_private_candidate",
    }
    controls = {
        "schema": "potbot.atomic-oracle-controls/1",
        "cases": [
            {
                "case_id": case.id,
                "positive_response": canonical_json(case.expected),
                "negative_responses": ['{"status":"NEGATIVE_CONTROL_CANARY"}'],
            }
            for case in suite.cases
        ],
    }
    return suite, provenance, controls


def package():
    return prepare_candidate_package(*inputs())


def review(frozen):
    return {
        "schema": "potbot.atomic-gold-review/1",
        "input_manifest_sha256": gold_review_request(frozen)["input_manifest_sha256"],
        "reviewer_id": "separate-reviewer",
        "source": "Synthetic protocol fixture only",
        "answers": [
            {
                "case_id": case["id"],
                "verdict": "resolved",
                "expected": copy.deepcopy(case["expected"]),
                "rationale": "Fixture agreement, not an actual independent review",
            }
            for case in frozen["suite"]["cases"]
        ],
    }


def target():
    return ModelTarget(
        "fixture",
        "fixture/model",
        "loopback_openai",
        ("status",),
        "fixture-revision",
        base_url="http://127.0.0.1:12345/v1",
    )


def args(**extra):
    return Namespace(
        **{
            "repeat": 1,
            "review_bundle": None,
            "dataset_card": None,
            "confirm_prompt_disclosure": True,
            "judge_derived": False,
            "shuffle_seed": 42,
            "store_text": True,
            "min_samples": 12,
            "max_cost_microunits": 0,
            **extra,
        }
    )


def forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected transport, credential, model or provider access")


def test_blind_request_omits_proposed_gold_controls_splits_and_author():
    suite, provenance, controls = inputs()
    cases = (replace(suite.cases[0], expected={"status": "PROPOSED_GOLD_CANARY"}), *suite.cases[1:])
    suite = replace(suite, cases=cases)
    controls["cases"][0]["positive_response"] = canonical_json(cases[0].expected)
    frozen = prepare_candidate_package(suite, provenance, controls)
    request = gold_review_request(frozen)
    exported = canonical_json(request)
    for hidden in (
        "PROPOSED_GOLD_CANARY",
        "NEGATIVE_CONTROL_CANARY",
        "candidate-author",
        "expected",
        "positive_response",
        "negative_responses",
        "holdout",
        "development",
        "package_sha256",
    ):
        assert hidden not in exported
    assert len(request["cases"]) == 4
    assert request["cases"][0]["task"] == suite.cases[0].task
    assert request["input_manifest_sha256"] == content_hash(frozen["review_input"])


def test_frozen_package_detaches_mutable_inputs_and_canonicalizes_control_order():
    suite, provenance, controls = inputs()
    frozen = prepare_candidate_package(suite, provenance, controls)
    backwards = copy.deepcopy(controls)
    backwards["cases"].reverse()
    assert prepare_candidate_package(suite, provenance, backwards) == frozen
    provenance["author_id"] = "changed"
    controls["cases"][0]["positive_response"] = "changed"
    suite.cases[0].context["reference_input"]["state"] = "changed"
    assert candidate_package(frozen)[1] == frozen
    assert "changed" not in canonical_json(frozen)


@pytest.mark.parametrize(
    "mutation",
    [
        "positive",
        "negative",
        "duplicate-negative",
        "missing",
        "duplicate-case",
        "unknown-case",
        "structural",
        "empty-oracle",
    ],
)
def test_bad_or_vacuous_oracle_controls_are_refused(mutation):
    suite, provenance, controls = inputs()
    if mutation == "positive":
        controls["cases"][0]["positive_response"] = '{"status":"wrong"}'
    elif mutation == "negative":
        controls["cases"][0]["negative_responses"] = [controls["cases"][0]["positive_response"]]
    elif mutation == "duplicate-negative":
        controls["cases"][0]["negative_responses"] *= 2
    elif mutation == "missing":
        controls["cases"].pop()
    elif mutation == "duplicate-case":
        controls["cases"][1] = controls["cases"][0]
    elif mutation == "unknown-case":
        controls["cases"][0]["case_id"] = "unknown"
    elif mutation == "structural":
        suite = replace(
            suite, cases=(replace(suite.cases[0], grader="json_shape"), *suite.cases[1:])
        )
    else:
        suite = replace(
            suite,
            cases=(replace(suite.cases[0], grader="exact_text", expected=""), *suite.cases[1:]),
        )
    with pytest.raises(InvalidInputError):
        prepare_candidate_package(suite, provenance, controls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("exposure", "secret_holdout"),
        ("exposure", "public_synthetic_fixture"),
        ("author_id", ""),
        ("author_id", " ../author"),
        ("licence", ""),
        ("source", "x" * 513),
    ],
)
def test_provenance_is_bounded_and_cannot_declare_authentication(field, value):
    suite, provenance, controls = inputs()
    provenance[field] = value
    with pytest.raises(InvalidInputError):
        prepare_candidate_package(suite, provenance, controls)


def test_reworded_duplicate_reference_inputs_cannot_cross_the_split():
    suite, provenance, controls = inputs()
    suite = replace(
        suite,
        cases=(
            suite.cases[0],
            replace(suite.cases[0], id="alias", task="Reworded", split="holdout"),
        ),
    )
    controls["cases"] = [controls["cases"][0], {**controls["cases"][0], "case_id": "alias"}]
    with pytest.raises(InvalidInputError, match="Repeated"):
        prepare_candidate_package(suite, provenance, controls)


@pytest.mark.parametrize("mutation", ["gold", "input", "control", "extra", "boolean-schema"])
def test_package_is_fully_reconstructed_instead_of_trusting_its_hash(mutation):
    frozen = package()
    if mutation == "gold":
        frozen["suite"]["cases"][0]["expected"] = {"status": "wrong"}
    elif mutation == "input":
        frozen["review_input"]["cases"][0]["expected"] = {"status": "leaked"}
    elif mutation == "control":
        frozen["controls"]["cases"][0]["negative_responses"] = ['{"status":"other_wrong"}']
    elif mutation == "extra":
        frozen["trusted"] = True
    else:
        frozen["suite"]["schema_version"] = True
    with pytest.raises(InvalidInputError):
        candidate_package(frozen)


def test_complete_declared_agreement_never_authenticates_review_or_secrecy():
    frozen = package()
    audit = audit_candidate_review(frozen, review(frozen))
    assert audit["status"] == "declared_agreement"
    assert (
        audit["missing_case_ids"]
        == audit["uncertain_case_ids"]
        == audit["conflicting_case_ids"]
        == []
    )
    for field in (
        "qualified",
        "independently_reviewed",
        "reviewer_identity_authenticated",
        "secret_holdout",
    ):
        assert audit[field] is False
    assert audit["automatic_routing_changes"] == 0
    assert audit["suite_sha256"] == candidate_package(frozen)[0].identity
    assert "rationale" not in canonical_json(audit)


def test_missing_uncertain_and_conflicting_answers_remain_distinct():
    frozen = package()
    assert audit_candidate_review(frozen)["missing_case_ids"] == [f"case-{i}" for i in range(4)]
    document = review(frozen)
    document["answers"].pop()
    document["answers"][0].update(verdict="uncertain", expected=None)
    document["answers"][1]["expected"] = {"status": "CONFLICTING_GOLD_CANARY"}
    audit = audit_candidate_review(frozen, document)
    assert audit["status"] == "conflicting_gold"
    assert audit["missing_case_ids"] == ["case-3"]
    assert audit["uncertain_case_ids"] == ["case-0"]
    assert audit["conflicting_case_ids"] == ["case-1"]
    assert "CONFLICTING_GOLD_CANARY" not in canonical_json(audit)


@pytest.mark.parametrize(
    "mutation",
    [
        "self",
        "self-case",
        "stale-inputs",
        "duplicate",
        "unknown",
        "rationale",
        "uncertain-with-gold",
        "wrong-type",
    ],
)
def test_invalid_review_cannot_supply_agreement(mutation):
    frozen = package()
    document = review(frozen)
    if mutation in {"self", "self-case"}:
        document["reviewer_id"] = "candidate-author" if mutation == "self" else "CANDIDATE-AUTHOR"
    elif mutation == "stale-inputs":
        document["input_manifest_sha256"] = "0" * 64
    elif mutation == "duplicate":
        document["answers"][1] = document["answers"][0]
    elif mutation == "unknown":
        document["answers"][0]["case_id"] = "unknown"
    elif mutation == "rationale":
        document["answers"][0]["rationale"] = ""
    elif mutation == "uncertain-with-gold":
        document["answers"][0]["verdict"] = "uncertain"
    else:
        document["answers"][0]["expected"] = "wrong type"
    with pytest.raises(InvalidInputError):
        audit_candidate_review(frozen, document)


def cli_files(tmp_path):
    suite, provenance, controls = inputs()
    for name, value in [
        (
            "suite",
            {"schema_version": 1, "id": suite.id, "cases": [asdict(case) for case in suite.cases]},
        ),
        ("provenance", provenance),
        ("controls", controls),
        ("models", {"schema_version": 1, "models": [asdict(target())]}),
    ]:
        (tmp_path / (name + ".json")).write_text(canonical_json(value), encoding="utf-8")
    return suite


def test_cli_prepare_and_review_are_private_offline_and_never_overwrite(
    tmp_path, monkeypatch, capsys
):
    cli_files(tmp_path)
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    destination = tmp_path / "frozen"
    command = [
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
    assert cli.main(command) == 0
    assert "NEGATIVE_CONTROL_CANARY" not in capsys.readouterr().out
    full = destination / "candidate-package.json"
    request = destination / "review-request.json"
    assert json.loads(request.read_text())["input_manifest_sha256"]
    if os.name == "posix":
        assert stat.S_IMODE(full.stat().st_mode) == stat.S_IMODE(request.stat().st_mode) == 0o600
    with pytest.raises(SystemExit) as error:
        cli.main(command)
    assert error.value.code == 2
    audit_path = tmp_path / "pending.json"
    assert (
        cli.main(
            [
                "audit-atomic-gold-review",
                "--candidate-package",
                str(full),
                "--output",
                str(audit_path),
            ]
        )
        == 2
    )
    assert json.loads(audit_path.read_text())["status"] == "pending_review"
    frozen = json.loads(full.read_text())
    (tmp_path / "review.json").write_text(canonical_json(review(frozen)))
    audit_path = tmp_path / "agreed.json"
    assert (
        cli.main(
            [
                "audit-atomic-gold-review",
                "--candidate-package",
                str(full),
                "--gold-review",
                str(tmp_path / "review.json"),
                "--output",
                str(audit_path),
            ]
        )
        == 0
    )
    assert json.loads(audit_path.read_text())["independently_reviewed"] is False


def test_incomplete_review_blocks_before_transport_or_vault_construction(tmp_path, monkeypatch):
    frozen = package()
    full = tmp_path / "candidate.json"
    full.write_text(canonical_json(frozen))
    monkeypatch.setattr(cli, "BenchmarkTransport", forbidden)
    monkeypatch.setattr(cli, "ServiceCredentials", forbidden)
    suite = candidate_package(frozen)[0]
    config = args(candidate_package=full)
    assert cli._candidate_admission(config, suite)["status"] == "pending_review"
    with pytest.raises(InvalidInputError, match="incomplete or conflicting"):
        cli._execute(config, suite, [target()], ["generic"], None)
    with pytest.raises(InvalidInputError, match="requires its exact"):
        cli._candidate_admission(args(gold_review=full), suite)
    with pytest.raises(InvalidInputError, match="separate inputs"):
        cli._candidate_admission(args(candidate_package=full, dataset_card=full), suite)
    changed = replace(suite, cases=(replace(suite.cases[0], task="Changed"), *suite.cases[1:]))
    with pytest.raises(InvalidInputError, match="differs"):
        cli._candidate_admission(config, changed)


def test_run_uses_existing_packets_without_proposed_gold_controls_or_reviewer_text(
    tmp_path, monkeypatch
):
    frozen = package()
    document = review(frozen)
    document["answers"][0]["rationale"] = "REVIEWER_RATIONALE_CANARY"
    full, reviewed = tmp_path / "candidate.json", tmp_path / "review.json"
    full.write_text(canonical_json(frozen))
    reviewed.write_text(canonical_json(document))
    transmitted = []

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def request(self, model, prompt):
            transmitted.append(prompt.system + prompt.user)
            packet = json.loads(prompt.user.split("\n", 1)[1])
            output = evaluate_reference("check_exit_status", packet["context"]["reference_input"])
            return Generation(
                canonical_json(output), 2, returned_model=model.model, origin="synthetic_transport"
            )

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    report = cli._execute(
        args(candidate_package=full, gold_review=reviewed),
        candidate_package(frozen)[0],
        [target()],
        ["generic"],
        None,
    )
    assert len(transmitted) == 4
    for hidden in (
        "NEGATIVE_CONTROL_CANARY",
        "REVIEWER_RATIONALE_CANARY",
        "separate-reviewer",
        "candidate-author",
        '"expected"',
    ):
        assert all(hidden not in packet for packet in transmitted)
    assert all(row["score"] == 1 for row in report["results"])
    assert report["candidate_dataset_review"]["package_sha256"] == frozen["package_sha256"]
    assert report["candidate_dataset_review"]["status"] == "declared_agreement"
    assert report["candidate_dataset_review"]["qualified"] is False
    assert report["qualified"] is report["auto_promoted"] is False
