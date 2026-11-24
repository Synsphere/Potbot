"""Evaluate frozen routing criteria against retained atomic benchmark evidence.

No live shadow requests, runtime selection, promotion or qualification. The
configured baseline is a comparator, not an authenticated rollback route.
"""

from __future__ import annotations

import json
from typing import Any

from potbot.controller.atomic_tasks import ATOMIC_KINDS
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_atomic_bench import compare_atomic
from potbot.evaluation.agentic_experiments import audit_atomic_experiment
from potbot.evaluation.agentic_model_bench import ModelTarget, RoleSuite, _wilson_lower
from potbot.models.prompt_compiler import PromptVariant

_LIMITS = {
    "minimum_distinct_cases": (12, 200),
    "minimum_accuracy_lower_95_bps": (0, 10000),
    "maximum_accuracy_drop_bps": (0, 10000),
    "maximum_p95_latency_ms": (1, 3600000),
    "maximum_mean_billed_microunits": (0, 10**12),
    "maximum_mean_total_tokens": (1, 10**7),
}
_IDENTITY = {"atomic_kind", "role", "variant", "baseline", "candidate"}
_PROMOTION_GATES = [
    "authenticated_independent_gold",
    "protected_holdout_isolation",
    "actual_server_and_worker_identity",
    "measured_peak_ram_and_vram",
    "licence_and_deployment_acceptance",
    "qualified_baseline_and_rollback",
    "live_shadow_and_canary_evidence",
    "operator_promotion",
]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidInputError(message)


def validate_shadow_policy(
    document: object,
    suite: RoleSuite,
    models: list[ModelTarget],
    variants: tuple[PromptVariant, ...],
) -> dict[str, Any]:
    """Require one explicit rule for every planned holdout scope, before dispatch."""
    _require(
        isinstance(document, dict)
        and set(document) == {"schema", "rules"}
        and document.get("schema") == "potbot.atomic-shadow-policy/1"
        and isinstance(document.get("rules"), list)
        and 1 <= len(document["rules"]) <= 64,
        "Shadow policy needs a bounded exact rule document",
    )
    assert isinstance(document, dict)
    targets = {model.id: model for model in models}
    kinds = {kind.name for kind in ATOMIC_KINDS}
    expected = {
        ((case.context or {}).get("atomic_kind"), case.role, variant)
        for case in suite.cases
        if case.split == "holdout"
        for variant in variants
    }
    _require(bool(expected), "Shadow policy requires planned holdout cases")
    scopes: set[tuple[str, str, str]] = set()
    for rule in document["rules"]:
        _require(
            isinstance(rule, dict) and set(rule) == _IDENTITY | set(_LIMITS),
            "Shadow rule fields differ from the explicit contract",
        )
        _require(
            all(isinstance(rule[name], str) for name in _IDENTITY)
            and all(
                type(rule[name]) is int and low <= rule[name] <= high
                for name, (low, high) in _LIMITS.items()
            ),
            "Invalid shadow rule identities or integer limits",
        )
        scope = (rule["atomic_kind"], rule["role"], rule["variant"])
        _require(
            scope not in scopes and scope in expected and scope[0] in kinds,
            "Duplicate or unplanned shadow scope",
        )
        scopes.add(scope)
        baseline, candidate = rule["baseline"], rule["candidate"]
        _require(
            baseline != candidate and baseline in targets and candidate in targets,
            "Shadow rules need distinct configured targets",
        )
        selected = (targets[baseline], targets[candidate])
        _require(
            all(rule["role"] in model.roles for model in selected),
            "Shadow target is not eligible for this role",
        )
        _require(
            (selected[0].model, selected[0].revision, selected[0].backend, selected[0].provider)
            != (selected[1].model, selected[1].revision, selected[1].backend, selected[1].provider),
            "Shadow target aliases do not supply an independent comparison",
        )
    _require(scopes == expected, "Every planned holdout scope needs a shadow rule")
    detached: dict[str, Any] = json.loads(canonical_json(document))
    return detached


