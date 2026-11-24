"""Observe, tick, interrupt and explicitly supervise durable atomic task graphs.

A scheduler process only queues *previously checked* Cog operations. It never
claims a native worker or grants a Syn installation new privileges.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import time
from collections.abc import Sequence
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments, database_context


def _interval(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid scheduler interval") from None
    if not math.isfinite(result) or not 0.25 <= result <= 30:
        raise argparse.ArgumentTypeError("interval must be between 0.25 and 30 seconds")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot atomic-graphs", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list owned graph history")
    listing.add_argument("--after")
    listing.add_argument("--limit", type=int, default=25)
    listing.add_argument("--active-only", action="store_true")
    inspect = commands.add_parser("inspect", help="read graph and exact job outcomes")
    inspect.add_argument("graph_id", type=UUID)
    tick = commands.add_parser("tick", help="run one explicitly authorized scheduler pass")
    tick.add_argument("--graphs", type=int, default=20)
    tick.add_argument("--leaves", type=int, default=8)
    tick.add_argument("--after")
    worker = commands.add_parser("worker", help="opt in to continuously tick admitted graphs")
    worker.add_argument("--interval", type=_interval, default=2.0)
    worker.add_argument("--graphs", type=int, default=20)
    worker.add_argument("--leaves", type=int, default=8)
    stop = commands.add_parser("interrupt", help="fence future dispatch and cancel linked jobs")
    stop.add_argument("graph_id", type=UUID)
    stop.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args(argv)

    from potbot.api.operations_cli import _setup_token
    from potbot.controller.atomic_graph_pump import AtomicGraphPump
    from potbot.controller.atomic_graph_runtime import AtomicGraphService
    from potbot.domain.errors import DomainError

    try:
        database, context = database_context(args)
        service = AtomicGraphService(database)
        pump = AtomicGraphPump(service)
        if args.command == "list":
            result = pump.list(
                context, after=args.after, limit=args.limit, active_only=args.active_only
            )
        elif args.command == "inspect":
            result = service.inspect(context, args.graph_id)
        elif args.command == "interrupt":
            if not args.confirm:
                parser.error("interrupt requires --confirm")
            # Stopping needs graph ownership; no operator setup secret is needed
            # beyond the authenticated workspace principal for cancellation.
            result = service.interrupt(context, args.graph_id)
        elif args.command == "tick":
            result = pump.pass_once(
                context,
                setup_token=_setup_token(),
                after=args.after,
                graphs=args.graphs,
                leaves_per_graph=args.leaves,
            )
        else:
            # This is a foreground, explicitly opted-in local service.
            # Termination does not cancel already queued or dispatched jobs.
            stopped = False

            def request_stop(_signal: int, _frame: object) -> None:
                nonlocal stopped
                stopped = True

            for number in (signal.SIGINT, signal.SIGTERM):
                signal.signal(number, request_stop)
            cursor: str | None = None
            while not stopped:
                # Fetch the current operator credential each tick so revocation
                # takes effect without restarting this process.
                result = pump.pass_once(
                    context,
                    setup_token=_setup_token(),
                    after=cursor,
                    graphs=args.graphs,
                    leaves_per_graph=args.leaves,
                )
                cursor = result["next_cursor"]
                events = [
                    {
                        "graph_id": item["graph_id"],
                        "decision": item["decision"],
                        "launched": item["launched"],
                    }
                    for item in result["decisions"]
                    if item["decision"] != "no_dispatch"
                ]
                if events:
                    print(json.dumps({"events": events}, sort_keys=True), flush=True)
                # Bounded sleep allows SIGTERM/CTRL-C to stop promptly.
                deadline = time.monotonic() + args.interval
                while not stopped and time.monotonic() < deadline:
                    time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            return 0
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except DomainError as error:
        # Never print setup tokens, private job inputs, or raw SQL/HTTP bodies.
        print(
            json.dumps(
                {"error": error.code.value if hasattr(error, "code") else type(error).__name__}
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
