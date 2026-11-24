#!/usr/bin/env python3
"""Opt-in agentic role/prompt matrix benchmark (no downloads or paid calls by default).

Preflight: python scripts/benchmark_agentic_models.py preflight --suite ... --models ...
Run:       python scripts/benchmark_agentic_models.py run --suite ... --models ... --output-dir ...
Monitor:   python scripts/benchmark_agentic_models.py monitor --history-dir ... --output ...
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.domain.errors import InvalidInputError, SchemaViolationError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_model_bench import (
    ModelTarget,
    Observation,
    RoleSuite,
    judge_agreement,
    judge_case,
    monitor_history,
    observe_case,
    prompt_for_case,
    summarise,
)
from potbot.evaluation.agentic_model_transport import BenchmarkTransport
from potbot.evaluation.agentic_monitor import HealthEvent, review_health_events
from potbot.models.hosted_policy import HostedPolicy
from potbot.models.prompt_compiler import PromptVariant
from potbot.service_credentials import ServiceCredentials

_VARIANTS: tuple[PromptVariant, ...] = ("generic", "family", "model_role")


def _read(path: Path, *, limit: int = 1024 * 1024) -> Any:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise InvalidInputError("benchmark input exceeds the size limit")
    return json.loads(raw.decode("utf-8"))


def _strict_read(path: Path, *, limit: int = 8 * 1024 * 1024) -> Any:
    from potbot.models.envelopes import strict_json

    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    return strict_json(raw, max_output_bytes=limit)


def _targets(path: Path) -> list[ModelTarget]:
    document = _read(path)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "models"}
        or document["schema_version"] != 1
        or not isinstance(document["models"], list)
    ):
        raise InvalidInputError("invalid model target document")
    targets = [ModelTarget.from_dict(x) for x in document["models"]]
    if not 1 <= len(targets) <= 16 or len({m.id for m in targets}) != len(targets):
        raise InvalidInputError("model targets must be distinct and bounded")
    return targets


def _report_html(document: dict[str, Any]) -> str:
    rows = document["summary"]["rows"]
    header = "<tr><th>Role</th><th>Model</th><th>Prompt</th><th>Completed</th><th>Pass</th><th>Wilson lower 95%</th><th>Median ms</th><th>Measured billed µunits</th></tr>"
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(cell))}</td>"
            for cell in (
                r["role"],
                r["model_id"],
                r["variant"],
                f"{r['completed']}/{r['total']}",
                r["accuracy"],
                r["wilson_lower_95"],
                r["median_latency_ms"],
                r["measured_billed_microunits"],
            )
        )
        + "</tr>"
        for r in rows
    )
    detail = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(cell))}</td>"
            for cell in (
                r["case_id"],
                r["role"],
                r["model_id"],
                r["variant"],
                r["input_profile"],
                r["trial"],
                r["status"],
                r["returned_model"],
                r["model_identity"],
                r["score"],
                r["elapsed_ms"],
                (
                    r["response"]
                    if r["response"] is not None
                    else "[output hash only: " + str(r["output_sha256"]) + "]"
                ),
            )
        )
        + "</tr>"
        for r in sorted(
            document["results"],
            key=lambda v: (v["case_id"], v["variant"], v["trial"], v["model_id"]),
        )
    )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<title>Potbot model-role benchmark</title>"
        "<style>body{font:15px system-ui;max-width:1300px;margin:3em auto;padding:0 1em}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:.5em;text-align:left}"
        "th{background:#eee}small{color:#555}</style>"
        "<h1>Potbot model-role comparison</h1>"
        "<p>Objective fixture/rubric matches, not verified end-to-end task execution. "
        "Scores and routing recommendations are <strong>advisory only</strong>.</p>"
        f"<p>Run {html.escape(document['run_id'])}; suite {html.escape(document['suite_sha256'])}</p>"
        f"<p>Execution: {html.escape(document['execution']['status'])}. Returned labels are unauthenticated declarations.</p>"
        f"<table>{header}{body}</table>"
        "<h2>Side-by-side attempts (same suite tasks and input hashes)</h2>"
        "<table><tr><th>Case</th><th>Role</th><th>Model</th><th>Prompt profile</th>"
        "<th>Input profile</th><th>Trial</th><th>Status</th><th>Returned model label</th>"
        "<th>Label comparison</th><th>Rubric match</th><th>Latency ms</th><th>Output</th></tr>"
        f"{detail}</table>"
        "<h2>Paired input-profile comparisons</h2>"
        f"<pre>{html.escape(json.dumps(document['summary']['input_comparisons'], indent=2))}</pre>"
        "<h2>Judge calibration</h2>"
        f"<pre>{html.escape(json.dumps(document['judges'], indent=2))}</pre>"
        "<small>Unknown tokens/billing are displayed as unknown, not as zero.</small></html>"
    )


def _private_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation; benchmark receipts are never silently overwritten.
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(contents)


def _preflight(
    models: list[ModelTarget], suite: RoleSuite, variants: list[PromptVariant], repeat: int
) -> dict[str, Any]:
    matrix = [
        (case.id, case.role, model.id, variant, trial)
        for case in suite.cases
        for model in models
        if case.role in model.roles
        for variant in variants
        for trial in range(1, repeat + 1)
    ]
    paid = sum(
        1
        for _, _, mid, _, _ in matrix
        if next(m for m in models if m.id == mid).backend == "hosted"
    )
    local = sum(
        1
        for _, _, mid, _, _ in matrix
        if next(m for m in models if m.id == mid).backend
        in {"loopback_openai", "loopback_ollama_v1"}
    )
    return {
        "suite_sha256": suite.identity,
        "model_targets_sha256": content_hash([asdict(model) for model in models]),
        "matrix_attempts": len(matrix),
        "possible_hosted_calls": paid,
        "possible_local_calls": local,
        "deterministic_attempts": len(matrix) - paid - local,
        "network_started": False,
        "qualified": False,
    }


def _parsed_variants(raw: str) -> list[PromptVariant]:
    variants = raw.split(",")
    if (
        not variants
        or any(v not in _VARIANTS for v in variants)
        or len(set(variants)) != len(variants)
    ):
        raise InvalidInputError("choose distinct prompt variants: generic,family,model_role")
    return variants  # type: ignore[return-value]


def _review_admission(
    args: argparse.Namespace, suite: RoleSuite, models: list[ModelTarget]
) -> str | None:
    marked = suite.id.startswith("atomic-review-") or any(
        "review_pair_sha256" in (case.context or {}) for case in suite.cases
    )
    path = getattr(args, "review_bundle", None)
    if path is None:
        if marked:
            raise InvalidInputError("frozen reviewer tasks require their exact provenance bundle")
        return None
    from potbot.evaluation.agentic_reviewers import admit_reviewer_models, reviewer_suite

    bundle = _read(path, limit=16 * 1024 * 1024)
    frozen, _, subjects = reviewer_suite(bundle)
    if frozen != suite:
        raise InvalidInputError("reviewer suite differs from its frozen bundle")
    admit_reviewer_models(models, subjects)
    return str(bundle["bundle_sha256"])


def _candidate_admission(args: argparse.Namespace, suite: RoleSuite) -> dict[str, Any] | None:
    path = getattr(args, "candidate_package", None)
    review_path = getattr(args, "gold_review", None)
    if path is None:
        if review_path is not None:
            raise InvalidInputError("gold review requires its exact candidate package")
        return None
    if getattr(args, "dataset_card", None) is not None:
        raise InvalidInputError("public fixture cards and private candidates are separate inputs")
    from potbot.evaluation.agentic_candidate_dataset import (
        audit_candidate_review,
        candidate_package,
    )

    document = _strict_read(path)
    frozen, package = candidate_package(document)
    if frozen != suite:
        raise InvalidInputError("candidate suite differs from its frozen package")
    return audit_candidate_review(package, _strict_read(review_path) if review_path else None)


def _deployment_admission(
    args: argparse.Namespace, suite: RoleSuite, models: list[ModelTarget]
) -> dict[str, Any]:
    bindings = getattr(args, "deployment_bindings", None)
    root = getattr(args, "project_root", None)
    if (bindings is None) != (root is None):
        raise InvalidInputError("deployment bindings and owning project root are required together")
    if bindings is None:
        return {
            "status": "unbound",
            "qualified": False,
            "server_loaded_identity_authenticated": False,
        }
    from potbot.evaluation.agentic_deployments import inspect_benchmark_deployments
    from potbot.models.envelopes import strict_json

    with bindings.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    document = strict_json(raw, max_output_bytes=1024 * 1024)
    return inspect_benchmark_deployments(document, models, suite, project_root=root)


def _experiment_evidence(args: argparse.Namespace) -> dict[str, Any]:
    evidence = {
        name: _strict_read(getattr(args, name)) if getattr(args, name, None) else None
        for name in (
            "candidate_package",
            "gold_review",
            "dataset_card",
            "deployment_bindings",
            "review_bundle",
        )
    }
    if getattr(args, "shadow_policy", None) is not None:
        evidence["shadow_policy"] = _strict_read(args.shadow_policy)
    return evidence


def _experiment_admission(
    args: argparse.Namespace,
    suite: RoleSuite,
    models: list[ModelTarget],
    variants: list[PromptVariant],
) -> dict[str, Any] | None:
    path = getattr(args, "experiment_plan", None)
    if path is None:
        if getattr(args, "shadow_policy", None) is not None:
            raise InvalidInputError("shadow criteria require their frozen experiment plan")
        return None
    from potbot.evaluation.agentic_experiments import (
        admit_atomic_experiment,
        read_experiment_document,
    )

    if getattr(args, "judge_derived", False):
        raise InvalidInputError(
            "freeze reviewer experiments separately; no unplanned derived calls"
        )
    if getattr(args, "command", "run") == "run" and not args.store_text:
        raise InvalidInputError(
            "frozen experiment auditing requires explicit private text retention"
        )
    return admit_atomic_experiment(
        read_experiment_document(path),
        suite,
        models,
        variants=tuple(variants),
        repeat=args.repeat,
        shuffle_seed=args.shuffle_seed,
        evidence=_experiment_evidence(args),
        execution_order=getattr(args, "execution_order", "shuffled"),
    )


def _execute(
    args: argparse.Namespace,
    suite: RoleSuite,
    models: list[ModelTarget],
    variants: list[PromptVariant],
    policy: HostedPolicy | None,
    *,
    run_id: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not 1 <= args.repeat <= 5:
        raise InvalidInputError("repeats must be between one and five")
    experiment = _experiment_admission(args, suite, models, variants)
    review_bundle_sha256 = _review_admission(args, suite, models)
    candidate_review = _candidate_admission(args, suite)
    if candidate_review is not None and candidate_review["status"] != "declared_agreement":
        raise InvalidInputError("candidate gold review is incomplete or conflicting")
    deployment_inspection = _deployment_admission(args, suite, models)
    if deployment_inspection["status"] == "blocked":
        raise InvalidInputError("local benchmark deployment preflight is blocked")
    plan = _preflight(models, suite, variants, args.repeat)
    if not plan["matrix_attempts"] or plan["matrix_attempts"] > 1600:
        raise InvalidInputError("benchmark matrix is empty or exceeds 1600 attempts")
    dataset_audit = None
    if getattr(args, "dataset_card", None) is not None:
        from potbot.evaluation.agentic_dataset import audit_atomic_dataset

        dataset_audit = audit_atomic_dataset(suite, _read(args.dataset_card))
    if any(m.backend != "deterministic" for m in models) and not args.confirm_prompt_disclosure:
        raise InvalidInputError("running any model requires explicit prompt disclosure consent")
    if plan["possible_hosted_calls"]:
        if (
            not args.allow_paid
            or policy is None
            or not policy.allow_prompt_disclosure
            or plan["possible_hosted_calls"] > args.max_paid_calls
            or args.max_cost_microunits <= 0
        ):
            raise InvalidInputError("hosted benchmarks require policy, paid opt-in and finite caps")
        # Check the whole main matrix BEFORE sending any paid request. The
        # optional derived-judge cohort has unknown size, and is separately
        # bounded by the same running caps.
        admission = 0
        for case in suite.cases:
            for model in models:
                if model.backend != "hosted" or case.role not in model.roles:
                    continue
                assert model.provider is not None
                rule = policy.rule(model.provider, model.model)
                if (
                    rule.input_microunits_per_million <= 0
                    and rule.output_microunits_per_million <= 0
                ):
                    raise InvalidInputError("hosted benchmark price rules require a nonzero rate")
                for variant in variants:
                    packet = prompt_for_case(model.model, case, variant)
                    request, _, _ = policy.request(
                        {
                            "provider": model.provider,
                            "model": model.model,
                            "role": "coding" if case.role == "coding" else "reasoning",
                        },
                        {
                            "prompt": packet.user,
                            "system": packet.system,
                            "max_output_tokens": model.max_output_tokens,
                            "timeout_seconds": 90,
                        },
                    )
                    policy.authorize(request)
                    admission += args.repeat * rule.charge(
                        len((packet.user + packet.system).encode()) + 1024,
                        model.max_output_tokens,
                    )
        if admission > args.max_cost_microunits:
            raise InvalidInputError("whole hosted benchmark exceeds global cost allowance")
    cost_reserved = 0
    paid_attempts = 0
    transport = BenchmarkTransport(
        policy=policy,
        credentials=ServiceCredentials() if policy else None,
        local_key=os.environ.get("POTBOT_BENCH_LOCAL_KEY", "local-only"),
    )

    def run_one(model: ModelTarget, packet: Any) -> Any:
        nonlocal cost_reserved, paid_attempts
        if model.backend == "hosted":
            assert policy is not None and model.provider is not None
            rule = policy.rule(model.provider, model.model)
            if paid_attempts >= args.max_paid_calls:
                raise InvalidInputError("total hosted benchmark call cap exhausted")
            # Policy-estimated max must be admitted before any possible network send.
            bound = rule.charge(
                len((packet.user + packet.system).encode()) + 1024, model.max_output_tokens
            )
            if cost_reserved + bound > args.max_cost_microunits:
                raise InvalidInputError("global hosted benchmark budget exhausted")
            cost_reserved += bound  # Held even on uncertain provider completion.
            paid_attempts += 1
        return transport.request(model, packet)

    randomizer = random.Random(args.shuffle_seed)
    grid = [
        (case, model, variant, trial)
        for case in suite.cases
        for model in models
        if case.role in model.roles
        for variant in variants
        for trial in range(1, args.repeat + 1)
    ]
    randomizer.shuffle(grid)
    execution_order = getattr(args, "execution_order", "shuffled")
    if execution_order not in {"shuffled", "model_blocks_v1"}:
        raise InvalidInputError("Unknown benchmark execution order")
    if execution_order == "model_blocks_v1":
        # Stable grouping preserves the randomized cell order within a model,
        # and randomizes block order. This avoids loading weights for each cell.
        model_order = list(models)
        randomizer.shuffle(model_order)
        rank = {model.id: index for index, model in enumerate(model_order)}
        grid.sort(key=lambda cell: rank[cell[1].id])
    observations: list[Observation] = []
    identity_mismatch = False
    transport_blocked = False
    for index, (case, model, variant, trial) in enumerate(grid, start=1):
        key = {"case_id": case.id, "model_id": model.id, "variant": variant, "trial": trial}
        if on_event:
            # Persist before dispatch: a process crash cannot later be mistaken
            # for a call that was never attempted.
            on_event({"phase": "possibly_dispatched", **key})
        row = observe_case(
            model=model,
            case=case,
            variant=variant,
            trial=trial,
            generate=run_one,
            retain_response=True,
        )
        observations.append(row)
        if on_event:
            safe_row = replace(row, response=row.response if args.store_text else None)
            on_event({"phase": "terminal", "result": safe_row.to_dict()})
        print(
            f"[Potbot benchmark] {index}/{len(grid)} {model.id} {case.role} {variant} {row.status}",
            file=sys.stderr,
        )
        if getattr(transport, "execution_blocked", False):
            transport_blocked = True
            break
        if row.status == "identity_mismatch":
            identity_mismatch = True
            break
    judge_rows: list[Observation] = []
    if args.judge_derived and not identity_mismatch and not transport_blocked:
        from potbot.evaluation.agentic_reviewers import model_revision_identity

        # Only derive a pair when independently graded outputs have different
        # correctness. Never ask a model to judge its own output.
        by_case: dict[tuple[str, str, int], list[Observation]] = {}
        for row in observations:
            if row.score is not None and row.response is not None:
                by_case.setdefault((row.case_id, row.variant, row.trial), []).append(row)
        pairs = 0
        cases = {case.id: case for case in suite.cases}
        for (case_id, variant, trial), sample in sorted(by_case.items()):
            passing = [r for r in sample if r.score == 1]
            failing = [r for r in sample if r.score == 0]
            if not passing or not failing or pairs >= args.max_judge_pairs:
                continue
            first, second = passing[0], failing[0]
            if len(first.response or "") + len(second.response or "") > 12000:
                # Never silently truncate candidate evidence before judging it.
                continue
            for judge_model in models:
                source_identities = {
                    model_revision_identity(model)
                    for model in models
                    if model.id in {first.model_id, second.model_id}
                }
                if (
                    "judge" not in judge_model.roles
                    or model_revision_identity(judge_model) in source_identities
                ):
                    continue
                for reverse in (False, True):
                    jcase = judge_case(
                        f"{case_id}-{variant}-{trial}-{first.model_id}-{second.model_id}",
                        cases[case_id].task,
                        first.response or "",
                        second.response or "",
                        reference_winner="A",
                        reverse=reverse,
                    )
                    judge_key = {
                        "case_id": jcase.id,
                        "model_id": judge_model.id,
                        "variant": variant,
                        "trial": trial,
                    }
                    if on_event:
                        on_event({"phase": "possibly_dispatched", **judge_key})
                    judged = observe_case(
                        model=judge_model,
                        case=jcase,
                        variant=variant,
                        trial=trial,
                        generate=run_one,
                        retain_response=args.store_text,
                    )
                    judge_rows.append(judged)
                    if on_event:
                        on_event({"phase": "terminal", "result": judged.to_dict()})
                    if judged.status == "identity_mismatch":
                        identity_mismatch = True
                        break
                if identity_mismatch:
                    break
            pairs += 1
            if identity_mismatch:
                break
    all_rows = observations + judge_rows
    report = {
        "schema_version": 1,
        "run_id": run_id or str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "suite_sha256": suite.identity,
        "model_targets_sha256": content_hash([asdict(model) for model in models]),
        "mode": "measured_reference_algorithms"
        if all(m.backend == "deterministic" for m in models)
        else "measured_requests",
        "dataset_audit": dataset_audit,
        "candidate_dataset_review": candidate_review,
        "deployment_inspection": deployment_inspection,
        "execution": {
            "status": "stopped_model_identity_mismatch"
            if identity_mismatch
            else "stopped_transport_failure"
            if transport_blocked
            else "completed",
            "scheduled_primary_attempts": len(grid),
            "unattempted_primary_attempts": len(grid) - len(observations),
            "no_automatic_replay": True,
            "returned_model_labels_authenticated": False,
        },
        "experiment_plan_sha256": experiment["plan_sha256"] if experiment else None,
        "review_bundle_sha256": review_bundle_sha256,
        "reference_runtime": None,
        "admission_ceiling_microunits": args.max_cost_microunits,
        "reserved_ceiling_microunits": cost_reserved,
        "hosted_attempted_calls": paid_attempts,
        "results": [
            replace(row, response=row.response if args.store_text else None).to_dict()
            for row in all_rows
        ],
        "summary": summarise(all_rows, minimum_samples=args.min_samples),
        "judges": judge_agreement(all_rows),
        "qualified": False,
        "auto_promoted": False,
    }
    if execution_order != "shuffled":
        report["execution"]["execution_order"] = execution_order
    if any(m.backend == "loopback_ollama_v1" for m in models):
        from potbot.evaluation.agentic_ollama_transport import transport_profile

        report["transport_profiles"] = {"loopback_ollama_v1": transport_profile()}
    from potbot.evaluation.agentic_openai_profile import profile_for_target

    openai_profiles = {m.id: p for m in models if (p := profile_for_target(m)) is not None}
    if openai_profiles:
        report.setdefault("transport_profiles", {})["openai_cheap_v1"] = openai_profiles
    if any(m.backend == "deterministic" for m in models):
        from potbot.evaluation.agentic_reference import reference_runtime

        report["reference_runtime"] = reference_runtime()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run"):
        group = command.add_parser(name)
        group.add_argument("--suite", type=Path, required=True)
        group.add_argument("--models", type=Path, required=True)
        group.add_argument("--dataset-card", type=Path)
        group.add_argument("--candidate-package", type=Path)
        group.add_argument("--gold-review", type=Path)
        group.add_argument("--review-bundle", type=Path)
        group.add_argument("--deployment-bindings", type=Path)
        group.add_argument("--project-root", type=Path)
        group.add_argument("--experiment-plan", type=Path)
        group.add_argument("--shadow-policy", type=Path)
        group.add_argument("--shuffle-seed", type=int, default=42)
        group.add_argument(
            "--execution-order", choices=("shuffled", "model_blocks_v1"), default="shuffled"
        )
        group.add_argument("--variants", default="generic,family,model_role")
        group.add_argument("--repeat", type=int, default=1)
        if name == "run":
            group.add_argument("--output-dir", type=Path, required=True)
            group.add_argument("--provider-policy", type=Path)
            group.add_argument("--allow-paid", action="store_true")
            group.add_argument("--confirm-prompt-disclosure", action="store_true")
            group.add_argument("--max-paid-calls", type=int, default=0)
            group.add_argument("--max-cost-microunits", type=int, default=0)
            group.add_argument("--judge-derived", action="store_true")
            group.add_argument("--max-judge-pairs", type=int, default=8)
            group.add_argument("--min-samples", type=int, default=12)
            group.add_argument("--store-text", action="store_true")
    for name in ("prepare-experiment", "audit-experiment", "shadow-routing"):
        experiment = command.add_parser(name)
        experiment.add_argument("--suite", type=Path, required=True)
        experiment.add_argument("--models", type=Path, required=True)
        experiment.add_argument("--output", type=Path, required=True)
        for binding in (
            "candidate-package",
            "gold-review",
            "dataset-card",
            "deployment-bindings",
            "review-bundle",
            "shadow-policy",
        ):
            experiment.add_argument("--" + binding, type=Path)
        if name == "prepare-experiment":
            experiment.add_argument("--variants", default="generic,family,model_role")
            experiment.add_argument("--repeat", type=int, default=1)
            experiment.add_argument("--shuffle-seed", type=int, default=42)
            experiment.add_argument(
                "--execution-order", choices=("shuffled", "model_blocks_v1"), default="shuffled"
            )
        else:
            experiment.add_argument("--experiment-plan", type=Path, required=True)
            experiment.add_argument("--results", type=Path, required=True)
    prepare = command.add_parser("prepare-atomic-dataset")
    prepare.add_argument("--suite", type=Path, required=True)
    prepare.add_argument("--provenance", type=Path, required=True)
    prepare.add_argument("--controls", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    gold = command.add_parser("audit-atomic-gold-review")
    gold.add_argument("--candidate-package", type=Path, required=True)
    gold.add_argument("--gold-review", type=Path)
    gold.add_argument("--output", type=Path, required=True)
    build = command.add_parser("build-review-suite")
    build.add_argument("--suite", required=True, type=Path)
    build.add_argument("--models", required=True, type=Path)
    build.add_argument("--results", required=True, type=Path)
    build.add_argument("--first", required=True)
    build.add_argument("--second", required=True)
    build.add_argument("--source-variant", default="generic")
    build.add_argument("--source-trial", type=int, default=1)
    build.add_argument("--variants", default="generic,family,model_role")
    build.add_argument("--repeat", type=int, default=1)
    build.add_argument("--output-dir", required=True, type=Path)
    calibration = command.add_parser("calibrate-reviewers")
    calibration.add_argument("--review-bundle", required=True, type=Path)
    calibration.add_argument("--models", required=True, type=Path)
    calibration.add_argument("--results", required=True, type=Path)
    calibration.add_argument("--variants", default="generic,family,model_role")
    calibration.add_argument("--repeat", type=int, default=1)
    calibration.add_argument("--minimum-pairs", type=int, default=12)
    calibration.add_argument("--output", required=True, type=Path)
    pin = command.add_parser("pin-reference")
    pin.add_argument("--output", required=True, type=Path)
    audit = command.add_parser("audit-atomic-suite")
    audit.add_argument("--suite", type=Path, required=True)
    audit.add_argument("--dataset-card", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    compare = command.add_parser("compare-atomic")
    compare.add_argument("--suite", required=True, type=Path)
    compare.add_argument("--models", required=True, type=Path)
    compare.add_argument("--results", required=True, type=Path)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--variants", default="generic,family,model_role")
    compare.add_argument("--repeat", type=int, default=1)
    compare.add_argument("--minimum-cases", type=int, default=12)
    compare.add_argument("--output", required=True, type=Path)
    monitor = command.add_parser("monitor")
    monitor.add_argument("--history-dir", required=True, type=Path)
    monitor.add_argument("--output", required=True, type=Path)
    monitor.add_argument(
        "--events", type=Path, help="Optional bounded workflow/assumption health JSON"
    )
    try:
        args = parser.parse_args(argv)
        if args.command in {"prepare-experiment", "audit-experiment", "shadow-routing"}:
            from potbot.evaluation.agentic_experiments import (
                audit_atomic_experiment,
                freeze_atomic_experiment,
                read_experiment_document,
            )

            suite = RoleSuite.from_dict(_strict_read(args.suite))
            models = _targets(args.models)
            evidence = _experiment_evidence(args)
            if args.command == "prepare-experiment":
                payload = freeze_atomic_experiment(
                    suite,
                    models,
                    variants=tuple(_parsed_variants(args.variants)),
                    repeat=args.repeat,
                    shuffle_seed=args.shuffle_seed,
                    evidence=evidence,
                    execution_order=args.execution_order,
                )
            elif args.command == "shadow-routing":
                from potbot.evaluation.agentic_shadow import evaluate_atomic_shadow

                payload = evaluate_atomic_shadow(
                    evidence.get("shadow_policy"),
                    read_experiment_document(args.experiment_plan),
                    read_experiment_document(args.results),
                    suite,
                    models,
                    evidence=evidence,
                )
            else:
                payload = audit_atomic_experiment(
                    read_experiment_document(args.experiment_plan),
                    read_experiment_document(args.results),
                    suite,
                    models,
                    evidence=evidence,
                )
            _private_write(args.output, canonical_json(payload) + "\n")
            print(json.dumps({"plan_sha256": payload.get("plan_sha256"), "qualified": False}))
            return (
                2
                if (
                    payload.get("complete_for_analysis") is False
                    or payload.get("all_offline_limits_met") is False
                )
                else 0
            )
        if args.command == "prepare-atomic-dataset":
            from potbot.evaluation.agentic_candidate_dataset import (
                gold_review_request,
                prepare_candidate_package,
            )

            package = prepare_candidate_package(
                RoleSuite.from_dict(_strict_read(args.suite)),
                _strict_read(args.provenance),
                _strict_read(args.controls),
            )
            request = gold_review_request(package)
            args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            _private_write(
                args.output_dir / "candidate-package.json", canonical_json(package) + "\n"
            )
            _private_write(args.output_dir / "review-request.json", canonical_json(request) + "\n")
            print(
                json.dumps(
                    {
                        "package_sha256": package["package_sha256"],
                        "case_count": len(package["suite"]["cases"]),
                        "qualified": False,
                    }
                )
            )
            return 0
        if args.command == "audit-atomic-gold-review":
            from potbot.evaluation.agentic_candidate_dataset import audit_candidate_review

            report = audit_candidate_review(
                _strict_read(args.candidate_package),
                _strict_read(args.gold_review) if args.gold_review else None,
            )
            _private_write(args.output, canonical_json(report) + "\n")
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "case_count": report["case_count"],
                        "qualified": False,
                    }
                )
            )
            return 0 if report["status"] == "declared_agreement" else 2
        if args.command == "build-review-suite":
            from potbot.evaluation.agentic_reviewers import build_reviewer_bundle

            payload = build_reviewer_bundle(
                _read(args.results, limit=16 * 1024 * 1024),
                RoleSuite.from_dict(_read(args.suite)),
                _targets(args.models),
                first=args.first,
                second=args.second,
                variants=tuple(_parsed_variants(args.variants)),
                repeat=args.repeat,
                source_variant=args.source_variant,
                source_trial=args.source_trial,
            )
            args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            _private_write(args.output_dir / "review-bundle.json", canonical_json(payload) + "\n")
            _private_write(
                args.output_dir / "review-suite.json", canonical_json(payload["suite"]) + "\n"
            )
            print(
                json.dumps(
                    {
                        "bundle_sha256": payload["bundle_sha256"],
                        "source_tasks": len(payload["pairs"]),
                        "output_dir": str(args.output_dir),
                        "qualified": False,
                    }
                )
            )
            return 0
        if args.command == "calibrate-reviewers":
            from potbot.evaluation.agentic_reviewers import calibrate_reviewers

            payload = calibrate_reviewers(
                _read(args.review_bundle, limit=16 * 1024 * 1024),
                _read(args.results, limit=16 * 1024 * 1024),
                _targets(args.models),
                variants=tuple(_parsed_variants(args.variants)),
                repeat=args.repeat,
                minimum_pairs=args.minimum_pairs,
            )
            _private_write(args.output, canonical_json(payload) + "\n")
            print("Reviewer calibration retained; no model calls or routing changes.")
            return 0
        if args.command == "pin-reference":
            from potbot.evaluation.agentic_reference import (
                REFERENCE_MODEL,
                REFERENCE_ROLES,
                reference_identity,
            )

            target = ModelTarget(
                "atomic-reference",
                REFERENCE_MODEL,
                "deterministic",
                REFERENCE_ROLES,
                reference_identity(),
            )
            _private_write(
                args.output,
                canonical_json(
                    {
                        "schema_version": 1,
                        "models": [asdict(target)],
                    }
                )
                + "\n",
            )
            print("Reference source pin retained; no model or network request.")
            return 0
        if args.command == "audit-atomic-suite":
            from potbot.evaluation.agentic_dataset import audit_atomic_dataset

            payload = audit_atomic_dataset(
                RoleSuite.from_dict(_read(args.suite)), _read(args.dataset_card)
            )
            _private_write(args.output, canonical_json(payload) + "\n")
            print("Atomic fixture integrity audited; independent gold review remains pending.")
            return 0
        if args.command == "compare-atomic":
            from potbot.evaluation.agentic_atomic_bench import compare_atomic

            payload = compare_atomic(
                _read(args.results, limit=16 * 1024 * 1024),
                RoleSuite.from_dict(_read(args.suite)),
                _targets(args.models),
                baseline=args.baseline,
                candidate=args.candidate,
                variants=tuple(_parsed_variants(args.variants)),
                repeat=args.repeat,
                minimum_cases=args.minimum_cases,
            )
            _private_write(args.output, canonical_json(payload) + "\n")
            print("Atomic comparison retained; no model calls or routing changes.")
            return 0
        if args.command == "monitor":
            inputs = sorted(args.history_dir.glob("*/report.json"))
            if len(inputs) > 500:
                raise InvalidInputError("monitor has too many reports")
            documents = [_read(p, limit=4 * 1024 * 1024) for p in inputs]
            documents.sort(key=lambda d: d.get("created_at", ""))
            payload = monitor_history(documents)
            if args.events is not None:
                entries = _read(args.events, limit=2 * 1024 * 1024)
                if (
                    not isinstance(entries, dict)
                    or set(entries) != {"schema_version", "events"}
                    or entries["schema_version"] != 1
                    or not isinstance(entries["events"], list)
                    or len(entries["events"]) > 10000
                ):
                    raise InvalidInputError("monitor requires bounded versioned events")
                payload["workflow_and_assumptions"] = review_health_events(
                    [HealthEvent.from_dict(item) for item in entries["events"]]
                )
            _private_write(args.output, canonical_json(payload) + "\n")
            print(f"Audited {len(documents)} report(s); recommendations remain advisory.")
            return 0
        suite = RoleSuite.from_dict(_read(args.suite))
        models = _targets(args.models)
        variants = _parsed_variants(args.variants)
        if not 1 <= args.repeat <= 5:
            raise InvalidInputError("invalid repeat count")
        if args.command == "preflight":
            experiment = _experiment_admission(args, suite, models, variants)
            review_bundle_sha256 = _review_admission(args, suite, models)
            plan = _preflight(models, suite, variants, args.repeat)
            plan["deployment_inspection"] = _deployment_admission(args, suite, models)
            plan["candidate_dataset_review"] = _candidate_admission(args, suite)
            plan["experiment_plan_sha256"] = experiment["plan_sha256"] if experiment else None
            if review_bundle_sha256 is not None:
                plan["review_bundle_sha256"] = review_bundle_sha256
            if args.dataset_card is not None:
                from potbot.evaluation.agentic_dataset import audit_atomic_dataset

                plan["dataset_audit"] = audit_atomic_dataset(suite, _read(args.dataset_card))
            print(json.dumps(plan, indent=2))
            candidate_review = plan["candidate_dataset_review"]
            return (
                2
                if (
                    plan["deployment_inspection"]["status"] == "blocked"
                    or (
                        candidate_review is not None
                        and candidate_review["status"] != "declared_agreement"
                    )
                )
                else 0
            )
        if (
            not 1 <= args.max_judge_pairs <= 30
            or not 1 <= args.min_samples <= 10000
            or not 0 <= args.max_paid_calls <= 2000
            or not 0 <= args.max_cost_microunits <= 10**12
        ):
            raise InvalidInputError("invalid judge or evidence minimum")
        experiment = _experiment_admission(args, suite, models, variants)
        policy = HostedPolicy.read(args.provider_policy) if args.provider_policy else None
        run_id = str(uuid4())
        destination = args.output_dir / run_id
        _private_write(
            destination / "run.json",
            canonical_json(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "suite_sha256": suite.identity,
                    "status": "started",
                    "experiment_plan_sha256": experiment["plan_sha256"] if experiment else None,
                    "no_automatic_replay": True,
                }
            )
            + "\n",
        )
        if experiment is not None:
            _private_write(destination / "experiment-plan.json", canonical_json(experiment) + "\n")
        journal = destination / "attempts.jsonl"
        # Durable per-attempt journal; incomplete calls are uncertain. A
        # restart must create a NEW run, never append or replay a paid request.
        journal_fd = os.open(str(journal), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(journal_fd, "w", encoding="utf-8") as handle:

            def on_event(event: dict[str, Any]) -> None:
                handle.write(canonical_json(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            report = _execute(
                args, suite, models, variants, policy, run_id=run_id, on_event=on_event
            )
        _private_write(destination / "report.json", canonical_json(report) + "\n")
        _private_write(destination / "comparison.html", _report_html(report))
        print(
            json.dumps(
                {
                    "run_id": report["run_id"],
                    "report_dir": str(destination),
                    "n_attempts": len(report["results"]),
                    "qualified": False,
                },
                indent=2,
            )
        )
        return 0 if report["execution"]["status"] == "completed" else 2
    except (
        InvalidInputError,
        SchemaViolationError,
        ValueError,
        OSError,
        TypeError,
        UnicodeError,
        KeyError,
    ):
        # Do not print raw task/prompt/provider errors, credentials or file contents.
        parser.exit(
            2, "Benchmark configuration or execution failed; inspect safe local configuration.\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
