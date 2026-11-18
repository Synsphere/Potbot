"""Exact immutable provenance for selected work on the existing jobs queue."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.operation_workbench import canonical_uuid, operation_id, parse_request
from potbot.domain.result_assertions import bounded_value, validate_result_oracle
from potbot.domain.serialization import content_hash

SCHEMA = "potbot-task-execution/v1"
BINDING_SCHEMA = "potbot-task-execution-binding/v1"
ORACLE_SCHEMA = "potbot-task-result-oracle/v1"
MAX_BINDING_BYTES = 131_072
_FIELDS = {
    "schema",
    "workspaceId",
    "principalId",
    "selectionId",
    "selectionSha256",
    "searchId",
    "resultId",
    "candidateId",
    "operationId",
    "ownerSha256",
    "consumerSha256",
    "taskBinding",
    "capabilities",
    "request",
    "requestSha256",
    "acceptance",
    "acceptanceSha256",
    "sha256",
}
_TASK_FIELDS = {
    "episodeId",
    "contractRevisionId",
    "contractSha256",
    "repositoryStateId",
    "repositorySha256",
    "semanticReleaseId",
    "semanticReleaseSha256",
}
_CAPABILITIES = {"classes", "inputTypes", "outputTypes", "effects", "scopes", "platforms"}


def execution_consumer_sha256() -> str:
    """Bind interpretation, admission and evidence to this installed consumer."""
    root = Path(__file__).resolve().parents[1]
    modules = (
        "domain/task_execution.py",
        "domain/operation_workbench.py",
        "domain/jobs.py",
        "domain/result_assertions.py",
        "controller/task_execution.py",
        "controller/task_execution_outcomes.py",
        "controller/operation_workbench.py",
        "controller/operation_checks.py",
        "controller/native_cog_dispatch.py",
        "controller/jobs.py",
        "controller/job_runner.py",
        "controller/operation_discovery_metadata.py",
        "models/hosted_gateway.py",
        "models/hosted_policy.py",
        "persistence/artifacts.py",
        "semantics/catalog.py",
        "tools/execution_contracts.py",
        "tools/sandbox_cog_worker.py",
    )
    pins = {}
    for relative in modules:
        path = root / relative
        if path.resolve(strict=True) != path or not path.is_file():
            raise StateMismatchError("Task execution consumer source is redirected")
        before, raw = path.stat(), path.read_bytes()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise StateMismatchError("Task execution consumer changed during readback")
        pins[relative] = hashlib.sha256(raw).hexdigest()
    return content_hash({"schema": "potbot-task-execution-consumer/v1", "sources": pins})


def parse_binding(value: object) -> dict[str, Any]:
    binding = bounded_value(value, maximum=MAX_BINDING_BYTES)
    if (
        not isinstance(binding, dict)
        or set(binding) != _FIELDS
        or binding["schema"] != BINDING_SCHEMA
    ):
        raise InvalidInputError("Task execution binding has an invalid envelope")
    for key in ("workspaceId", "principalId", "selectionId", "searchId", "resultId"):
        canonical_uuid(binding[key])
    for key in (
        "selectionSha256",
        "candidateId",
        "operationId",
        "ownerSha256",
        "consumerSha256",
        "requestSha256",
        "acceptanceSha256",
        "sha256",
    ):
        operation_id(binding[key])
    task = binding["taskBinding"]
    if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
        raise InvalidInputError("Task execution requires the exact original task binding")
    for key in ("episodeId", "contractRevisionId", "repositoryStateId", "semanticReleaseId"):
        canonical_uuid(task[key])
    for key in ("contractSha256", "repositorySha256", "semanticReleaseSha256"):
        operation_id(task[key])
    capabilities = binding["capabilities"]
    if (
        not isinstance(capabilities, dict)
        or set(capabilities) != _CAPABILITIES
        or any(
            not isinstance(items, list)
            or len(items) > 64
            or any(not isinstance(item, str) or not 1 <= len(item) <= 512 for item in items)
            or len(items) != len(set(items))
            for items in capabilities.values()
        )
    ):
        raise InvalidInputError("Task execution capabilities have an invalid shape")
    request = parse_request(binding["request"]).to_dict()
    acceptance = validate_result_oracle(binding["acceptance"], schema=ORACLE_SCHEMA)
    if (
        request != binding["request"]
        or request["episode_id"] != task["episodeId"]
        or content_hash(request) != binding["requestSha256"]
        or content_hash(acceptance) != binding["acceptanceSha256"]
        or content_hash({key: item for key, item in binding.items() if key != "sha256"})
        != binding["sha256"]
    ):
        raise StateMismatchError("Task execution binding integrity failed")
    return dict(binding)
