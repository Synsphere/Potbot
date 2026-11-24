"""Freeze an atomic experiment and reconcile every predeclared primary cell.

Pure retained-evidence analysis; no inference, replay, resource attestation or
routing authority. A content hash proves consistency, not historical priority.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from potbot.controller.atomic_tasks import ATOMIC_KINDS
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_atomic_bench import validated_atomic_observations
from potbot.evaluation.agentic_dataset import atomic_input_identity
from potbot.evaluation.agentic_model_bench import ModelTarget, RoleSuite, prompt_for_case
from potbot.models.prompt_compiler import PromptVariant

_EVIDENCE = {
    "candidate_package",
    "gold_review",
    "dataset_card",
    "deployment_bindings",
    "review_bundle",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidInputError(message)


def read_experiment_document(path: Path) -> Any:
    """Read bounded retained metadata, including finite statistical fractions.

    Model response envelopes intentionally forbid floats and have a smaller node
    limit. Experiment reports are a separate nonexecuting file contract.
    """
    with path.open("rb") as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    _require(len(raw) <= 16 * 1024 * 1024, "Experiment document exceeds 16 MiB")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            _require(key not in result, "Duplicate experiment JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidInputError("Invalid experiment JSON") from None
    stack = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        _require(depth <= 32 and nodes <= 200000, "Experiment JSON exceeds structural bounds")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    canonical_json(value)  # Reject NaN, Infinity and exponent overflow anywhere.
    return value


def freeze_atomic_experiment(
    suite: RoleSuite,
    models: list[ModelTarget],
    *,
    variants: tuple[PromptVariant, ...],
    repeat: int,
    shuffle_seed: int,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Bind exact inputs, targets, prompt bytes, evidence and bounded cell order."""
    _require(
        type(repeat) is int
        and 1 <= repeat <= 5
        and type(shuffle_seed) is int
        and 0 <= shuffle_seed < 2**32
        and bool(variants)
        and len(set(variants)) == len(variants)
        and not set(variants) - {"generic", "family", "model_role"}
        and 1 <= len(models) <= 16
        and len({model.id for model in models}) == len(models)
        and type(suite.schema_version) is int
        and suite.schema_version == 1,
        "Invalid frozen experiment bounds",
    )
    _require(
        isinstance(evidence, dict) and set(evidence) == _EVIDENCE,
        "Experiment evidence must declare every supported binding",
    )
    kinds = {kind.name for kind in ATOMIC_KINDS}
    seen: set[str] = set()
    count = 0
    used: set[str] = set()
    for case in suite.cases:
        identity = atomic_input_identity(case)
        _require(
            (case.context or {}).get("atomic_kind") in kinds and identity not in seen,
            "Experiment needs known atomic kinds and distinct task inputs",
        )
        seen.add(identity)
        eligible = [model for model in models if case.role in model.roles]
        _require(bool(eligible), "Every frozen case needs an eligible target")
        used.update(model.id for model in eligible)
        count += len(eligible) * len(variants) * repeat
    _require(
        used == {model.id for model in models} and count <= 1600,
        "Experiment contains an unused target or exceeds 1600 cells",
    )
    cells = [
        {
            "case_id": case.id,
            "model_id": model.id,
            "variant": variant,
            "trial": trial,
            "input_sha256": case.input_digest,
            "prompt_sha256": prompt_for_case(model.model, case, variant).identity,
        }
        for case in suite.cases
        for model in models
        if case.role in model.roles
        for variant in variants
        for trial in range(1, repeat + 1)
    ]
    payload = {
        "schema": "potbot.atomic-experiment-plan/1",
        "suite_sha256": suite.identity,
        "targets_sha256": content_hash([asdict(model) for model in models]),
        "variants": list(variants),
        "repeat": repeat,
        "shuffle_seed": shuffle_seed,
        "evidence_sha256": {
            key: content_hash(evidence[key]) if evidence[key] is not None else None
            for key in sorted(_EVIDENCE)
        },
        "cells": cells,
    }
    return {**payload, "plan_sha256": content_hash(payload)}


