"""Owner-only consumption of an original checked model job; no inference or replay.

The native typed-artifact reader is a contract boundary for a future qualified
model Cog. It does not install an engine, attest weights, settle token budgets or
make the legacy reasoning callback a production native transport.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from potbot.controller.model_route_bindings import (
    compilation_digest,
    compile_route_request,
    prompt_recipe,
)
from potbot.controller.reasoning_result_contracts import validate_reasoning_output
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.models.native_results import (
    MAX_RESULT_BYTES,
    REQUEST,
    RESULT,
    parse_native_result,
    validate_native_request,
)
from potbot.models.providers.contracts import ProviderUsage
from potbot.models.registry import DeploymentSpec
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database
from potbot.workspace_sandbox import load_workspace_sandbox


def deployment_from_content(content: dict[str, Any]) -> DeploymentSpec:
    try:
        kwargs = {
            k: v
            for k, v in content.items()
            if k not in {"resource_profile", "prompt_versions", "roles", "response_schemas"}
        }
        spec = DeploymentSpec(
            **kwargs,
            resource_profile_json=canonical_json(content["resource_profile"]),
            prompt_versions=tuple(content["prompt_versions"].items()),
            roles=tuple(content["roles"]),
            response_schemas=tuple(content["response_schemas"]),
        )
        if spec.to_dict() != content:
            raise ValueError
        return spec
    except (TypeError, KeyError, ValueError, AttributeError):
        raise InvalidInputError("Stored deployment contract is invalid") from None


def native_reasoning_deployment(
    conn: Any, actor: WorkspaceContext, native_input: dict[str, Any], recipe: dict[str, Any]
) -> DeploymentSpec:
    if native_input["type_ref"] != REQUEST or not isinstance(native_input["value"], dict):
        raise StateMismatchError("Native job has no declared model request port")
    value = native_input["value"]
    selected = conn.execute(
        "SELECT content,content_hash FROM model_deployments WHERE workspace_id=%s AND content_hash=%s",
        (actor.workspace_id, value.get("deployment_sha256")),
    ).fetchone()
    if selected is None:
        raise ScopeDeniedError("Original native deployment is unavailable")
    spec = deployment_from_content(selected["content"])
    version = (
        "model-aware-reasoning-json-object-v1"
        if recipe["output_contract"]
        else "model-aware-reasoning-v1"
    )
    if (
        spec.fingerprint != selected["content_hash"]
        or "reasoning" not in spec.roles
        or spec.output_adapter != "potbot.native-model-result/1"
        or RESULT not in spec.response_schemas
        or dict(spec.prompt_versions).get(recipe["role"]) != version
    ):
        raise StateMismatchError("Native deployment or declared reasoning prompt differs")
    validate_native_request(value, spec)
    return spec


class ModelReasoningResultReader:
    def __init__(self, database: Database, artifacts: ArtifactStore | None = None) -> None:
        if artifacts is not None and artifacts.db is not database:
            raise ScopeDeniedError("Model artifact reader belongs to a different database")
        self.database, self.artifacts = database, artifacts

    def read(self, actor: WorkspaceContext, decision_id: UUID) -> dict[str, Any]:
        if not isinstance(decision_id, UUID):
            raise InvalidInputError("Model result requires its original decision UUID")
        with self.database.transaction(actor) as conn:
            d = conn.execute(
                "SELECT * FROM model_route_decisions WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (actor.workspace_id, decision_id, actor.principal_id),
            ).fetchone()
            if d is None:
                raise ScopeDeniedError("Owned model result is unavailable")
            recipe = conn.execute(
                "SELECT * FROM model_route_prompt_recipes WHERE workspace_id=%s AND policy_id=%s",
                (actor.workspace_id, d["policy_id"]),
            ).fetchone()
            compiled = conn.execute(
                "SELECT * FROM model_route_compiled_prompts WHERE workspace_id=%s AND decision_id=%s",
                (actor.workspace_id, d["id"]),
            ).fetchone()
            if recipe is None or compiled is None:
                raise StateMismatchError("Reasoning consumption requires frozen compiled input")
            exact, prompt_hash = compile_route_request(
                recipe["recipe"], compiled["facts"], d["request"]
            )
            if (
                compiled["prompt_sha256"] != prompt_hash
                or compiled["recipe_sha256"] != content_hash(prompt_recipe(recipe["recipe"]))
                or compiled["binding_sha256"] != compilation_digest(d, compiled)
            ):
                raise StateMismatchError("Retained model prompt provenance differs")
            job = conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=%s AND id=%s AND owner_principal_id=%s",
                (actor.workspace_id, d["job_id"], actor.principal_id),
            ).fetchone()
            if (
                job is None
                or job["state"] != "succeeded"
                or job["dispatched_at"] is None
                or job["finished_at"] is None
                or not isinstance(job["result"], dict)
            ):
                raise StateMismatchError("Original model job is not confirmed complete")
            if (
                job["operation_check_id"] != d["check_job_id"]
                or job["job_type"] != exact["job_type"]
                or job["input"] != exact["input"]
                or job["selected_models"] != exact["selected_models"]
                or job["episode_id"] != d["episode_id"]
                or job["contract_revision_id"] != d["contract_revision_id"]
            ):
                raise StateMismatchError("Model result belongs to a different checked request")
            if job["job_type"] == "model.generate":
                observations = conn.execute(
                    "SELECT h.id,i.status,i.result FROM hosted_requests h JOIN hosted_invocations i ON i.workspace_id=h.workspace_id AND i.id=h.id WHERE h.workspace_id=%s AND h.job_id=%s ORDER BY h.ordinal",
                    (actor.workspace_id, job["id"]),
                ).fetchall()
                if (
                    len(observations) != 1
                    or observations[0]["status"] != "completed"
                    or not isinstance(observations[0]["result"], dict)
                ):
                    raise StateMismatchError("Original provider completion is unavailable")
                observed = observations[0]["result"]
                selection = exact["selected_models"][0]
                if (
                    observed.get("finish_reason") != "stop"
                    or observed.get("model") != selection["model"]
                    or observed.get("provider") != selection["provider"]
                    or observed.get("role") != selection["role"]
                    or job["result"].get("invocation_ids") != [str(observations[0]["id"])]
                ):
                    raise StateMismatchError("Original provider result is incomplete or differs")
                text = conn.execute(
                    "SELECT COALESCE(string_agg(data->>'text','' ORDER BY sequence),'') AS text FROM job_events WHERE workspace_id=%s AND job_id=%s AND event_type='job.output'",
                    (actor.workspace_id, job["id"]),
                ).fetchone()["text"]
                try:
                    usage = ProviderUsage(**observed["usage"]).to_dict()
                except (TypeError, KeyError):
                    raise InvalidInputError("Original provider usage is invalid") from None
                if usage["output_tokens"] > exact["input"]["max_output_tokens"]:
                    raise StateMismatchError("Original model usage exceeded its checked bound")
                finish = observed["finish_reason"]
                native = None
            else:
                spec = native_reasoning_deployment(conn, actor, exact["input"], recipe["recipe"])
                if job["result"].get("operation_id") != str(job["id"]):
                    raise StateMismatchError(
                        "Native artifact publication belongs to a different operation"
                    )
                try:
                    artifact_id = UUID(job["result"]["artifact_id"])
                except (KeyError, TypeError, ValueError):
                    raise StateMismatchError(
                        "Original native model artifact is unavailable"
                    ) from None
                native = (spec, artifact_id)
                text, usage, finish = "", None, "unknown"
        if native is not None:
            spec, artifact_id = native
            artifacts = self.artifacts
            if artifacts is None:
                sandbox = load_workspace_sandbox(self.database, actor)
                artifacts = ArtifactStore(
                    self.database, LocalArtifactBackend(sandbox.path("artifacts"))
                )
            metadata = artifacts.metadata(actor, artifact_id)
            if (
                metadata["byte_length"] > MAX_RESULT_BYTES
                or metadata["media_type"] != "application/json"
            ):
                raise StateMismatchError("Native model artifact exceeds its typed bound")
            raw = artifacts.read(
                actor, artifact_id
            )  # Existing backend verifies the original bytes.
            parsed = parse_native_result(
                raw, request=exact["input"]["value"], job_id=job["id"], deployment=spec
            )
            text, usage, finish = parsed["text"], parsed["usage"], parsed["finish_reason"]
            if finish != "stop":
                raise StateMismatchError("Native model completion was interrupted")
        parsed_role = validate_reasoning_output(
            recipe["recipe"]["role"], text, compiled["facts"]["context"]
        )
        return {
            "schema": "potbot.checked-reasoning-result/1",
            "decision_id": str(d["id"]),
            "job_id": str(job["id"]),
            "model": recipe["recipe"]["model"],
            "role": recipe["recipe"]["role"],
            "prompt_sha256": prompt_hash,
            "text": text,
            "output_sha256": content_hash(text),
            "usage": usage,
            "usage_origin": "worker_reported" if native is not None else "provider_reported",
            "finish_reason": finish,
            "parsed": parsed_role,
            "role_contract_valid": True,
            "semantic_correctness": "not_evaluated",
            "runtime_qualified": False,
            "replayed": False,
        }
