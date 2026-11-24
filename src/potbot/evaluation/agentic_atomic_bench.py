"""Offline, paired atomic-role analysis of the existing benchmark's receipts.

This verifies internal evidence consistency, not provider identity or deployment
qualification. It performs no inference, dispatch, permission or routing changes.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections import defaultdict
from typing import Any

from potbot.controller.atomic_tasks import ATOMIC_KINDS
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.evaluation.agentic_model_bench import (
    ModelTarget,
    Observation,
    RoleSuite,
    prompt_for_case,
    returned_model_identity,
    rubric_score,
)
from potbot.models.prompt_compiler import PromptVariant


def _reject(condition: bool, message: str) -> None:
    if condition:
        raise InvalidInputError(message)


def _metrics(rows: list[Observation]) -> dict[str, Any]:
    times = sorted(row.elapsed_ms for row in rows if row.elapsed_ms is not None)
    result: dict[str, Any] = {
        "completed_attempts": len(rows),
        "median_latency_ms": statistics.median(times) if times else None,
        "p95_latency_ms": times[math.ceil(len(times) * 0.95) - 1] if times else None,
    }
    for name in ("input_tokens", "output_tokens", "billed_microunits"):
        values = [getattr(row, name) for row in rows]
        result[name] = sum(values) if values and all(v is not None for v in values) else None
    return result


def validated_atomic_observations(
    document: dict[str, Any],
    suite: RoleSuite,
    models: list[ModelTarget],
    *,
    variants: tuple[PromptVariant, ...],
    repeat: int,
) -> dict[tuple[str, str, str, int], Observation]:
    """Recheck bounded retained bytes; this does not authenticate provider claims."""
    _reject(
        type(repeat) is not int
        or not 1 <= repeat <= 5
        or not variants
        or len(set(variants)) != len(variants)
        or bool(set(variants) - {"generic", "family", "model_role"}),
        "Invalid observation matrix bounds",
    )
    targets = {model.id: model for model in models}
    _reject(
        not 1 <= len(targets) <= 16 or len(targets) != len(models),
        "Observation targets must be distinct and bounded",
    )
    _reject(
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("suite_sha256") != suite.identity
        or document.get("qualified") is not False
        or document.get("auto_promoted") is not False
        or not isinstance(document.get("results"), list)
        or len(document["results"]) > 1600,
        "Atomic comparison requires bounded unqualified benchmark evidence",
    )
    if "model_targets_sha256" in document:
        _reject(
            document["model_targets_sha256"]
            != content_hash(
                [
                    {name: getattr(model, name) for name in model.__dataclass_fields__}
                    for model in models
                ]
            ),
            "Benchmark model configuration identity differs",
        )
    kinds = {kind.name for kind in ATOMIC_KINDS}
    cases = {case.id: case for case in suite.cases}
    input_splits: dict[str, str] = {}
    for case in suite.cases:
        kind = (case.context or {}).get("atomic_kind")
        _reject(kind not in kinds, "Every case needs an existing atomic kind")
        _reject(case.input_digest in input_splits, "Repeated task inputs cannot inflate holdouts")
        input_splits[case.input_digest] = case.split

    indexed: dict[tuple[str, str, str, int], Observation] = {}
    for raw in document["results"]:
        _reject(not isinstance(raw, dict), "Invalid benchmark observation")
        try:
            row = Observation(**raw)
        except TypeError:
            raise InvalidInputError("Invalid benchmark observation fields") from None
        _reject(
            any(
                not isinstance(getattr(row, field), str)
                for field in (
                    "model_id",
                    "model",
                    "revision",
                    "role",
                    "case_id",
                    "split",
                    "variant",
                    "input_sha256",
                    "prompt_sha256",
                    "status",
                    "backend",
                    "tier",
                    "grader",
                )
            ),
            "Observation identities must be text",
        )
        model = targets.get(row.model_id)
        _reject(row.case_id not in cases, "Unknown observation case")
        case = cases[row.case_id]
        _reject(model is None or case is None, "Unknown observation target or case")
        assert model is not None and case is not None
        _reject(
            row.model != model.model
            or row.revision != model.revision
            or row.backend != model.backend
            or row.tier != model.tier
            or row.role != case.role
            or row.role not in model.roles
            or row.split != case.split
            or row.grader != case.grader
            or row.input_sha256 != case.input_digest
            or row.variant not in variants
            or type(row.trial) is not int
            or not 1 <= row.trial <= repeat,
            "Observation differs from its declared model, input or matrix",
        )
        variant: PromptVariant = row.variant  # type: ignore[assignment]
        _reject(
            row.prompt_sha256 != prompt_for_case(model.model, case, variant).identity,
            "Observation prompt identity differs",
        )
        key = (row.model_id, row.case_id, row.variant, row.trial)
        _reject(key in indexed, "Duplicate attempts cannot inflate benchmark evidence")
        _reject(
            row.status not in {"completed", "indeterminate", "identity_mismatch"},
            "Unknown observation status",
        )
        identity = returned_model_identity(row.model, row.returned_model)
        _reject(row.model_identity != identity, "Returned model identity classification differs")
        _reject(
            not isinstance(row.generation_origin, str) or not 1 <= len(row.generation_origin) <= 80,
            "Invalid generation origin",
        )
        _reject(
            (row.status == "identity_mismatch") != (identity == "mismatch"),
            "Mismatched output cannot acquire a completed attribution",
        )
        for value in (row.elapsed_ms, row.input_tokens, row.output_tokens, row.billed_microunits):
            _reject(value is not None and (type(value) is not int or value < 0), "Invalid metric")
        if row.status in {"completed", "identity_mismatch"}:
            _reject(
                not isinstance(row.response, str)
                or len(row.response.encode("utf-8")) > 65536
                or row.elapsed_ms is None,
                "Completed comparisons require retained bounded output and timing",
            )
            assert row.response is not None
            _reject(
                hashlib.sha256(row.response.encode("utf-8")).hexdigest() != row.output_sha256
                or (
                    row.status == "completed"
                    and (
                        type(row.score) not in {int, float}
                        or row.score != rubric_score(case, row.response)
                    )
                )
                or (row.status == "identity_mismatch" and row.score is not None),
                "Output hash or independent rubric score differs",
            )
        else:
            _reject(
                any(
                    value is not None
                    for value in (
                        row.response,
                        row.output_sha256,
                        row.score,
                        row.elapsed_ms,
                        row.input_tokens,
                        row.output_tokens,
                        row.billed_microunits,
                    )
                ),
                "Uncertain attempts cannot contain completed metrics",
            )
        indexed[key] = row

    return indexed


def compare_atomic(
    document: dict[str, Any],
    suite: RoleSuite,
    models: list[ModelTarget],
    *,
    baseline: str,
    candidate: str,
    variants: tuple[PromptVariant, ...] = ("generic", "family", "model_role"),
    repeat: int = 1,
    minimum_cases: int = 12,
) -> dict[str, Any]:
    """Regrade retained bytes and compare the complete declared holdout matrix.

    Repeats never increase independent sample size. Missing/uncertain attempts
    stay visible and block comparison readiness, rather than improving a score.
    """
    _reject(
        type(repeat) is not int
        or not 1 <= repeat <= 5
        or type(minimum_cases) is not int
        or not 4 <= minimum_cases <= 200
        or not variants
        or len(set(variants)) != len(variants)
        or bool(set(variants) - {"generic", "family", "model_role"}),
        "Invalid atomic comparison bounds",
    )
    targets = {model.id: model for model in models}
    _reject(
        len(targets) != len(models)
        or baseline == candidate
        or baseline not in targets
        or candidate not in targets,
        "Comparison needs two distinct configured targets",
    )
    selected = (targets[baseline], targets[candidate])
    _reject(
        (selected[0].model, selected[0].revision, selected[0].backend, selected[0].provider)
        == (selected[1].model, selected[1].revision, selected[1].backend, selected[1].provider),
        "Aliases for the same model revision are not independent targets",
    )
    indexed = validated_atomic_observations(
        document, suite, models, variants=variants, repeat=repeat
    )
    kinds = {kind.name for kind in ATOMIC_KINDS}

    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for case in suite.cases:
        if case.split == "holdout":
            groups[((case.context or {})["atomic_kind"], case.role)].append(case)
    comparisons = []
    for (kind, role), group in sorted(groups.items()):
        for variant in variants:
            metrics: dict[str, list[Observation]] = {baseline: [], candidate: []}
            differences: list[int] = []
            missing = uncertain = structural = 0
            for case in group:
                paired: dict[str, list[Observation]] = {baseline: [], candidate: []}
                for model in selected:
                    for trial in range(1, repeat + 1):
                        attempt = indexed.get((model.id, case.id, variant, trial))
                        if attempt is None:
                            missing += 1
                        elif attempt.status != "completed":
                            uncertain += 1
                        else:
                            paired[model.id].append(attempt)
                            metrics[model.id].append(attempt)
                if case.grader == "json_shape":
                    structural += 1
                elif all(len(paired[model.id]) == repeat for model in selected):
                    differences.append(
                        int(all(r.score == 1 for r in paired[candidate]))
                        - int(all(r.score == 1 for r in paired[baseline]))
                    )
            n = len(differences)
            delta = statistics.mean(differences) if differences else None
            # Conservative paired Hoeffding interval; trials of a case are one unit.
            radius = math.sqrt(2 * math.log(40) / n) if n else None
            comparisons.append(
                {
                    "atomic_kind": kind,
                    "role": role,
                    "variant": variant,
                    "holdout_cases": len(group),
                    "paired_objective_cases": n,
                    "missing_attempts": missing,
                    "indeterminate_attempts": uncertain,
                    "structural_only_cases": structural,
                    "candidate_minus_baseline_accuracy": delta,
                    "paired_interval_95": [max(-1.0, delta - radius), min(1.0, delta + radius)]
                    if delta is not None and radius is not None
                    else None,
                    "comparison_ready": n >= minimum_cases
                    and not missing
                    and not uncertain
                    and not structural
                    and all(
                        model.revision.casefold() not in {"unknown", "unqualified", "latest"}
                        for model in selected
                    ),
                    "baseline": _metrics(metrics[baseline]),
                    "candidate": _metrics(metrics[candidate]),
                }
            )
    covered = {key[0] for key in groups}
    return {
        "schema": "potbot.atomic-role-comparison/1",
        "suite_sha256": suite.identity,
        "evidence_sha256": content_hash(document),
        "baseline": baseline,
        "candidate": candidate,
        "targets_sha256": content_hash(
            [
                {name: getattr(model, name) for name in model.__dataclass_fields__}
                for model in models
            ]
        ),
        "minimum_distinct_cases": minimum_cases,
        "repeat": repeat,
        "comparisons": comparisons,
        "uncovered_atomic_kinds": sorted(kinds - covered),
        "unknown_measurements": ["peak_ram", "peak_vram", "host_runtime", "licence_acceptance"],
        "qualified": False,
        "automatic_routing_changes": 0,
        "scope": "retained rubric evidence; provider identity and live deployment unverified",
    }
