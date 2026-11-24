"""Frozen blind reviewer tasks and retained-evidence A/B calibration.

This reuses existing rubric and observation owners. A consistent bundle cannot
authenticate its author, gold labels, provider model or deployed runtime.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from typing import Any

from potbot.controller.atomic_tasks import ATOMIC_KINDS
from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_atomic_bench import _metrics, validated_atomic_observations
from potbot.evaluation.agentic_model_bench import (
    ModelTarget,
    Observation,
    RoleCase,
    RoleSuite,
    _wilson_lower,
    judge_case,
    vars_case,
)
from potbot.models.envelopes import strict_json
from potbot.models.prompt_compiler import PromptVariant

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UNKNOWN = {"unknown", "unqualified", "latest"}
_SCHEMA = "potbot.atomic-review-suite/1"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidInputError(message)


def model_revision_identity(model: ModelTarget) -> tuple[str, str]:
    # Moving the same declared weights to another backend does not create a peer.
    return model.model, model.revision


def _suite_for_pairs(pairs: list[dict[str, Any]]) -> RoleSuite:
    cases: list[RoleCase] = []
    for pair in pairs:
        for reverse in (False, True):
            case = judge_case(
                "review-" + pair["pair_id"],
                pair["task"],
                pair["first_output"],
                pair["second_output"],
                reference_winner=pair["reference_winner"],
                reverse=reverse,
            )
            cases.append(
                replace(
                    case,
                    context={
                        "atomic_kind": "compare_two_answers",
                        "review_pair_sha256": pair["pair_id"],
                        "order": "reverse" if reverse else "forward",
                    },
                )
            )
    return RoleSuite("atomic-review-" + content_hash(pairs)[:32], tuple(cases))


def _pair_identity(pair: dict[str, Any]) -> str:
    return content_hash(
        {
            name: pair[name]
            for name in (
                "source_input_sha256",
                "task",
                "first_output",
                "second_output",
            )
        }
    )


def build_reviewer_bundle(
    document: dict[str, Any],
    suite: RoleSuite,
    models: list[ModelTarget],
    *,
    first: str,
    second: str,
    variants: tuple[PromptVariant, ...] = ("generic", "family", "model_role"),
    repeat: int = 1,
    source_variant: PromptVariant = "generic",
    source_trial: int = 1,
) -> dict[str, Any]:
    """Select one predeclared source cell per task; never pick its best attempt."""
    indexed = validated_atomic_observations(
        document, suite, models, variants=variants, repeat=repeat
    )
    targets = {model.id: model for model in models}
    _require(
        first != second and first in targets and second in targets, "Select two source targets"
    )
    _require(
        model_revision_identity(targets[first]) != model_revision_identity(targets[second]),
        "Source aliases are not independent targets",
    )
    _require(
        source_variant in variants and type(source_trial) is int and 1 <= source_trial <= repeat,
        "Source cell must be declared before reviewer task construction",
    )
    pairs: list[dict[str, Any]] = []
    excluded = []
    for case in suite.cases:
        rows = [
            indexed.get((mid, case.id, source_variant, source_trial)) for mid in (first, second)
        ]
        reason = None
        if case.split != "holdout":
            reason = "development_case"
        elif case.grader == "json_shape":
            reason = "structural_only_reference"
        elif any(row is None for row in rows):
            reason = "missing_source_attempt"
        elif any(row is not None and row.status != "completed" for row in rows):
            reason = "indeterminate_source_attempt"
        elif {row.score for row in rows if row is not None} != {0, 1}:
            reason = "no_objective_pass_fail_pair"
        if reason is not None:
            excluded.append({"case_id": case.id, "reason": reason})
            continue
        a, b = rows
        assert a is not None and b is not None and a.response is not None and b.response is not None
        task = canonical_json({"role": case.role, "task": case.task, "context": case.context})
        if len((task + a.response + b.response).encode()) > 12000:
            excluded.append({"case_id": case.id, "reason": "bounded_reviewer_packet_exceeded"})
            continue
        pair = {
            "source_case_id": case.id,
            "source_input_sha256": case.input_digest,
            "source_atomic_kind": (case.context or {})["atomic_kind"],
            "task": task,
            "first_output": a.response,
            "second_output": b.response,
            "reference_winner": "A" if a.score == 1 else "B",
        }
        pair["pair_id"] = _pair_identity(pair)
        pairs.append(pair)
    _require(
        1 <= len(pairs) <= 100,
        "Reviewer suite requires one to 100 eligible source tasks; no truncation",
    )
    frozen = _suite_for_pairs(pairs)
    payload = {
        "schema": _SCHEMA,
        "source_suite_sha256": suite.identity,
        "source_report_sha256": content_hash(document),
        "source_targets_sha256": content_hash([asdict(model) for model in models]),
        "source_variant": source_variant,
        "source_trial": source_trial,
        "subjects": [
            {**asdict(targets[mid]), "roles": list(targets[mid].roles)} for mid in (first, second)
        ],
        "pairs": pairs,
        "excluded": excluded,
        "suite": {
            "schema_version": 1,
            "id": frozen.id,
            "cases": [vars_case(c) for c in frozen.cases],
        },
        "suite_sha256": frozen.identity,
        "gold_review_authenticated": False,
        "qualified": False,
        "automatic_routing_changes": 0,
    }
    return {**payload, "bundle_sha256": content_hash(payload)}


def reviewer_suite(bundle: object) -> tuple[RoleSuite, list[dict[str, Any]], list[ModelTarget]]:
    """Reconstruct every orientation from exact retained pair bytes and references."""
    fields = {
        "schema",
        "source_suite_sha256",
        "source_report_sha256",
        "source_targets_sha256",
        "source_variant",
        "source_trial",
        "subjects",
        "pairs",
        "excluded",
        "suite",
        "suite_sha256",
        "gold_review_authenticated",
        "qualified",
        "automatic_routing_changes",
        "bundle_sha256",
    }
    _require(
        isinstance(bundle, dict) and set(bundle) == fields, "Invalid frozen reviewer bundle fields"
    )
    assert isinstance(bundle, dict)
    _require(
        bundle["schema"] == _SCHEMA
        and bundle["qualified"] is False
        and bundle["gold_review_authenticated"] is False
        and type(bundle["automatic_routing_changes"]) is int
        and bundle["automatic_routing_changes"] == 0,
        "Reviewer bundle cannot declare qualification or authenticated gold",
    )
    _require(
        content_hash({k: v for k, v in bundle.items() if k != "bundle_sha256"})
        == bundle["bundle_sha256"],
        "Frozen reviewer bundle digest differs",
    )
    for name in (
        "source_suite_sha256",
        "source_report_sha256",
        "source_targets_sha256",
        "suite_sha256",
    ):
        _require(
            isinstance(bundle[name], str) and _DIGEST.fullmatch(bundle[name]) is not None,
            "Reviewer provenance needs exact SHA-256 identities",
        )
    _require(
        bundle["source_variant"] in {"generic", "family", "model_role"}
        and type(bundle["source_trial"]) is int
        and 1 <= bundle["source_trial"] <= 5,
        "Invalid reviewer source cell",
    )
    _require(
        isinstance(bundle["subjects"], list) and len(bundle["subjects"]) == 2,
        "Reviewer suite needs two source subjects",
    )
    subjects = [ModelTarget.from_dict(item) for item in bundle["subjects"]]
    _require(
        subjects[0].id != subjects[1].id
        and model_revision_identity(subjects[0]) != model_revision_identity(subjects[1]),
        "Source subject aliases cannot supply independent answers",
    )
    _require(
        isinstance(bundle["pairs"], list) and 1 <= len(bundle["pairs"]) <= 100,
        "Reviewer pairs must be bounded",
    )
    _require(
        isinstance(bundle["excluded"], list) and len(bundle["excluded"]) <= 200,
        "Invalid excluded source inventory",
    )
    seen_inputs: set[str] = set()
    seen_cases: set[str] = set()
    pair_fields = {
        "pair_id",
        "source_case_id",
        "source_input_sha256",
        "source_atomic_kind",
        "task",
        "first_output",
        "second_output",
        "reference_winner",
    }
    for pair in bundle["pairs"]:
        _require(
            isinstance(pair, dict)
            and set(pair) == pair_fields
            and all(isinstance(value, str) for value in pair.values()),
            "Invalid reviewer pair fields",
        )
        _require(
            pair["pair_id"] == _pair_identity(pair)
            and _DIGEST.fullmatch(pair["source_input_sha256"]) is not None,
            "Reviewer pair identity differs",
        )
        _require(
            pair["reference_winner"] in {"A", "B"}
            and pair["source_atomic_kind"] in {kind.name for kind in ATOMIC_KINDS}
            and 1 <= len(pair["source_case_id"]) <= 128
            and len((pair["task"] + pair["first_output"] + pair["second_output"]).encode())
            <= 12000,
            "Invalid reviewer source task",
        )
        _require(
            pair["source_input_sha256"] not in seen_inputs
            and pair["source_case_id"] not in seen_cases,
            "Repeated source tasks cannot inflate reviewer sample size",
        )
        try:
            packet = json.loads(pair["task"])
        except ValueError:
            raise InvalidInputError("Invalid retained source packet") from None
        _require(
            isinstance(packet, dict)
            and set(packet) == {"role", "task", "context"}
            and canonical_json(packet) == pair["task"]
            and content_hash(packet) == pair["source_input_sha256"]
            and all(packet["role"] in model.roles for model in subjects)
            and isinstance(packet["context"], dict)
            and packet["context"].get("atomic_kind") == pair["source_atomic_kind"],
            "Retained source packet differs from its input identity",
        )
        seen_inputs.add(pair["source_input_sha256"])
        seen_cases.add(pair["source_case_id"])
    reasons = {
        "development_case",
        "structural_only_reference",
        "missing_source_attempt",
        "indeterminate_source_attempt",
        "no_objective_pass_fail_pair",
        "bounded_reviewer_packet_exceeded",
    }
    for item in bundle["excluded"]:
        _require(
            isinstance(item, dict)
            and set(item) == {"case_id", "reason"}
            and isinstance(item["case_id"], str)
            and 1 <= len(item["case_id"]) <= 128
            and isinstance(item["reason"], str)
            and item["reason"] in reasons
            and item["case_id"] not in seen_cases,
            "Invalid excluded source disposition",
        )
        seen_cases.add(item["case_id"])
    _require(len(seen_cases) <= 200, "Source inventory exceeds its suite bound")
    suite = _suite_for_pairs(bundle["pairs"])
    _require(
        suite.identity == bundle["suite_sha256"] and RoleSuite.from_dict(bundle["suite"]) == suite,
        "Reviewer orientations or hidden references differ from retained pairs",
    )
    return suite, bundle["pairs"], subjects


def _choice(row: Observation) -> str | None:
    try:
        parsed = strict_json(row.response or "", max_output_bytes=65536)
        if (
            isinstance(parsed, dict)
            and set(parsed) == {"choice"}
            and parsed["choice"] in {"A", "B"}
        ):
            return str(parsed["choice"])
    except (DomainError, TypeError):
        pass
    return None


def admit_reviewer_models(models: list[ModelTarget], subjects: list[ModelTarget]) -> None:
    """Refuse declared self-review before the existing runner can dispatch a call."""
    _require(
        1 <= len(models) <= 16 and all(model.roles == ("judge",) for model in models),
        "Calibration targets must select only the judge role",
    )
    identities = [model_revision_identity(model) for model in models]
    _require(
        len(set(identities)) == len(identities),
        "Reviewer aliases cannot pose as independent models",
    )
    _require(
        not set(identities) & {model_revision_identity(model) for model in subjects},
        "A model alias cannot judge its own source answer",
    )


def calibrate_reviewers(
    bundle: dict[str, Any],
    document: dict[str, Any],
    models: list[ModelTarget],
    *,
    variants: tuple[PromptVariant, ...] = ("generic", "family", "model_role"),
    repeat: int = 1,
    minimum_pairs: int = 12,
) -> dict[str, Any]:
    """Measure conditional peer-review quality; every source task counts once."""
    _require(
        type(minimum_pairs) is int and 4 <= minimum_pairs <= 100,
        "Invalid reviewer evidence minimum",
    )
    suite, pairs, subjects = reviewer_suite(bundle)
    admit_reviewer_models(models, subjects)
    _require(
        isinstance(document, dict)
        and document.get("review_bundle_sha256") == bundle["bundle_sha256"],
        "Reviewer run did not retain this frozen bundle identity",
    )
    indexed = validated_atomic_observations(
        document, suite, models, variants=variants, repeat=repeat
    )
    reports = []
    for model in models:
        for variant in variants:
            complete_pairs = correct_pairs = inconsistent_pairs = 0
            valid_verdicts = wrong_approvals = a_choices = 0
            missing = uncertain = malformed = 0
            measured: list[Observation] = []
            for pair in pairs:
                decisions: list[tuple[str, str]] = []
                for reverse in (False, True):
                    side = "reverse" if reverse else "forward"
                    case_id = "review-" + pair["pair_id"] + "-" + side
                    winner = pair["reference_winner"]
                    if reverse:
                        winner = "B" if winner == "A" else "A"
                    for trial in range(1, repeat + 1):
                        row = indexed.get((model.id, case_id, variant, trial))
                        if row is None:
                            missing += 1
                            continue
                        if row.status != "completed":
                            uncertain += 1
                            continue
                        measured.append(row)
                        choice = _choice(row)
                        if choice is None:
                            malformed += 1
                            continue
                        valid_verdicts += 1
                        wrong_approvals += choice != winner
                        a_choices += choice == "A"
                        underlying = ("B" if choice == "A" else "A") if reverse else choice
                        decisions.append((underlying, pair["reference_winner"]))
                if len(decisions) == 2 * repeat:
                    complete_pairs += 1
                    correct_pairs += all(actual == expected for actual, expected in decisions)
                    inconsistent_pairs += len({actual for actual, _ in decisions}) != 1
            exact_revisions = all(m.revision.casefold() not in _UNKNOWN for m in [model, *subjects])
            reports.append(
                {
                    "model_id": model.id,
                    "revision": model.revision,
                    "backend": model.backend,
                    "variant": variant,
                    "declared_source_tasks": len(pairs),
                    "complete_valid_source_tasks": complete_pairs,
                    "symmetric_correct_source_tasks": correct_pairs,
                    "symmetric_reference_accuracy": correct_pairs / complete_pairs
                    if complete_pairs
                    else None,
                    "wilson_lower_95": _wilson_lower(correct_pairs, complete_pairs)
                    if complete_pairs
                    else None,
                    "position_inconsistent_source_tasks": inconsistent_pairs,
                    "observed_valid_verdicts": valid_verdicts,
                    "observed_wrong_answer_approvals": wrong_approvals,
                    "wrong_answer_approval_rate_observed": wrong_approvals / valid_verdicts
                    if valid_verdicts
                    else None,
                    "first_position_choice_rate_observed": a_choices / valid_verdicts
                    if valid_verdicts
                    else None,
                    "missing_attempts": missing,
                    "indeterminate_attempts": uncertain,
                    "malformed_verdicts": malformed,
                    "comparison_ready": complete_pairs >= minimum_pairs
                    and not (missing or uncertain or malformed)
                    and exact_revisions,
                    "measurements": _metrics(measured),
                    "qualified": False,
                }
            )
    return {
        "schema": "potbot.atomic-reviewer-calibration/1",
        "bundle_sha256": bundle["bundle_sha256"],
        "suite_sha256": suite.identity,
        "reviewer_report_sha256": content_hash(document),
        "reviewer_targets_sha256": content_hash([asdict(model) for model in models]),
        "minimum_distinct_source_tasks": minimum_pairs,
        "repeat": repeat,
        "reviewers": reports,
        "excluded_source_tasks": bundle["excluded"],
        "gold_review_authenticated": False,
        "provider_identity_authenticated": False,
        "deployment_qualified": False,
        "qualified": False,
        "automatic_routing_changes": 0,
        "scope": "Retained reference consistency and observed verdicts; source gold, deployment and independence of task samples require separate evidence",
    }