def admit_atomic_experiment(
    document: object,
    suite: RoleSuite,
    models: list[ModelTarget],
    *,
    variants: tuple[PromptVariant, ...],
    repeat: int,
    shuffle_seed: int,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild instead of trusting a rehashed caller-provided cell list."""
    rebuilt = freeze_atomic_experiment(
        suite,
        models,
        variants=variants,
        repeat=repeat,
        shuffle_seed=shuffle_seed,
        evidence=evidence,
    )
    _require(
        isinstance(document, dict) and canonical_json(document) == canonical_json(rebuilt),
        "Experiment differs from its frozen inputs, bindings or execution matrix",
    )
    return rebuilt


def audit_atomic_experiment(
    plan: object,
    document: dict[str, Any],
    suite: RoleSuite,
    models: list[ModelTarget],
    *,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Regrade complete retained outputs; keep every missing/uncertain denominator."""
    _require(isinstance(plan, dict), "Invalid experiment plan")
    assert isinstance(plan, dict)
    _require(isinstance(plan.get("variants"), list), "Invalid experiment variants")
    rebuilt = admit_atomic_experiment(
        plan,
        suite,
        models,
        variants=tuple(plan["variants"]),
        repeat=plan.get("repeat", 0),
        shuffle_seed=plan.get("shuffle_seed", -1),
        evidence=evidence,
    )
    _require(
        isinstance(document, dict)
        and document.get("experiment_plan_sha256") == rebuilt["plan_sha256"]
        and document.get("model_targets_sha256") == rebuilt["targets_sha256"],
        "Report is not bound to this frozen experiment",
    )
    indexed = validated_atomic_observations(
        document,
        suite,
        models,
        variants=tuple(plan["variants"]),
        repeat=plan["repeat"],
    )
    groups: dict[tuple[str, str, str, str, str], list[Any]] = defaultdict(list)
    cases = {case.id: case for case in suite.cases}
    missing = uncertain = mismatched = failed = completed = 0
    known_cost = 0
    unknown_cost = 0
    for cell in rebuilt["cells"]:
        key = (cell["model_id"], cell["case_id"], cell["variant"], cell["trial"])
        row = indexed.get(key)
        case = cases[cell["case_id"]]
        groups[
            (
                (case.context or {})["atomic_kind"],
                case.role,
                case.split,
                cell["model_id"],
                cell["variant"],
            )
        ].append((case.id, row))
        if row is None:
            missing += 1
        elif row.status == "indeterminate":
            uncertain += 1
        elif row.status == "identity_mismatch":
            mismatched += 1
        else:
            completed += 1
            failed += int(row.score != 1)
        if row is None or row.billed_microunits is None:
            unknown_cost += 1
        else:
            known_cost += row.billed_microunits
    comparisons = []
    for (kind, role, split, model, variant), group in sorted(groups.items()):
        by_case: dict[str, list[Any]] = defaultdict(list)
        for case_id, row in group:
            by_case[case_id].append(row)
        complete = sum(
            all(row is not None and row.status == "completed" for row in rows)
            for rows in by_case.values()
        )
        passed = sum(
            all(row is not None and row.status == "completed" and row.score == 1 for row in rows)
            for rows in by_case.values()
        )
        comparisons.append(
            {
                "atomic_kind": kind,
                "role": role,
                "split": split,
                "model_id": model,
                "variant": variant,
                "planned_attempts": len(group),
                "distinct_cases": len(by_case),
                "complete_cases": complete,
                "all_repeats_passed_cases": passed,
                "verified_pass_fraction_of_planned_cases": passed / len(by_case),
            }
        )
    return {
        "schema": "potbot.atomic-experiment-audit/1",
        "plan_sha256": rebuilt["plan_sha256"],
        "report_sha256": content_hash(document),
        "planned_attempts": len(rebuilt["cells"]),
        "completed_attempts": completed,
        "failed_rubric_attempts": failed,
        "missing_attempts": missing,
        "indeterminate_attempts": uncertain,
        "identity_mismatch_attempts": mismatched,
        "observed_billed_subtotal_microunits": known_cost,
        "unknown_billing_attempts": unknown_cost,
        "total_billed_microunits": None if unknown_cost else known_cost,
        "complete_for_analysis": not (missing or uncertain or mismatched),
        "cohorts": comparisons,
        "uncovered_atomic_kinds": sorted(
            {kind.name for kind in ATOMIC_KINDS}
            - {(case.context or {})["atomic_kind"] for case in suite.cases}
        ),
        "prior_registration_authenticated": False,
        "qualified": False,
        "automatic_routing_changes": 0,
        "unknown_measurements": [
            "server_loaded_identity",
            "peak_ram",
            "peak_vram",
            "independent_gold_authentication",
            "holdout_isolation",
        ],
    }
