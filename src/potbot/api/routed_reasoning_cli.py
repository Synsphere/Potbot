"""Run or recover an operator-selected checked reasoning investigation."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot reasoning", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="freeze a checked plan without queuing work")
    start.add_argument("--plan", type=Path, required=True)
    commands.add_parser("inspect", help="inspect original phase and job identities")
    commands.add_parser("advance", help="take one controller step through the ordinary job queue")
    commands.add_parser("result", help="read the retained completed synthesis")
    stop = commands.add_parser("interrupt", help="fence dispatch and request cancellation")
    stop.add_argument("--confirm", action="store_true", required=True)
    follow = commands.add_parser(
        "follow", help="advance this run until it reaches a terminal state"
    )
    follow.add_argument("--interval", type=float, default=1.0)
    for command in (
        start,
        commands.choices["inspect"],
        commands.choices["advance"],
        commands.choices["result"],
        stop,
        follow,
    ):
        command.add_argument("run_id", type=UUID)
    args = parser.parse_args(argv)
    if args.command == "follow" and not 0.2 <= args.interval <= 30:
        parser.error("--interval must be between 0.2 and 30 seconds")

    from potbot.api.jobs_cli import _read_object, database_context
    from potbot.api.operations_cli import _setup_token
    from potbot.api.wire import OperationSemantics, error_response
    from potbot.controller.routed_reasoning import RoutedReasoning

    def emit(value: object) -> None:
        print(json.dumps(value, sort_keys=True, ensure_ascii=True), flush=True)

    try:
        database, actor = database_context(args)
        service = RoutedReasoning(database)
        run_id = args.run_id
        if args.command == "start":
            result = service.start(actor, run_id, _read_object(args.plan, maximum=32768))
        elif args.command == "inspect":
            result = service.inspect(actor, run_id)
        elif args.command == "result":
            result = service.result(actor, run_id)
        elif args.command == "interrupt":
            result = service.interrupt(actor, run_id)
        elif args.command == "advance":
            result = service.advance(actor, run_id, setup_token=_setup_token())
        else:
            previous = None
            while True:
                result = service.inspect(actor, run_id)
                if result["state"] == "active":
                    result = service.advance(actor, run_id, setup_token=_setup_token())
                if result != previous:
                    emit(result)
                    previous = result
                if result["state"] != "active":
                    return 0 if result["state"] == "completed" else 2
                time.sleep(args.interval)
        emit(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        read_only = args.command in {"inspect", "result"}
        response = error_response(
            error, semantics=OperationSemantics(read_only=read_only, idempotency_key=not read_only)
        )
        emit(response.body)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
