"""Inspect operation readiness without starting workers or discovering providers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from potbot.api.jobs_cli import add_scope_arguments


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
    args = parser.parse_args(argv)

    # Keep --help usable without PostgreSQL, native credentials or web extras.
    from potbot.api.jobs_cli import database_context
    from potbot.api.wire import OperationSemantics, error_response
    from potbot.controller.operation_catalogue import OperationCatalogue

    try:
        database, actor = database_context(args)
        catalogue = OperationCatalogue(database)
        if args.command == "list":
            result = catalogue.list(
                actor,
                query=args.query,
                kind=args.kind,
                state=args.state,
                after=args.after,
                limit=args.limit,
            )
        else:
            result = catalogue.inspect(actor, args.operation_id)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except Exception as error:
        response = error_response(error, semantics=OperationSemantics(read_only=True))
        print(json.dumps(response.body, sort_keys=True, ensure_ascii=True))
        return 1
