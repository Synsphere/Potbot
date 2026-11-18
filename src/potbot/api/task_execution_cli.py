"""Check, execute and verify an exact discovery selection through existing jobs."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot task-execution", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    context = commands.add_parser("context", help="read the exact selection and operation contract")
    check = commands.add_parser(
        "check", help="queue a check with fixed input and result assertions"
    )
    run = commands.add_parser("run", help="launch the immutable request retained by a check")
    jobs = commands.add_parser("jobs", help="list owned checks and runs for this selection")
    inspect = commands.add_parser("job", help="reopen an owned check or run")
    events = commands.add_parser("events", help="read bounded owned progress events")
    result = commands.add_parser("result", help="verify complete output against fixed assertions")
    artifact = commands.add_parser("artifact", help="read a verified bounded artifact range")
    download = commands.add_parser("download", help="verify and save an owned result artifact")
    cancel = commands.add_parser("cancel", help="stop owned work without replaying the operation")
    for command in (context, check, run, jobs, inspect, events, result, artifact, download, cancel):
        command.add_argument("selection_id", type=UUID)
    for command in (inspect, events, result, artifact, download, cancel):
        command.add_argument("job_id", type=UUID)
    for command in (check, run, cancel):
        command.add_argument(
            "--key", required=True, help="reuse the exact key after a lost response"
        )
        command.add_argument("--confirm", action="store_true", required=True)
    check.add_argument("--selection-sha256", required=True)
    check.add_argument("--input", required=True, type=Path, help="existing bounded JobSpec JSON")
    check.add_argument("--acceptance", required=True, type=Path, help="fixed literal result oracle")
    run.add_argument("--check-job", required=True, type=UUID)
    run.add_argument("--binding-sha256", required=True)
    jobs.add_argument("--limit", type=int, default=25)
    jobs.add_argument("--after")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    artifact.add_argument("--offset", type=int, default=0)
    artifact.add_argument("--length", type=int, default=262144)
    download.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    # Help needs no PostgreSQL, native vault, provider or worker configuration.
    from potbot.api.jobs_cli import _read_object, database_context
    from potbot.api.wire import OperationSemantics, error_response
    from potbot.controller.task_execution import TaskExecutionService

    try:
        database, actor = database_context(args)
        service = TaskExecutionService(database)
        if args.command == "context":
            value = service.context(actor, args.selection_id)
        elif args.command in {"check", "run"}:
            from potbot.api.operations_cli import _setup_token

            token = _setup_token()
            if args.command == "check":
                value = service.check(
                    actor,
                    args.selection_id,
                    args.selection_sha256,
                    _read_object(args.input, maximum=16_384),
                    _read_object(args.acceptance, maximum=65_536),
                    args.key,
                    setup_token=token,
                )
            else:
                value = service.launch(
                    actor,
                    args.selection_id,
                    args.check_job,
                    args.binding_sha256,
                    args.key,
                    setup_token=token,
                )
        elif args.command == "jobs":
            value = service.jobs(actor, args.selection_id, limit=args.limit, after=args.after)
        elif args.command == "job":
            value = service.inspect(actor, args.selection_id, args.job_id)
        elif args.command == "events":
            value = service.events(
                actor, args.selection_id, args.job_id, after=args.after, limit=args.limit
            )
        elif args.command == "result":
            value = service.result(actor, args.selection_id, args.job_id)
        elif args.command == "artifact":
            value = service.artifact(
                actor, args.selection_id, args.job_id, offset=args.offset, length=args.length
            )
        elif args.command == "download":
            from potbot.api.operation_result_download import download_result

            owned = service.inspect(actor, args.selection_id, args.job_id)
            operation_id = owned["operationId"]

            class Reader:
                def artifact(
                    self,
                    context: Any,
                    identity: str,
                    job_id: UUID,
                    *,
                    offset: int = 0,
                    length: int = 262144,
                ) -> dict[str, Any]:
                    # The existing downloader validates the inner operation
                    # scope/digest; every range first repeats the selected-job join.
                    page: dict[str, Any] = service.artifact(
                        context, args.selection_id, job_id, offset=offset, length=length
                    )
                    from potbot.domain.errors import StateMismatchError

                    if (
                        page.get("schema") != "potbot-task-execution/v1"
                        or page.get("selectionId") != str(args.selection_id)
                        or page.get("operationId") != identity
                    ):
                        raise StateMismatchError(
                            "Selected artifact range belongs to another binding"
                        )
                    return {
                        **{
                            name: page[name]
                            for name in (
                                "workspaceId",
                                "principalId",
                                "operationId",
                                "artifact",
                                "offset",
                                "dataBase64",
                                "nextOffset",
                            )
                        },
                        "schema": "potbot-operation-workbench/v1",
                    }

            value = download_result(Reader(), actor, operation_id, args.job_id, args.output)
        else:
            value = service.cancel(actor, args.selection_id, args.job_id, args.key)
        print(json.dumps(value, sort_keys=True, ensure_ascii=True))
        return 0
    except Exception as error:
        mutation = args.command in {"check", "run", "cancel"}
        response = error_response(
            error,
            semantics=OperationSemantics(read_only=not mutation, idempotency_key=mutation),
        )
        print(json.dumps(response.body, sort_keys=True, ensure_ascii=True))
        return 1
