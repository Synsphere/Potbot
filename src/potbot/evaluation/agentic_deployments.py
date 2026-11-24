"""Bind benchmark declarations to existing offline local-deployment checks.

These caller-local checks cannot attest the weights, template or runtime loaded
by an independent loopback server. They do not load models or grant dispatch.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.evaluation.agentic_model_bench import ModelTarget, RoleSuite
from potbot.models.preflight import (
    HostObservation,
    LocalHostProfile,
    preflight_local,
)


def inspect_benchmark_deployments(
    document: object,
    models: list[ModelTarget],
    suite: RoleSuite,
    *,
    project_root: Path,
    observe_host: Callable[[LocalHostProfile], HostObservation] | None = None,
) -> dict[str, Any]:
    """Check every local target against one exact existing host profile."""
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "bindings"}
        or document["schema"] != "potbot.benchmark-deployment-bindings/1"
        or not isinstance(document["bindings"], list)
        or not 1 <= len(document["bindings"]) <= 16
        or len({model.id for model in models}) != len(models)
    ):
        raise InvalidInputError("invalid benchmark deployment bindings")
    targets = {model.id: model for model in models if model.backend == "loopback_openai"}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for binding in document["bindings"]:
        if (
            not isinstance(binding, dict)
            or set(binding) != {"model_id", "host_profile"}
            or not isinstance(binding["model_id"], str)
            or binding["model_id"] not in targets
            or binding["model_id"] in seen
        ):
            raise InvalidInputError("bind each configured local target exactly once")
        target = targets[binding["model_id"]]
        seen.add(target.id)
        profile = LocalHostProfile.from_dict(binding["host_profile"])
        deployment = profile.deployment
        required_roles = {
            "coding" if case.role == "coding" else "reasoning"
            for case in suite.cases
            if case.role in target.roles
        }
        if (
            target.model != deployment.canonical_model
            or target.revision != profile.model_revision
            or target.max_output_tokens > deployment.max_output_tokens
            or not required_roles <= set(deployment.roles)
        ):
            raise InvalidInputError("benchmark target differs from its exact deployment")
        report = preflight_local(
            profile,
            project_root=project_root,
            observation=observe_host(profile) if observe_host else None,
        )
        rows.append(
            {
                "model_id": target.id,
                "host_profile_sha256": profile.fingerprint,
                "deployment_sha256": deployment.fingerprint,
                "deployment": deployment.to_dict(),
                "model_revision": profile.model_revision,
                "preflight": report.to_dict(),
            }
        )
    if seen != set(targets):
        raise InvalidInputError("deployment bindings must cover every configured local target")
    return {
        "schema": "potbot.benchmark-deployment-inspection/1",
        "bindings_sha256": content_hash(document),
        "status": "blocked"
        if any(row["preflight"]["status"] == "blocked" for row in rows)
        else "ready_for_load_check",
        "targets": sorted(rows, key=lambda row: row["model_id"]),
        "server_loaded_identity_authenticated": False,
        "licence_acceptance_authenticated": False,
        "loading_tested": False,
        "generation_tested": False,
        "qualified": False,
    }