def evaluate_atomic_shadow(
    policy: object,
    plan: object,
    document: dict[str, Any],
    suite: RoleSuite,
    models: list[ModelTarget],
    *,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Regrade literal outputs and preserve full denominators; never make a request."""
    _require(
        isinstance(plan, dict) and isinstance(plan.get("variants"), list),
        "Shadow evaluation requires a frozen experiment",
    )
    assert isinstance(plan, dict)
    rules = validate_shadow_policy(policy, suite, models, tuple(plan["variants"]))
    _require(
        canonical_json(evidence.get("shadow_policy")) == canonical_json(rules)
        and isinstance(plan.get("evidence_sha256"), dict)
        and plan["evidence_sha256"].get("shadow_policy") == content_hash(rules),
        "Shadow policy was not bound into this experiment before execution",
    )
    audit = audit_atomic_experiment(plan, document, suite, models, evidence=evidence)
    decisions = []
    for rule in rules["rules"]:
        comparison = compare_atomic(
            document,
            suite,
            models,
            baseline=rule["baseline"],
            candidate=rule["candidate"],
            variants=tuple(plan["variants"]),
            repeat=plan["repeat"],
            minimum_cases=rule["minimum_distinct_cases"],
        )
        paired = next(
            row
            for row in comparison["comparisons"]
            if (row["atomic_kind"], row["role"], row["variant"])
            == (rule["atomic_kind"], rule["role"], rule["variant"])
        )
        cohort = next(
            row
            for row in audit["cohorts"]
            if row["split"] == "holdout"
            and row["model_id"] == rule["candidate"]
            and (row["atomic_kind"], row["role"], row["variant"])
            == (rule["atomic_kind"], rule["role"], rule["variant"])
        )
        blockers = []
        if not audit["complete_for_analysis"]:
            blockers.append("incomplete_experiment")
        if not paired["comparison_ready"]:
            blockers.append("paired_holdout_not_ready")
        lower = _wilson_lower(cohort["all_repeats_passed_cases"], cohort["distinct_cases"])
        if lower < rule["minimum_accuracy_lower_95_bps"] / 10000:
            blockers.append("correctness_floor")
        interval = paired["paired_interval_95"]
        if interval is None or interval[0] < -rule["maximum_accuracy_drop_bps"] / 10000:
            blockers.append("paired_quality_margin")
        metrics = paired["candidate"]
        attempts = metrics["completed_attempts"]
        latency = metrics["p95_latency_ms"]
        if latency is None or latency > rule["maximum_p95_latency_ms"]:
            blockers.append("tail_latency_limit")
        cost = metrics["billed_microunits"]
        if cost is None or not attempts:
            blockers.append("billing_unknown")
        elif cost > rule["maximum_mean_billed_microunits"] * attempts:
            blockers.append("billing_limit")
        tokens = None
        if metrics["input_tokens"] is not None and metrics["output_tokens"] is not None:
            tokens = metrics["input_tokens"] + metrics["output_tokens"]
        if tokens is None or not attempts:
            blockers.append("tokens_unknown")
        elif tokens > rule["maximum_mean_total_tokens"] * attempts:
            blockers.append("token_limit")
        decisions.append(
            {
                "rule": rule,
                "paired_comparison": paired,
                "candidate_accuracy_lower_95": lower,
                "candidate_mean_billed_microunits": cost / attempts
                if cost is not None and attempts
                else None,
                "candidate_mean_total_tokens": tokens / attempts
                if tokens is not None and attempts
                else None,
                "offline_limits_met": not blockers,
                "blockers": blockers,
                "hypothetical_selection": rule["candidate"] if not blockers else rule["baseline"],
                "promotion_ready": False,
            }
        )
    return {
        "schema": "potbot.atomic-shadow-evaluation/1",
        "policy_sha256": content_hash(rules),
        "plan_sha256": audit["plan_sha256"],
        "report_sha256": audit["report_sha256"],
        "experiment_audit": audit,
        "decisions": decisions,
        "all_offline_limits_met": all(row["offline_limits_met"] for row in decisions),
        "promotion_blockers": list(_PROMOTION_GATES),
        "confidence_scope": "individual rule bounds; no multiplicity correction or qualification",
        "prior_registration_authenticated": False,
        "qualified": False,
        "promotion_ready": False,
        "automatic_routing_changes": 0,
        "model_requests": 0,
    }
