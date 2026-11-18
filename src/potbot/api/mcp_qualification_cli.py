"""Qualify reviewed MCP task cases and reconcile retained native receipts."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments

_MUTATIONS = frozenset({"admit", "control", "request", "run", "recover", "cancel"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="potbot mcp-qualification", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, parent in (("suites", "binding"), ("attempts", "suite")):
        command = commands.add_parser(name)
        command.add_argument("--" + parent, type=UUID)
        command.add_argument("--limit", type=int, default=25)
        command.add_argument("--after")
    for name in ("suite", "attempt", "report"):
        commands.add_parser(name).add_argument("identity", type=UUID)
    admit = commands.add_parser(
        "admit", help="review private cases against an existing task and installed MCP binding"
    )
    admit.add_argument("--binding", type=UUID, required=True)
    admit.add_argument("--episode", type=UUID, required=True)
    admit.add_argument("--state", type=UUID, required=True)
    admit.add_argument("--profile", type=Path, required=True)
    admit.add_argument("--suite", type=Path, required=True)
    for name in ("control", "request", "run", "recover", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("identity", type=UUID)
        command.add_argument("--revision", type=int, required=True)
        command.add_argument("--digest", required=True)
        if name == "control":
            command.add_argument("action", choices=("enable", "disable", "revoke"))
        if name in {"run", "recover"}:
            command.add_argument("--profile", type=Path, required=True)
            command.add_argument("--suite", type=Path, required=True)
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--workspace-name", required=True)
            command.add_argument("--artifacts-root", type=Path, required=True)
            command.add_argument("--receipts-root", type=Path, required=True)
    for name, command in commands.choices.items():
        if name in _MUTATIONS:
            command.add_argument(
                "--key",
                required=True,
                help="reuse the exact key and body to recover a lost response",
            )
            command.add_argument("--confirm", action="store_true", required=True)
    return parser


async def _execute(
    service: Any, database: Any, context: Any, args: argparse.Namespace, token: str
) -> dict[str, Any]:
    recovery = args.command == "recover"
    prior = service.execution_replay(
        context,
        args.identity,
        args.revision,
        args.digest,
        args.key,
        setup_token=token,
        recovery=recovery,
    )
    if prior is not None:
        return dict(prior)
    # Old command receipts remain readable without importing/configuring a host.
    from potbot.controller.mcp_discovery import installed_identity
    from potbot.domain.errors import (
        DomainError,
        IndeterminateExecutionError,
        OperationCancelledError,
        RevisionConflictError,
        ScopeDeniedError,
        StateMismatchError,
    )
    from potbot.tools.mcp_discovery_host import read_mcp_profile
    from potbot.tools.mcp_qualification_host import (
        configured_mcp_qualification_host,
        qualification_consumer_sha256,
        read_mcp_qualification_suite,
    )

    selected = service.attempt(context, args.identity)["attempt"]
    if (
        selected["revision"] != args.revision
        or selected["bindingSha256"] != args.digest
        or (not recovery and selected["status"] != "requested")
        or (recovery and not selected["recoveryAvailable"])
    ):
        raise RevisionConflictError("Review the current qualification before configuring its host")
    admitted = service.suite(context, UUID(selected["suiteId"]))["suite"]
    binding = UUID(selected["bindingId"])
    profile = read_mcp_profile(context, args.profile, binding_id=binding)
    suite = read_mcp_qualification_suite(args.suite)
    if (
        str(profile.agent_id) != selected["agentId"]
        or profile.configuration_sha256 != selected["configurationSha256"]
        or profile.installed.installation.fingerprint != admitted["installationSha256"]
        or suite.sha256 != selected["suiteSha256"]
        or suite.file_sha256 != admitted["suiteFileSha256"]
        or qualification_consumer_sha256() != admitted["consumerSha256"]
    ):
        raise ScopeDeniedError("Native qualification inputs differ from their reviewed admission")
    with configured_mcp_qualification_host(
        database,
        context,
        manifest=args.manifest,
        workspace_name=args.workspace_name,
        artifacts_root=args.artifacts_root,
        receipts_root=args.receipts_root,
    ) as host:
        host.native.validate_private_paths((*profile.private_paths, suite.path))
        claim = service.claim(
            context,
            args.identity,
            args.revision,
            args.digest,
            args.key,
            setup_token=token,
            recovery=recovery,
            fresh_only=True,
        )
        if "replay" in claim:
            return dict(claim["replay"])
        worker = UUID(claim["workerToken"])

        def authorize() -> None:
            current = service.validate_claim(context, args.identity, worker)
            readback = read_mcp_profile(context, args.profile, binding_id=binding)
            case_readback = read_mcp_qualification_suite(args.suite)
            config = current["config"]
            if (
                readback.configuration_sha256 != profile.configuration_sha256
                or config["configurationSha256"] != profile.configuration_sha256
                or config["suiteSha256"] != case_readback.sha256
                or config["suiteFileSha256"] != case_readback.file_sha256
                or config["consumerSha256"] != qualification_consumer_sha256()
                or any(
                    config[key] != value
                    for key, value in installed_identity(profile.installed, context).items()
                )
            ):
                raise ScopeDeniedError("The native qualification no longer matches its admission")

        def cancelled() -> bool:
            try:
                authorize()
                return False
            except DomainError:
                return True

        def record(event: dict[str, Any]) -> None:
            service.record_case(context, args.identity, worker, event)

        def load(case_id: str) -> dict[str, Any] | None:
            result = service.case(context, args.identity, worker, case_id)
            return dict(result) if result is not None else None

        try:
            authorize()
            execute = host.recover if recovery else host.run
            report = await execute(
                context,
                attempt_id=args.identity,
                profile=profile,
                suite=suite,
                reference=claim["reference"],
                task_binding=claim["config"]["taskBinding"],
                authorize_attempt=authorize,
                record_case=record,
                load_case=load,
                cancelled=cancelled,
            )
            authorize()
            return dict(service.complete(context, args.identity, worker, report))
        except BaseException as error:
            reason = (
                "qualification_indeterminate"
                if isinstance(error, (IndeterminateExecutionError, asyncio.CancelledError))
                else "qualification_cancelled"
                if isinstance(error, OperationCancelledError)
                else "authority_changed"
                if isinstance(error, (ScopeDeniedError, StateMismatchError))
                else "qualification_failed"
            )
            try:
                service.fail(context, args.identity, worker, reason)
            except DomainError:
                pass  # A newer control or setup-key rotation already fenced the claim.
            raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Help is usable without optional web, PostgreSQL or native dependencies.
    from potbot.api.wire import OperationSemantics, error_response

    try:
        from potbot.api.jobs_cli import database_context
        from potbot.api.operations_cli import _setup_token
        from potbot.controller.mcp_qualification import McpQualificationService

        database, context = database_context(args)
        service = McpQualificationService(database)
        token = _setup_token() if args.command in _MUTATIONS else ""
        if args.command == "suites":
            result = service.suites(
                context, binding_id=args.binding, limit=args.limit, after=args.after
            )
        elif args.command == "attempts":
            result = service.attempts(
                context, suite_id=args.suite, limit=args.limit, after=args.after
            )
        elif args.command in {"suite", "attempt", "report"}:
            result = getattr(service, args.command)(context, args.identity)
        elif args.command == "admit":
            from potbot.tools.mcp_discovery_host import read_mcp_profile
            from potbot.tools.mcp_qualification_host import read_mcp_qualification_suite

            profile = read_mcp_profile(context, args.profile, binding_id=args.binding)
            suite = read_mcp_qualification_suite(args.suite)
            result = service.admit(
                context, profile, suite, args.episode, args.state, args.key, setup_token=token
            )
        elif args.command == "control":
            result = service.control(
                context,
                args.identity,
                args.action,
                args.revision,
                args.digest,
                args.key,
                setup_token=token,
            )
        elif args.command in {"request", "cancel"}:
            result = getattr(service, args.command)(
                context, args.identity, args.revision, args.digest, args.key, setup_token=token
            )
        else:
            result = asyncio.run(_execute(service, database, context, args, token))
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
