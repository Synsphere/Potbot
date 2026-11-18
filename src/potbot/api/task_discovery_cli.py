"""Review task shortlists and explicitly refine them with an offline local model."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import _integer, add_scope_arguments

_MUTATIONS = frozenset({"create", "select", "cancel", "refine"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="potbot task-discovery", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    context = commands.add_parser("context", help="read the exact current task discovery binding")
    context.add_argument("--episode", type=UUID, required=True)
    context.add_argument("--state", type=UUID, required=True)
    searches = commands.add_parser("searches", help="read retained private searches")
    searches.add_argument("--episode", type=UUID)
    searches.add_argument("--limit", type=_integer(1, 25), default=25)
    searches.add_argument("--after")
    for name in ("search", "result", "selection"):
        commands.add_parser(name).add_argument("identity", type=UUID)
    inspect = commands.add_parser(
        "inspect", help="inspect one candidate through its existing owner"
    )
    inspect.add_argument("identity", type=UUID, help="retained search identity")
    inspect.add_argument("candidate", help="exact included candidate SHA-256")
    create = commands.add_parser("create", help="retain an exact reviewed discovery request")
    create.add_argument(
        "--input", type=Path, required=True, help="exact JSON request, at most 8 KiB"
    )
    for name in ("cancel", "select", "refine"):
        command = commands.add_parser(name)
        command.add_argument("identity", type=UUID)
        command.add_argument("--revision", type=_integer(1, 2**53 - 2), required=True)
        command.add_argument("--digest", required=True)
        if name == "select":
            command.add_argument("--candidate", required=True)
        if name == "refine":
            command.add_argument(
                "--model-dir", type=Path, required=True, help="existing pinned offline Qwen model"
            )
            command.add_argument("--artifacts-root", type=Path)
    for name, command in commands.choices.items():
        if name in _MUTATIONS:
            command.add_argument(
                "--key",
                required=True,
                help="reuse the exact key and body to recover a lost response",
            )
            command.add_argument("--confirm", action="store_true", required=True)
    return parser


def _service(database: Any) -> Any:
    from potbot.controller.task_discovery import TaskDiscoveryService

    return TaskDiscoveryService(database)


def _creation_body(path: Path) -> dict[str, Any]:
    from potbot.api.jobs_cli import _read_object
    from potbot.controller.task_discovery import TaskDiscoveryService

    value = _read_object(path, maximum=8192)
    TaskDiscoveryService._request(value)
    return value  # The original body owns idempotency; normalization belongs to the controller.


def _prepare_native(
    database: Any, context: Any, args: argparse.Namespace, embedding: dict[str, Any]
) -> dict[str, Any]:
    """Inspect retained identity and confined files without loading/downloading a model."""
    from potbot.domain.embeddings import SOURCE_TEXT_INPUT_FORMAT
    from potbot.domain.errors import InvalidInputError, ModelUnavailableError, ScopeDeniedError
    from potbot.domain.serialization import content_hash
    from potbot.memory.embedding_profile import (
        QWEN_MODEL,
        QWEN_REVISION,
        TORCH_VERSION,
        TRANSFORMERS_VERSION,
        EmbeddingProfile,
    )
    from potbot.memory.local_encoder import MANIFEST, MODEL_FILES
    from potbot.workspace_sandbox import load_workspace_sandbox

    with database.transaction(context) as conn:
        row = conn.execute(
            "SELECT s.content,s.content_hash,v.input_format FROM embedding_spaces s "
            "JOIN embedding_views v ON (v.workspace_id,v.space_id)=(s.workspace_id,s.id) "
            "WHERE s.workspace_id=%s AND s.id=%s AND v.embedding_name=%s",
            (context.workspace_id, UUID(embedding["spaceId"]), embedding["embeddingName"]),
        ).fetchone()
    if row is None:
        raise ScopeDeniedError("The selected named embedding space is unavailable")
    if (
        row["content_hash"] != embedding["profileSha256"]
        or content_hash(row["content"]) != row["content_hash"]
        or row["input_format"] != SOURCE_TEXT_INPUT_FORMAT
    ):
        raise InvalidInputError("The retained embedding profile differs from the reviewed request")
    try:
        profile = EmbeddingProfile(**row["content"])
    except TypeError:
        raise InvalidInputError("The retained embedding profile is invalid") from None
    if (
        profile.model != QWEN_MODEL
        or profile.model_revision != QWEN_REVISION
        or profile.tokenizer_revision != QWEN_REVISION
        or profile.backend != "transformers"
        or profile.backend_version != TRANSFORMERS_VERSION
        or profile.runtime_version != TORCH_VERSION
    ):
        raise InvalidInputError("Native refinement requires the existing pinned local Qwen profile")
    sandbox = load_workspace_sandbox(database, context)
    model_dir = sandbox.confine(args.model_dir, area="models")
    artifact_root = sandbox.confine(
        args.artifacts_root or sandbox.path("artifacts") / "objects", area="artifacts"
    )
    if not model_dir.is_dir() or any(
        (model_dir / name).is_symlink() or not (model_dir / name).is_file()
        for name in (*MODEL_FILES, MANIFEST)
    ):
        raise ModelUnavailableError("The pinned offline local model files are unavailable")
    # File digests, tokenizer/model loading and inference are verified inside
    # the existing supervised, budgeted worker; preflight never loads weights.
    return {"profile": profile, "sandbox": sandbox, "model": model_dir, "artifacts": artifact_root}


def _execute(
    service: Any, database: Any, context: Any, args: argparse.Namespace, token: str
) -> dict[str, Any]:
    prior = service.execution_replay(
        context, args.identity, args.revision, args.digest, args.key, setup_token=token
    )
    if prior is not None:
        return dict(prior)
    # Durable command recovery takes precedence over every optional native
    # import, filesystem check and model preparation.
    from potbot.domain.errors import (
        DomainError,
        IndeterminateExecutionError,
        OperationCancelledError,
        RevisionConflictError,
        ScopeDeniedError,
        StateMismatchError,
    )
    from potbot.domain.serialization import content_hash

    selected = service.search(context, args.identity)["search"]
    if (
        selected["revision"] != args.revision
        or selected["bindingSha256"] != args.digest
        or selected["status"] != "requested"
        or selected["mode"] != "hybrid"
        or not selected["approvalValid"]
        or not selected["authorityCurrent"]
    ):
        raise RevisionConflictError(
            "Review the current pending hybrid search before native refinement"
        )
    native = _prepare_native(database, context, args, selected["embedding"])
    claim = service.claim(
        context,
        args.identity,
        args.revision,
        args.digest,
        args.key,
        setup_token=token,
        fresh_only=True,
    )
    if "replay" in claim:
        return dict(claim["replay"])
    worker = UUID(claim["workerToken"])
    started = False
    binding_keys = ("context", "request", "semanticCandidates", "repositoryId")
    claim_hash = content_hash({key: claim[key] for key in binding_keys})

    def authorize() -> None:
        current = service.validate_claim(context, args.identity, worker)
        if content_hash({key: current[key] for key in binding_keys}) != claim_hash:
            raise ScopeDeniedError("The exact native discovery claim changed")

    def cancelled() -> bool:
        try:
            authorize()
            return False
        except DomainError:
            return True

    try:
        authorize()
        request = claim["request"]
        embedding = request["embedding"]
        if (
            embedding != selected["embedding"]
            or native["profile"].fingerprint != embedding["profileSha256"]
        ):
            raise ScopeDeniedError("The native embedding profile no longer matches its admission")
        from potbot.controller.task_discovery import embedding_documents, refinement_claim_sha256
        from potbot.memory.embeddings import EmbeddingService
        from potbot.memory.supervised_encoder import SupervisedQwenEncoder
        from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

        encoder = SupervisedQwenEncoder(
            native["model"], native["profile"], sandbox_root=native["sandbox"].root
        )
        embeddings = EmbeddingService(
            database, ArtifactStore(database, LocalArtifactBackend(native["artifacts"])), encoder
        )
        started = True
        report = embeddings.rank_transient(
            context,
            UUID(request["episodeId"]),
            UUID(embedding["spaceId"]),
            request["query"],
            embedding_documents(claim["semanticCandidates"]),
            embedding_name=embedding["embeddingName"],
            runtime_budget_ms=embedding["runtimeBudgetMs"],
            repository_id=UUID(claim["repositoryId"]),
            request_binding_sha256=refinement_claim_sha256(context, args.identity, worker),
            cancelled=cancelled,
        )
        started = False  # Confirmed usage is already settled by the embedding owner.
        authorize()
        return dict(service.complete(context, args.identity, worker, report))
    except BaseException as error:
        indeterminate = getattr(error, "embedding_usage_indeterminate", None)
        uncertain = indeterminate is True or (
            indeterminate is None
            and (
                isinstance(error, IndeterminateExecutionError)
                or (started and not isinstance(error, DomainError))
            )
        )
        reason = (
            "search_indeterminate"
            if uncertain
            else "search_cancelled"
            if isinstance(error, OperationCancelledError)
            else "authority_changed"
            if isinstance(error, (ScopeDeniedError, StateMismatchError, RevisionConflictError))
            else "search_failed"
        )
        try:
            service.fail(context, args.identity, worker, reason)
        except DomainError:
            pass  # A newer control or setup-key rotation already fenced the claim.
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from potbot.api.wire import OperationSemantics, error_response

    try:
        from potbot.api.jobs_cli import database_context
        from potbot.api.operations_cli import _setup_token

        database, context = database_context(args)
        service = _service(database)
        token = _setup_token() if args.command in _MUTATIONS else ""
        if args.command == "context":
            result = service.context(context, args.episode, args.state)
        elif args.command == "searches":
            result = service.searches(
                context, episode_id=args.episode, limit=args.limit, after=args.after
            )
        elif args.command in {"search", "result", "selection"}:
            result = getattr(service, args.command)(context, args.identity)
        elif args.command == "inspect":
            result = service.inspect_candidate(context, args.identity, args.candidate)
        elif args.command == "create":
            result = service.create(
                context, _creation_body(args.input), args.key, setup_token=token
            )
        elif args.command == "cancel":
            result = service.cancel(
                context, args.identity, args.revision, args.digest, args.key, setup_token=token
            )
        elif args.command == "select":
            result = service.select(
                context,
                args.identity,
                args.revision,
                args.digest,
                args.candidate,
                args.key,
                setup_token=token,
            )
        else:
            result = _execute(service, database, context, args, token)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except Exception as error:
        response = error_response(
            error,
            semantics=OperationSemantics(
                read_only=args.command not in _MUTATIONS, idempotency_key=args.command in _MUTATIONS
            ),
        )
        print(json.dumps(response.body, sort_keys=True, ensure_ascii=True))
        return 1
