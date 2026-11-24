"""Frozen private-candidate packages and blind, declared gold-review agreement.

No reviewer, source licence or access-control claim is authenticated here.
Agreement is a metadata gate for opt-in experiments, never routing authority.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import replace
from typing import Any

from potbot.controller.atomic_tasks import ATOMIC_KINDS
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.agentic_dataset import atomic_input_identity
from potbot.evaluation.agentic_model_bench import RoleSuite, rubric_score, vars_case

_MAX_PACKAGE_BYTES = 8 * 1024 * 1024
_PACKAGE = "potbot.atomic-candidate-package/1"
_REVIEW_INPUT = "potbot.atomic-gold-review-input/1"
_REVIEW = "potbot.atomic-gold-review/1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidInputError(message)


def _text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= maximum


def prepare_candidate_package(
    suite: RoleSuite, provenance: object, controls: object
) -> dict[str, Any]:
    """Freeze caller-supplied candidates; verify oracle controls without execution."""
    _require(
        isinstance(suite, RoleSuite) and type(suite.schema_version) is int,
        "Candidate suite schema must be exact",
    )
    _require(
        isinstance(provenance, dict)
        and set(provenance) == {"author_id", "licence", "source", "exposure"}
        and provenance["exposure"] == "declared_private_candidate"
        and isinstance(provenance["author_id"], str)
        and _ID.fullmatch(provenance["author_id"]) is not None
        and _text(provenance["licence"], 512)
        and _text(provenance["source"], 512),
        "Candidate provenance requires explicit bounded private-source declarations",
    )
    _require(
        isinstance(controls, dict)
        and set(controls) == {"schema", "cases"}
        and controls["schema"] == "potbot.atomic-oracle-controls/1"
        and isinstance(controls["cases"], list)
        and len(controls["cases"]) == len(suite.cases),
        "Every candidate case requires positive and negative oracle controls",
    )
    assert isinstance(provenance, dict) and isinstance(controls, dict)
    kinds = {kind.name for kind in ATOMIC_KINDS}
    cases = {case.id: case for case in suite.cases}
    seen: set[str] = set()
    for case in suite.cases:
        _require(
            isinstance((case.context or {}).get("atomic_kind"), str)
            and (case.context or {})["atomic_kind"] in kinds
            and case.grader != "json_shape"
            and rubric_score(case, "") == 0,
            "Candidates require an existing atomic kind and a substantive nonempty oracle",
        )
        identity = atomic_input_identity(case)
        _require(identity not in seen, "Repeated candidate input or development/holdout leakage")
        seen.add(identity)
    indexed: dict[str, dict[str, Any]] = {}
    for control in controls["cases"]:
        _require(
            isinstance(control, dict)
            and set(control) == {"case_id", "positive_response", "negative_responses"}
            and isinstance(control["case_id"], str)
            and control["case_id"] in cases
            and control["case_id"] not in indexed
            and _text(control["positive_response"], 65536)
            and isinstance(control["negative_responses"], list)
            and 1 <= len(control["negative_responses"]) <= 4
            and all(_text(response, 65536) for response in control["negative_responses"]),
            "Invalid or duplicate oracle control",
        )
        case = cases[control["case_id"]]
        negatives = control["negative_responses"]
        _require(
            len(set(negatives)) == len(negatives)
            and rubric_score(case, control["positive_response"]) == 1
            and all(rubric_score(case, response) == 0 for response in negatives),
            "Oracle must accept its positive control and reject every negative control",
        )
        indexed[case.id] = control
    _require(set(indexed) == set(cases), "Oracle control coverage is incomplete")
    # Neither per-case expected values nor their guessable hashes enter the
    # request sent to a reviewer. Split/author identity and controls stay private.
    inputs = {
        "schema": _REVIEW_INPUT,
        "dataset_id": suite.id,
        "cases": [
            {
                "case_id": case.id,
                "role": case.role,
                "task": case.task,
                "context": case.context,
                "grader": case.grader,
            }
            for case in suite.cases
        ],
    }
    payload = {
        "schema": _PACKAGE,
        "suite": {
            "schema_version": 1,
            "id": suite.id,
            "cases": [vars_case(case) for case in suite.cases],
        },
        "provenance": provenance,
        "controls": {
            "schema": controls["schema"],
            "cases": [indexed[key] for key in sorted(indexed)],
        },
        "review_input": inputs,
    }
    encoded = canonical_json(payload)
    # Detach caller-owned dictionaries so later edits cannot mutate this freeze.
    frozen = json.loads(encoded)
    result = {**frozen, "package_sha256": content_hash(frozen)}
    # Include the digest envelope and CLI's newline in the readable file bound.
    _require(
        len(canonical_json(result).encode()) + 1 <= _MAX_PACKAGE_BYTES,
        "Candidate package exceeds its bound",
    )
    return result


def candidate_package(document: object) -> tuple[RoleSuite, dict[str, Any]]:
    """Rebuild the complete freeze, including model-visible bytes and controls."""
    _require(
        isinstance(document, dict)
        and set(document)
        == {"schema", "suite", "provenance", "controls", "review_input", "package_sha256"}
        and document["schema"] == _PACKAGE
        and isinstance(document["suite"], dict)
        and type(document["suite"].get("schema_version")) is int,
        "Invalid candidate package",
    )
    assert isinstance(document, dict)
    suite = RoleSuite.from_dict(document["suite"])
    rebuilt = prepare_candidate_package(suite, document["provenance"], document["controls"])
    _require(document == rebuilt, "Candidate package differs from its frozen contents")
    return suite, rebuilt


def gold_review_request(document: object) -> dict[str, Any]:
    """Export tasks for independent answers, omitting the author's proposed gold."""
    _, package = candidate_package(document)
    inputs = package["review_input"]
    return {**inputs, "input_manifest_sha256": content_hash(inputs)}


