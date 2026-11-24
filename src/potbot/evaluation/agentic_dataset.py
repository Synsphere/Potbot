"""Integrity audit for public atomic fixtures, separate from trusted gold review.

A dataset card declares provenance; it cannot authenticate a reviewer or make
published fixtures secret. Routing and held-out admission remain separate gates.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from potbot.controller.atomic_tasks import ATOMIC_KINDS
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.evaluation.agentic_model_bench import RoleSuite


def audit_atomic_dataset(suite: RoleSuite, card: object) -> dict[str, Any]:
    required = {"schema", "suite_sha256", "author", "licence", "source", "exposure", "gold_review"}
    if (
        not isinstance(card, dict)
        or set(card) != required
        or card["schema"] != "potbot.atomic-dataset-card/1"
        or card["suite_sha256"] != suite.identity
        or card["exposure"] != "public_synthetic_fixture"
        or card["gold_review"] != "pending_independent_review"
    ):
        raise InvalidInputError("atomic dataset needs its exact public, unreviewed fixture card")
    for name in ("author", "licence", "source"):
        if not isinstance(card[name], str) or not 1 <= len(card[name]) <= 512:
            raise InvalidInputError("dataset provenance requires bounded nonempty text")
    known = {kind.name for kind in ATOMIC_KINDS}
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"development": 0, "holdout": 0})
    seen: set[str] = set()
    for case in suite.cases:
        context = case.context or {}
        kind = context.get("atomic_kind")
        if not isinstance(kind, str) or kind not in known:
            raise InvalidInputError("dataset case lacks an existing atomic kind")
        if case.grader == "json_shape":
            raise InvalidInputError("atomic fixture references need substantive expected values")
        # Reference inputs cannot gain independence through rewording or split IDs.
        identity = (
            content_hash({"atomic_kind": kind, "input": context["reference_input"]})
            if "reference_input" in context
            else case.input_digest
        )
        if identity in seen:
            raise InvalidInputError(
                "duplicate dataset input, including development/holdout leakage"
            )
        seen.add(identity)
        counts[kind][case.split] += 1
    return {
        "schema": "potbot.atomic-dataset-audit/1",
        "suite_sha256": suite.identity,
        "card_sha256": content_hash(card),
        "case_count": len(suite.cases),
        "coverage": dict(sorted(counts.items())),
        "uncovered_atomic_kinds": sorted(known - set(counts)),
        "provenance": {name: card[name] for name in ("author", "licence", "source")},
        "exposure": card["exposure"],
        "gold_review": card["gold_review"],
        "independently_reviewed": False,
        "secret_holdout": False,
        "qualified": False,
        "automatic_routing_changes": 0,
        "next_gate": "independent gold review and an independently collected, protected holdout",
    }
