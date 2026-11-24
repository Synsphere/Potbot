"""Bind compiled prompts and observed admission identity to the existing worker.

This is not weight/server attestation, qualification, inference or permission.
Hosted identity covers the operator policy; native identity covers the worker
installation and configured model projection. All ordinary admission remains.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.model_routing import bind_route_request, route_target
from potbot.domain.operation_workbench import canonical_uuid, operation_id, parse_request
from potbot.domain.serialization import canonical_json, content_hash
from potbot.models.prompt_compiler import compile_reasoning_prompt
from potbot.models.reasoning_schema import ROLE_SCHEMA_VARIANT, ReasoningOutputSpec


def _structured_target(recipe: dict[str, Any], executor: dict[str, Any]) -> None:
    if recipe["variant"] == ROLE_SCHEMA_VARIANT and (
        executor["job_type"] != "model.generate"
        or [model["provider"] for model in executor["selected_models"]] != ["openai"]
    ):
        raise ScopeDeniedError("Structured reasoning requires one OpenAI model job")


def _structured_output(recipe: dict[str, Any], facts: object) -> ReasoningOutputSpec | None:
    if recipe["variant"] != ROLE_SCHEMA_VARIANT:
        return None
    if recipe["output_contract"] != "json_object_v1" or not isinstance(facts, dict):
        raise StateMismatchError("The structured reasoning prompt has no frozen facts or contract")
    return ReasoningOutputSpec.from_context(recipe["role"], facts.get("context"))


def checked_output_spec(
    database: Any,
    actor: WorkspaceContext,
    *,
    decision_id: str | UUID | None = None,
    job_id: UUID | None = None,
) -> ReasoningOutputSpec | None:
    """Resolve format from immutable owned route records, never job input."""
    if (decision_id is None) == (job_id is None):
        raise InvalidInputError("Structured output lookup needs one decision or job")
    decision = canonical_uuid(str(decision_id)) if decision_id is not None else None
    with database.transaction(actor) as conn:
        row = conn.execute(
            "SELECT r.recipe,f.facts,d.request FROM model_route_decisions d "
            "JOIN model_route_prompt_recipes r ON r.workspace_id=d.workspace_id "
            "AND r.policy_id=d.policy_id AND r.principal_id=d.principal_id "
            "LEFT JOIN model_route_compiled_prompts f ON f.workspace_id=d.workspace_id "
            "AND f.decision_id=d.id AND f.principal_id=d.principal_id "
            "WHERE d.workspace_id=%s AND d.principal_id=%s AND (d.id=%s OR d.job_id=%s)",
            (actor.workspace_id, actor.principal_id, decision, job_id),
        ).fetchone()
    if row is None:
        return None
    _structured_target(row["recipe"], row["request"])
    return _structured_output(row["recipe"], row["facts"])


def prompt_recipe(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "model", "role", "variant", "output_contract"}
        or value.get("schema") != "potbot-route-prompt-recipe/v1"
    ):
        raise InvalidInputError("Route prompt recipe fields differ")
    if any(not isinstance(value[k], str) for k in ("model", "role", "variant")) or (
        value["output_contract"] is not None and not isinstance(value["output_contract"], str)
    ):
        raise InvalidInputError("Route prompt recipe identities must be text")
    if value["output_contract"] not in (None, "json_object_v1"):
        raise InvalidInputError("Unsupported route prompt output contract")
    # Validate via the same compiler, including role/output-contract compatibility.
    compile_reasoning_prompt(
        model=value["model"],
        role=value["role"],
        variant=value["variant"],
        output_contract="json_object_v1" if value["output_contract"] is not None else None,
        task="Recipe validation only",
    )
    return dict(json.loads(canonical_json(value)))


def compile_route_request(
    recipe: object, facts: object, request: object, *, fill: bool = False
) -> tuple[dict[str, Any], str]:
    selected = prompt_recipe(recipe)
    if (
        not isinstance(facts, dict)
        or set(facts) != {"task", "evidence", "constraints", "context"}
        or not isinstance(facts["evidence"], list)
        or not isinstance(facts["constraints"], list)
    ):
        raise InvalidInputError("Route prompt facts require the exact bounded packet")
    try:
        encoded_facts = canonical_json(facts)
        if len(encoded_facts.encode()) > 16384:
            raise InvalidInputError("Route prompt facts exceed their byte bound")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise InvalidInputError("Route prompt facts must be bounded JSON") from None
    packet_facts: dict[str, Any] = json.loads(encoded_facts)
    packet = compile_reasoning_prompt(
        model=selected["model"],
        role=selected["role"],
        variant=selected["variant"],
        output_contract=selected["output_contract"],
        task=packet_facts["task"],
        evidence=tuple(packet_facts["evidence"]),
        constraints=tuple(packet_facts["constraints"]),
        context=packet_facts["context"],
    )
    spec = parse_request(request)
    result = spec.to_dict()
    _structured_target(selected, result)
    if spec.job_type == "model.generate":
        if len(spec.selected_models) != 1 or spec.selected_models[0]["model"] != selected["model"]:
            raise ScopeDeniedError("Compiled prompt model differs from the selected executor")
        payload = result["input"]
    else:
        payload = result["input"]["value"]
        if not isinstance(payload, dict) or payload.get("model") != selected["model"]:
            raise ScopeDeniedError("Native inference must pin its explicit model field")
    expected = {"prompt": packet.user, "system": packet.system}
    if fill:
        if set(payload) & set(expected):
            raise InvalidInputError("Prompt compilation requires an unfilled request")
        payload.update(expected)
    elif any(payload.get(k) != v for k, v in expected.items()):
        raise StateMismatchError("Execution request differs from its compiled prompt")
    return parse_request(result).to_dict(), packet.identity


def worker_admission_identity(target: object, bindings: object) -> str:
    selected = route_target(target)
    if not isinstance(bindings, dict):
        raise InvalidInputError("Worker admission bindings are unavailable")
    native = selected["executor"]["job_type"] == "cog.execute"
    keys = (
        {"profileSha256", "installationSha256", "runtimeSha256", "nativeSha256"}
        if native
        else {"policySha256"}
    )
    if native and "releaseSha256" in bindings:
        keys.add("releaseSha256")
    observed = {k: operation_id(bindings.get(k)) for k in sorted(keys)}
    if native and observed["profileSha256"] != selected["executor"]["input"]["profile_sha256"]:
        raise StateMismatchError("Observed native profile differs from the selected executor")
    return content_hash(
        {
            "schema": "potbot-route-worker-admission/v1",
            "executor": selected["executor"],
            "observed": observed,
        }
    )


def configure_recipe(
    database: Any, actor: WorkspaceContext, policy_id: UUID, recipe: object
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    selected = prompt_recipe(recipe)
    digest = content_hash(selected)
    with database.transaction(actor) as conn:
        conn.execute("SELECT model_route_lock(%s,%s)", (actor.workspace_id, policy_id))
        policy = conn.execute(
            "SELECT * FROM model_route_policies WHERE workspace_id=%s AND id=%s AND principal_id=%s",
            (actor.workspace_id, policy_id, actor.principal_id),
        ).fetchone()
        if policy is None:
            raise ScopeDeniedError("Owned route is unavailable")
        if policy["manifest"]["prompt_contract_sha256"] != digest:
            raise StateMismatchError("Prompt recipe differs from the frozen route contract")
        target = policy["manifest"]["baseline"]["executor"]
        _structured_target(selected, target)
        model = (
            target["selected_models"][0]["model"]
            if target["job_type"] == "model.generate"
            else target["input"]["value"].get("model")
        )
        if model != selected["model"]:
            raise StateMismatchError("Prompt recipe selects another model")
        existing = conn.execute(
            "SELECT recipe FROM model_route_prompt_recipes WHERE workspace_id=%s AND policy_id=%s",
            (actor.workspace_id, policy_id),
        ).fetchone()
        if existing is not None:
            if existing["recipe"] != selected:
                raise StateMismatchError("Prompt recipe cannot be replaced")
        else:
            conn.execute(
                "INSERT INTO model_route_prompt_recipes(workspace_id,policy_id,principal_id,recipe,recipe_sha256) VALUES(%s,%s,%s,%s,%s)",
                (actor.workspace_id, policy_id, actor.principal_id, Jsonb(selected), digest),
            )
    return {"recipe_sha256": digest, "recipe": selected}


def prepare_compilation(
    conn: Any, actor: WorkspaceContext, policy: dict[str, Any], request: object, facts: object
) -> dict[str, Any] | None:
    recipe = conn.execute(
        "SELECT * FROM model_route_prompt_recipes WHERE workspace_id=%s AND policy_id=%s",
        (actor.workspace_id, policy["id"]),
    ).fetchone()
    if recipe is None:
        if facts is not None:
            raise StateMismatchError("The route has no frozen prompt recipe")
        return None
    exact, identity = compile_route_request(recipe["recipe"], facts, request)
    bind_route_request(policy["manifest"]["baseline"], exact)
    _structured_output(recipe["recipe"], facts)
    return {
        "facts": json.loads(canonical_json(facts)),
        "prompt_sha256": identity,
        "recipe_sha256": recipe["recipe_sha256"],
    }


def compilation_digest(decision: dict[str, Any], compilation: dict[str, Any]) -> str:
    return content_hash(
        {
            "schema": "potbot-route-compilation-binding/v1",
            "policy_id": str(decision["policy_id"]),
            "decision_id": str(decision["id"]),
            "epoch": decision["epoch"],
            "review_id": str(decision["review_id"]),
            "request_sha256": content_hash(decision["request"]),
            "facts_sha256": content_hash(compilation["facts"]),
            "prompt_sha256": compilation["prompt_sha256"],
            "recipe_sha256": compilation["recipe_sha256"],
        }
    )


def persist_compilation(
    conn: Any, actor: WorkspaceContext, decision: dict[str, Any], compilation: dict[str, Any] | None
) -> None:
    if compilation is None:
        return
    from psycopg.types.json import Jsonb

    digest = compilation_digest(decision, compilation)
    existing = conn.execute(
        "SELECT binding_sha256 FROM model_route_compiled_prompts WHERE workspace_id=%s AND decision_id=%s",
        (actor.workspace_id, decision["id"]),
    ).fetchone()
    if existing is not None:
        if existing["binding_sha256"] != digest:
            raise StateMismatchError("Allocation cannot replace its compiled input provenance")
        return
    conn.execute(
        "INSERT INTO model_route_compiled_prompts(workspace_id,decision_id,principal_id,facts,prompt_sha256,recipe_sha256,binding_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (
            actor.workspace_id,
            decision["id"],
            actor.principal_id,
            Jsonb(compilation["facts"]),
            compilation["prompt_sha256"],
            compilation["recipe_sha256"],
            digest,
        ),
    )


def checked_route_binding(
    database: Any,
    actor: WorkspaceContext,
    decision_id: str,
    request: object,
    observed: dict[str, Any],
) -> dict[str, str]:
    # This helper only reads route provenance; a route veto is not lease loss.
    # Report ineligibility so the existing worker can close an unsent job.
    try:
        return _checked_route_binding(database, actor, decision_id, request, observed)
    except RevisionConflictError:
        raise StateMismatchError("Route decision is stale or independently fenced") from None


def _checked_route_binding(
    database: Any,
    actor: WorkspaceContext,
    decision_id: str,
    request: object,
    observed: dict[str, Any],
) -> dict[str, str]:
    with database.transaction(actor) as conn:
        decision = conn.execute(
            "SELECT * FROM model_route_decisions WHERE workspace_id=%s AND id=%s AND principal_id=%s",
            (actor.workspace_id, canonical_uuid(decision_id), actor.principal_id),
        ).fetchone()
        if decision is None:
            raise ScopeDeniedError("Owned routing decision is unavailable")
        policy = conn.execute(
            "SELECT * FROM model_route_policies WHERE workspace_id=%s AND id=%s",
            (actor.workspace_id, decision["policy_id"]),
        ).fetchone()
        recipe = conn.execute(
            "SELECT * FROM model_route_prompt_recipes WHERE workspace_id=%s AND policy_id=%s",
            (actor.workspace_id, decision["policy_id"]),
        ).fetchone()
        conn.execute(
            "SELECT model_route_decision_current(d) FROM model_route_decisions d WHERE workspace_id=%s AND id=%s",
            (actor.workspace_id, decision["id"]),
        )
        if recipe is None:
            return {}  # No retroactive compiled-prompt claim for historical routes.
        compiled = conn.execute(
            "SELECT * FROM model_route_compiled_prompts WHERE workspace_id=%s AND decision_id=%s",
            (actor.workspace_id, decision["id"]),
        ).fetchone()
        if (
            policy is None
            or compiled is None
            or decision["request"] != parse_request(request).to_dict()
        ):
            raise StateMismatchError("Compiled routing provenance is missing or changed")
        exact, prompt_sha = compile_route_request(recipe["recipe"], compiled["facts"], request)
        target = policy["manifest"]["baseline"]
        bind_route_request(target, exact)
        if (
            compiled["recipe_sha256"] != content_hash(prompt_recipe(recipe["recipe"]))
            or compiled["prompt_sha256"] != prompt_sha
            or compiled["binding_sha256"] != compilation_digest(decision, compiled)
        ):
            raise StateMismatchError("Compiled route identity differs")
        if exact["job_type"] == "cog.execute":
            from potbot.controller.model_reasoning_results import native_reasoning_deployment

            native_reasoning_deployment(conn, actor, exact["input"], recipe["recipe"])
        runtime = worker_admission_identity(target, observed)
        if runtime != target["runtime_identity_sha256"]:
            raise StateMismatchError("Observed worker admission differs from the accepted route")
        result = {
            "modelRoutingSha256": compiled["binding_sha256"],
            "modelPromptSha256": prompt_sha,
            "modelRuntimeSha256": runtime,
        }
        output = _structured_output(recipe["recipe"], compiled["facts"])
        if output is not None:
            result["modelOutputSchemaSha256"] = output.fingerprint
        return result
