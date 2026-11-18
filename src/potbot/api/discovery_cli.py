"""Discover, review and measure capabilities without granting execution authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments

_MUTATIONS = frozenset(
    {
        "add-source",
        "refresh",
        "source-control",
        "publisher-control",
        "prepare",
        "measure",
        "handoff",
        "cancel",
    }
)


def _manifest_bytes(path: Path) -> bytes:
    from potbot.domain.errors import InvalidInputError

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 2_097_152:
                raise InvalidInputError("The curated manifest must be a bounded regular file")
            chunks = bytearray()
            while len(chunks) <= 2_097_152:
                data = os.read(fd, min(65_536, 2_097_153 - len(chunks)))
                if not data:
                    break
                chunks.extend(data)
            after = os.fstat(fd)
            if len(chunks) != before.st_size or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise InvalidInputError("The curated manifest changed during its bounded read")
            return bytes(chunks)
        finally:
            os.close(fd)
    except OSError:
        raise InvalidInputError("The curated manifest is unavailable or redirected") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="potbot discovery", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sources", "publishers", "checks", "search"):
        command = commands.add_parser(name)
        command.add_argument("--limit", type=int, default=25)
        command.add_argument("--after")
        if name in {"publishers", "search"}:
            command.add_argument("--source", type=UUID)
        if name == "search":
            command.add_argument("query", nargs="?", default="")
            command.add_argument(
                "--kind", choices=("skill", "mcp_server", "tool", "workflow", "plugin")
            )
            command.add_argument(
                "--constraints",
                type=Path,
                help="bounded classes/types/platform/effects/scopes/local JSON",
            )
    for name in ("source", "candidate", "check"):
        command = commands.add_parser(name)
        command.add_argument("identity", type=UUID)
    add = commands.add_parser(
        "add-source", help="admit one fixed source contract with the native setup capability"
    )
    add.add_argument(
        "--adapter", required=True, choices=("curated", "clawhub", "mcp_registry", "skills_sh")
    )
    add.add_argument("--name", required=True)
    add.add_argument("--ttl", type=int, default=900)
    add.add_argument(
        "--manifest", type=Path, help="exact curated JSON bytes; no arbitrary live endpoint"
    )
    refresh = commands.add_parser(
        "refresh", help="retrieve a complete bounded source search snapshot"
    )
    refresh.add_argument("identity", type=UUID)
    refresh.add_argument("--query", default="")
    refresh.add_argument("--manifest", type=Path, help="the same admitted curated JSON file")
    for name, actions in (
        ("source-control", ("enable", "disable", "revoke")),
        ("publisher-control", ("approve", "block", "revoke")),
    ):
        command = commands.add_parser(name)
        command.add_argument("identity", type=UUID)
        command.add_argument("action", choices=actions)
        command.add_argument("--revision", type=int, required=True)
        command.add_argument("--digest", required=True)
    prepare = commands.add_parser(
        "prepare", help="resolve an exact candidate and retain private package review"
    )
    prepare.add_argument("identity", type=UUID)
    prepare.add_argument("--snapshot", type=UUID, required=True)
    prepare.add_argument("--digest", required=True, help="exact candidate SHA-256 from discovery")
    prepare.add_argument("--agent", type=UUID, required=True)
    measure = commands.add_parser(
        "measure", help="measure one retained review in its configured existing sandbox"
    )
    measure.add_argument("identity", type=UUID)
    measure.add_argument("--revision", type=int, required=True)
    measure.add_argument("--digest", required=True)
    measure.add_argument(
        "--manifest", type=Path, required=True, help="existing qualified operator runtime manifest"
    )
    measure.add_argument("--workspace-name", required=True)
    measure.add_argument(
        "--probe-python",
        default="/usr/bin/python3",
        help="independently approved image interpreter",
    )
    measure.add_argument("--probe-python-sha256", required=True)
    for name in ("handoff", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("identity", type=UUID)
        command.add_argument("--revision", type=int, required=True)
        command.add_argument("--digest", required=True)
    for name, command in commands.choices.items():
        if name in _MUTATIONS:
            command.add_argument(
                "--key", required=True, help="reuse the exact key/body after a lost response"
            )
            command.add_argument("--confirm", action="store_true", required=True)
    return parser


def _measure(
    service: Any, database: Any, context: Any, args: argparse.Namespace, token: str
) -> dict[str, Any]:
    from potbot.context.skill_eligibility import configured_skill_eligibility
    from potbot.domain.errors import (
        DomainError,
        IndeterminateExecutionError,
        OperationCancelledError,
    )

    prior = service.measurement_replay(
        context, args.identity, args.revision, args.digest, args.key, setup_token=token
    )
    if prior is not None:
        return dict(prior)
    evaluator = configured_skill_eligibility(
        database,
        context,
        manifest=args.manifest,
        workspace_name=args.workspace_name,
        probe_executable=args.probe_python,
        probe_executable_sha256=args.probe_python_sha256,
    )
    claim = service.claim_measurement(
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
        service.validate_measurement(context, args.identity, worker)

    def cancelled() -> bool:
        try:
            authorize()
            return False
        except DomainError:
            return True

    try:
        report = evaluator.evaluate(
            context,
            check_id=args.identity,
            agent_id=UUID(claim["agentId"]),
            candidate_sha256=claim["candidateSha256"],
            source_binding_sha256=claim["sourceBindingSha256"],
            requirements=claim["requirements"],
            authorize_attempt=authorize,
            cancelled=cancelled,
        )
        return dict(service.complete_measurement(context, args.identity, worker, report))
    except Exception as error:
        reason = (
            "measurement_indeterminate"
            if isinstance(error, IndeterminateExecutionError)
            else "measurement_cancelled"
            if isinstance(error, OperationCancelledError)
            else "measurement_failed"
        )
        try:
            service.fail_measurement(context, args.identity, worker, reason)
        except DomainError:
            pass  # A current cancellation/rotation already fenced this attempt.
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Help never imports a web server, PostgreSQL, native vault or sandbox.
    from potbot.api.wire import OperationSemantics, error_response

    try:
        from potbot.api.jobs_cli import _read_object, database_context
        from potbot.api.operations_cli import _setup_token
        from potbot.controller.capability_discovery import DiscoveryService, native_registry_token
        from potbot.controller.registry_sources import RegistrySources, parse_curated_manifest
        from potbot.domain.errors import InvalidInputError

        raw = None
        candidates: tuple[Any, ...] = ()
        if args.command in {"add-source", "refresh"} and args.manifest is not None:
            raw = _manifest_bytes(args.manifest)
            candidates = parse_curated_manifest(raw)
        manifest_sha = hashlib.sha256(raw).hexdigest() if raw is not None else None
        database, context = database_context(args)
        client = RegistrySources(curated=candidates, skills_sh_token=native_registry_token)
        service = DiscoveryService(database, sources=client)
        token = _setup_token() if args.command in _MUTATIONS else ""
        if args.command == "sources":
            result = service.sources(context, limit=args.limit, after=args.after)
        elif args.command == "publishers":
            result = service.publishers(
                context, source_id=args.source, limit=args.limit, after=args.after
            )
        elif args.command == "checks":
            result = service.checks(context, limit=args.limit, after=args.after)
        elif args.command == "search":
            constraints = _read_object(args.constraints, maximum=8192) if args.constraints else None
            result = service.candidates(
                context,
                query=args.query,
                kind=args.kind,
                source_id=args.source,
                limit=args.limit,
                after=args.after,
                constraints=constraints,
            )
        elif args.command in {"source", "candidate", "check"}:
            result = getattr(service, args.command)(context, args.identity)
        elif args.command == "add-source":
            if (args.adapter == "curated") != (raw is not None):
                raise InvalidInputError(
                    "A curated source requires exactly its inspected manifest file"
                )
            result = service.add_source(
                context,
                {
                    "adapter": args.adapter,
                    "name": args.name,
                    "ttlSeconds": args.ttl,
                    "curatedManifestSha256": manifest_sha,
                },
                args.key,
                setup_token=token,
            )
        elif args.command == "refresh":
            result = service.refresh(
                context,
                args.identity,
                args.query,
                args.key,
                setup_token=token,
                curated_manifest_sha256=manifest_sha,
            )
        elif args.command in {"source-control", "publisher-control"}:
            result = service.control(
                context,
                args.command.removesuffix("-control"),
                args.identity,
                args.action,
                args.revision,
                args.digest,
                args.key,
                setup_token=token,
            )
        elif args.command == "prepare":
            result = service.create_check(
                context,
                args.identity,
                args.snapshot,
                args.digest,
                args.agent,
                args.key,
                setup_token=token,
            )
        elif args.command == "measure":
            assert token is not None
            result = _measure(service, database, context, args, token)
        else:
            result = service.action(
                context,
                args.identity,
                args.command,
                args.revision,
                args.digest,
                args.key,
                setup_token=token,
            )
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