def audit_candidate_review(document: object, review: object | None = None) -> dict[str, Any]:
    """Expose missing, uncertain and conflicting answers; authenticate no author."""
    suite, package = candidate_package(document)
    request = gold_review_request(package)
    cases = {case.id: case for case in suite.cases}
    reviewed: dict[str, str] = {}
    reviewer_digest = review_digest = None
    if review is not None:
        _require(
            isinstance(review, dict)
            and set(review)
            == {"schema", "input_manifest_sha256", "reviewer_id", "source", "answers"}
            and review["schema"] == _REVIEW
            and review["input_manifest_sha256"] == request["input_manifest_sha256"]
            and isinstance(review["reviewer_id"], str)
            and _ID.fullmatch(review["reviewer_id"]) is not None
            and review["reviewer_id"].casefold() != package["provenance"]["author_id"].casefold()
            and _text(review["source"], 512)
            and isinstance(review["answers"], list)
            and len(review["answers"]) <= len(cases),
            "Review needs matching inputs and a distinct declared reviewer identity",
        )
        assert isinstance(review, dict)
        for answer in review["answers"]:
            _require(
                isinstance(answer, dict)
                and set(answer) == {"case_id", "verdict", "expected", "rationale"}
                and isinstance(answer["case_id"], str)
                and answer["case_id"] in cases
                and answer["case_id"] not in reviewed
                and answer["verdict"] in ("resolved", "uncertain")
                and _text(answer["rationale"], 2048),
                "Review answers must be unique, bounded and inside the frozen case set",
            )
            case = cases[answer["case_id"]]
            if answer["verdict"] == "uncertain":
                _require(answer["expected"] is None, "Uncertain reviews cannot supply a gold label")
                reviewed[case.id] = "uncertain"
                continue
            # Use the existing rubric's own schema and type requirements.
            independently_supplied = replace(case, expected=answer["expected"])
            reviewed[case.id] = (
                "agrees"
                if canonical_json(independently_supplied.expected) == canonical_json(case.expected)
                else "conflicts"
            )
        reviewer_digest = content_hash({"declared_reviewer": review["reviewer_id"]})
        review_digest = content_hash(review)
    missing = sorted(set(cases) - set(reviewed))
    uncertain = sorted(key for key, status in reviewed.items() if status == "uncertain")
    conflicts = sorted(key for key, status in reviewed.items() if status == "conflicts")
    coverage = Counter((case.context or {})["atomic_kind"] for case in suite.cases)
    return {
        "schema": "potbot.atomic-candidate-review-audit/1",
        "package_sha256": package["package_sha256"],
        "suite_sha256": suite.identity,
        "input_manifest_sha256": request["input_manifest_sha256"],
        "review_sha256": review_digest,
        "declared_reviewer_sha256": reviewer_digest,
        "status": "conflicting_gold"
        if conflicts
        else "pending_review"
        if missing or uncertain
        else "declared_agreement",
        "case_count": len(cases),
        "coverage": dict(sorted(coverage.items())),
        "uncovered_atomic_kinds": sorted({kind.name for kind in ATOMIC_KINDS} - set(coverage)),
        "missing_case_ids": missing,
        "uncertain_case_ids": uncertain,
        "conflicting_case_ids": conflicts,
        "reviewer_identity_authenticated": False,
        "independently_reviewed": False,
        "secret_holdout": False,
        "qualified": False,
        "automatic_routing_changes": 0,
    }
