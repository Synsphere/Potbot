"""Inspect, check, run and monitor operations through the existing durable worker."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments


def _setup_token() -> str:
    """An explicit control command may use the operator's existing native capability."""
    from potbot.domain.errors import CapabilityUnavailableError
    from potbot.service_credentials import ServiceCredentials

    value = ServiceCredentials().store.get("api.setup_token")
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 4096
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise CapabilityUnavailableError("The operator setup capability is unavailable")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot operations", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="search scoped operation records")
    listing.add_argument("--query", "--q", default="")
    listing.add_argument("--kind", help="tool, cog, workflow, connector, model, skill or gateway")
    listing.add_argument("--state", help="filter by the recorded readiness state")
    listing.add_argument("--after", help="opaque cursor from the same scope and search")
    listing.add_argument("--limit", type=int, default=25)
    inspect = commands.add_parser("inspect", help="inspect evidence and linked recorded usage")
    inspect.add_argument("operation_id", help="the catalogue's 64-character operation ID")
    contract = commands.add_parser("contract", help="describe the supported invocation contract")
    contract.add_argument("operation_id")
    check = commands.add_parser("check", help="queue a task-bound worker eligibility check")
    check.add_argument("operation_id")
    check.add_argument("--input", required=True, type=Path, help="bounded existing JobSpec JSON")
    check.add_argument("--key", required=True, help="stable idempotency key for this check")
    run = commands.add_parser("run", help="queue one explicitly confirmed, previously checked run")
    run.add_argument("operation_id")
    run.add_argument("--check-job", required=True, type=UUID)
    run.add_argument("--input", required=True, type=Path, help="the exact previously checked JSON")
    run.add_argument("--key", required=True, help="reuse this key after a lost response")
    run.add_argument("--confirm", action="store_true", required=True)
    job = commands.add_parser("job", help="inspect an owned check or run")
    events = commands.add_parser("events", help="read a bounded page of owned progress events")
    result_parser = commands.add_parser("result", help="inspect owned output and result metadata")
    artifact = commands.add_parser("artifact", help="read a verified bounded result range as JSON")
    download = commands.add_parser("download", help="verify and save an owned native result")
    cancel = commands.add_parser("cancel", help="request cancellation without replaying work")
    for command in (job, events, result_parser, artifact, download, cancel):
        command.add_argument("operation_id")
        command.add_argument("job_id", type=UUID)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    artifact.add_argument("--offset", type=int, default=0)
    artifact.add_argument("--length", type=int, default=262144)
    download.add_argument("--output", required=True, type=Path)
    cancel.add_argument("--key", required=True)
    cancel.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args(argv)

    # Keep --help usable without PostgreSQL, native credentials or web extras.
    from potbot.api.jobs_cli import _read_object, database_context
    from potbot.api.wire import OperationSemantics, error_response
    from potbot.controller.operation_catalogue import OperationCatalogue

    try:
        database, actor = database_context(args)
        if args.command == "list":
            result = OperationCatalogue(database).list(
                actor,
                query=args.query,
                kind=args.kind,
                state=args.state,
                after=args.after,
                limit=args.limit,
            )
        elif args.command == "inspect":
            result = OperationCatalogue(database).inspect(actor, args.operation_id)
        else:
            from potbot.controller.operation_workbench import OperationWorkbench

            workbench = OperationWorkbench(database)
            if args.command == "contract":
                result = workbench.describe(actor, args.operation_id)
            elif args.command in {"check", "run"}:
                token = _setup_token()
                value = _read_object(args.input, maximum=16384)
                if args.command == "check":
                    result = workbench.check(
                        actor, args.operation_id, value, args.key, setup_token=token
                    )
                else:
                    result = workbench.launch(
                        actor,
                        args.operation_id,
                        args.check_job,
                        value,
                        args.key,
                        setup_token=token,
                    )
            elif args.command == "job":
                result = workbench.inspect(actor, args.operation_id, args.job_id)
            elif args.command == "events":
                result = workbench.events(
                    actor, args.operation_id, args.job_id, after=args.after, limit=args.limit
                )
            elif args.command == "result":
                result = workbench.result(actor, args.operation_id, args.job_id)
            elif args.command == "artifact":
                result = workbench.artifact(
                    actor, args.operation_id, args.job_id, offset=args.offset, length=args.length
                )
            elif args.command == "download":
                from potbot.api.operation_result_download import download_result

                result = download_result(
                    workbench, actor, args.operation_id, args.job_id, args.output
                )
            else:
                result = workbench.cancel(actor, args.operation_id, args.job_id, args.key)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except Exception as error:
        response = error_response(
            error,
            semantics=OperationSemantics(
                read_only=args.command not in {"check", "run", "cancel"},
                idempotency_key=args.command in {"check", "run", "cancel"},
            ),
        )
        print(json.dumps(response.body, sort_keys=True, ensure_ascii=True))
        return 1
