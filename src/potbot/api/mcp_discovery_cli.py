"""Review retained MCP interfaces and run one explicit native reconciliation."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments

_MUTATIONS = frozenset({"admit", "control", "request", "reconcile", "cancel"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="potbot mcp-discovery", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("bindings", "attempts", "entries"):
        command = commands.add_parser(name)
        command.add_argument("--limit", type=int, default=25)
        command.add_argument("--after")
        if name == "attempts":
            command.add_argument("--binding", type=UUID)
        if name == "entries":
            command.add_argument("identity", type=UUID)
            command.add_argument(
                "--kind", choices=("tool", "resource", "resource_template", "prompt")
            )
            command.add_argument("--query", default="")
    for name in ("binding", "attempt"):
        commands.add_parser(name).add_argument("identity", type=UUID)
    reference = commands.add_parser(
        "reference", help="read an exact retained reference; no operation is invoked"
    )
    reference.add_argument("identity", type=UUID)
    reference.add_argument("entry", type=UUID)
    admit = commands.add_parser(
        "admit", help="bind existing installed owner identities to signed catalogue inspection"
    )
    admit.add_argument("--profile", type=Path, required=True)
    admit.add_argument("--name", required=True)
    admit.add_argument("--ttl", type=int, default=300)
    admit.add_argument("--watch-ms", type=int, default=0)
    for name in ("control", "request", "reconcile", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("identity", type=UUID)
        command.add_argument("--revision", type=int, required=True)
        command.add_argument("--digest", required=True)
        if name == "control":
            command.add_argument("action", choices=("enable", "disable", "revoke"))
        if name == "reconcile":
            command.add_argument("--profile", type=Path, required=True)
            command.add_argument(
                "--manifest",
                type=Path,
                required=True,
                help="existing qualified operator runtime manifest",
            )
            command.add_argument("--workspace-name", required=True)
    for name, command in commands.choices.items():
        if name in _MUTATIONS:
            command.add_argument(
                "--key",
                required=True,
                help="reuse the exact key and body to recover a lost response",
            )
            command.add_argument("--confirm", action="store_true", required=True)
    return parser


async def _reconcile(
    service: Any, database: Any, context: Any, args: argparse.Namespace, token: str
) -> dict[str, Any]:
    # An old attempt must be inspectable even when its runtime has gone away.
    prior = service.execution_replay(
        context, args.identity, args.revision, args.digest, args.key, setup_token=token
    )
    if prior is not None:
        return dict(prior)
    from potbot.controller.mcp_discovery import installed_identity
    from potbot.domain.errors import (
        DomainError,
        IndeterminateExecutionError,
        OperationCancelledError,
        RevisionConflictError,
        ScopeDeniedError,
    )
    from potbot.tools.mcp_discovery_host import configured_mcp_discovery_host, read_mcp_profile
    from potbot.tools.mcp_protocol import McpCatalogChangedError

    selected = service.attempt(context, args.identity)["attempt"]
    if (
        selected["status"] != "requested"
        or selected["revision"] != args.revision
        or selected["bindingSha256"] != args.digest
    ):
        raise RevisionConflictError(
            "Review the current MCP reconciliation before configuring its host"
        )
    profile = read_mcp_profile(context, args.profile, binding_id=UUID(selected["bindingId"]))
    if (
        str(profile.agent_id) != selected["agentId"]
        or profile.configuration_sha256 != selected["configurationSha256"]
        or profile.installed.installation.fingerprint != selected["installationSha256"]
    ):
        raise ScopeDeniedError("The native MCP profile differs from the selected reconciliation")
    with configured_mcp_discovery_host(
        database, context, manifest=args.manifest, workspace_name=args.workspace_name
    ) as host:
        host.validate_private_paths(profile.private_paths)
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

        def authorize() -> None:
            current = service.validate_claim(context, args.identity, worker)
            readback = read_mcp_profile(
                context, args.profile, binding_id=UUID(selected["bindingId"])
            )
            if (
                readback.configuration_sha256 != profile.configuration_sha256
                or current["config"]["configurationSha256"] != profile.configuration_sha256
                or any(
                    current["config"][key] != value
                    for key, value in installed_identity(profile.installed, context).items()
                )
            ):
                raise ScopeDeniedError("The running MCP profile no longer matches its admission")

        def cancelled() -> bool:
            try:
                authorize()
                return False
            except DomainError:
                return True

        try:
            authorize()
            catalog, observation = await host.observe(
                context,
                attempt_id=args.identity,
                installed=profile.installed,
                configuration_sha256=profile.configuration_sha256,
                agent_id=profile.agent_id,
                watch_ms=claim["config"]["watchMs"],
                authorize_attempt=authorize,
                cancelled=cancelled,
            )
            authorize()
            return dict(service.complete(context, args.identity, worker, catalog, observation))
        except BaseException as error:
            reason = (
                "reconciliation_indeterminate"
                if isinstance(error, (IndeterminateExecutionError, asyncio.CancelledError))
                else "reconciliation_cancelled"
                if isinstance(error, OperationCancelledError)
                else "catalog_changed"
                if isinstance(error, McpCatalogChangedError)
                else "authority_changed"
                if isinstance(error, ScopeDeniedError)
                else "reconciliation_failed"
            )
            try:
                service.fail(context, args.identity, worker, reason)
            except DomainError:
                pass  # A current control or rotation already fenced this claim.
            raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Argument help never imports optional native, PostgreSQL or web packages.
    from potbot.api.wire import OperationSemantics, error_response

    try:
        from potbot.api.jobs_cli import database_context
        from potbot.api.operations_cli import _setup_token
        from potbot.controller.mcp_discovery import McpDiscoveryService

        database, context = database_context(args)
        service = McpDiscoveryService(database)
        token = _setup_token() if args.command in _MUTATIONS else ""
        if args.command == "bindings":
            result = service.bindings(context, limit=args.limit, after=args.after)
        elif args.command == "attempts":
            result = service.attempts(
                context, binding_id=args.binding, limit=args.limit, after=args.after
            )
        elif args.command == "entries":
            result = service.entries(
                context,
                args.identity,
                kind=args.kind,
                query=args.query,
                limit=args.limit,
                after=args.after,
            )
        elif args.command in {"binding", "attempt"}:
            result = getattr(service, args.command)(context, args.identity)
        elif args.command == "reference":
            result = service.reference(context, args.identity, args.entry)
        elif args.command == "admit":
            from potbot.tools.mcp_discovery_host import read_mcp_profile

            profile = read_mcp_profile(context, args.profile)
            result = service.admit(
                context,
                profile.installed,
                profile.agent_id,
                profile.configuration_sha256,
                args.name,
                args.ttl,
                args.watch_ms,
                args.key,
                setup_token=token,
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
            result = asyncio.run(_reconcile(service, database, context, args, token))
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
