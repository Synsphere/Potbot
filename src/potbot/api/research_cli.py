"""Review, run and inspect configured external research and exact cited evidence."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot research", description=__doc__)
    add_scope_arguments(parser)
    parser.add_argument(
        "--policy", type=Path, help="exact operator policy file; otherwise POTBOT_RESEARCH_POLICY"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    context = commands.add_parser(
        "context", help="read the current task, need and configured disclosure bounds"
    )
    need = commands.add_parser(
        "need-create", help="record an information need for the selected task"
    )
    admit = commands.add_parser(
        "admit", help="retain a reviewed research action without making a request"
    )
    run = commands.add_parser(
        "run", help="reserve and queue one explicitly approved external action"
    )
    runs = commands.add_parser("runs", help="list workspace-shared research runs")
    inspect = commands.add_parser("inspect", help="reopen a retained research run")
    events = commands.add_parser("events", help="read bounded research progress")
    result = commands.add_parser("result", help="audit retained source and execution evidence")
    source = commands.add_parser("source", help="read one authenticated source extraction")
    citation = commands.add_parser(
        "cite", help="derive an exact UTF-8 source quote without external requests"
    )
    compare = commands.add_parser(
        "compare", help="retain a reviewed cited-source agreement or conflict report"
    )
    comparison = commands.add_parser(
        "comparison", help="independently read back a retained comparison"
    )
    cancel = commands.add_parser(
        "cancel", help="cancel queued/running research without replaying a request"
    )
    worker = commands.add_parser(
        "worker-once", help="process one admitted run through the native configured worker"
    )
    for command in (context, need, admit, compare):
        command.add_argument("--episode", required=True, type=UUID)
    for command in (context, need, admit):
        command.add_argument("--state", required=True, type=UUID)
    context.add_argument("--need", type=UUID)
    for command in (admit, compare):
        command.add_argument("--need", required=True, type=UUID)
        command.add_argument("--context-sha256", required=True)
    for command in (need, admit, run, compare, cancel):
        command.add_argument(
            "--key", required=True, help="reuse the exact original key after a lost response"
        )
        command.add_argument("--confirm", required=True, action="store_true")
    for command in (need, admit, citation, compare):
        command.add_argument("--input", required=True, type=Path)
    run.add_argument("--action", required=True, type=UUID)
    run.add_argument("--policy-sha256", required=True)
    run.add_argument("--disclosure-confirmed", required=True, action="store_true")
    runs.add_argument("--episode", type=UUID)
    runs.add_argument("--limit", type=int, default=25)
    runs.add_argument("--after")
    for command in (inspect, events, result, source, cancel):
        command.add_argument("run_id", type=UUID)
    source.add_argument("source_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    comparison.add_argument("evidence_id", type=UUID)
    worker.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)

    # All command help works without optional PostgreSQL/HTTP/native imports.
    from potbot.api.jobs_cli import _read_object, database_context
    from potbot.api.wire import OperationSemantics, error_response
    from potbot.controller.research_workbench import ResearchWorkbench
    from potbot.domain.errors import InvalidInputError
    from potbot.research.source_policy import ExternalResearchPolicy

    try:
        value: dict[str, Any] | None
        database, actor = database_context(args)
        policy = ExternalResearchPolicy.read(args.policy) if args.policy else None
        service = ResearchWorkbench(database, policy=policy)
        token = None
        if args.command in {"need-create", "admit", "run", "compare"}:
            from potbot.api.operations_cli import _setup_token

            token = _setup_token()
        if args.command == "context":
            value = service.context(actor, args.episode, args.state, args.need)
        elif args.command == "need-create":
            assert token is not None
            value = service.create_need(
                actor,
                args.episode,
                args.state,
                _read_object(args.input, maximum=65_536),
                args.key,
                setup_token=token,
            )
        elif args.command == "admit":
            assert token is not None
            value = service.admit(
                actor,
                args.episode,
                args.state,
                args.need,
                args.context_sha256,
                _read_object(args.input, maximum=65_536),
                args.key,
                setup_token=token,
            )
        elif args.command == "run":
            assert token is not None
            value = service.queue(
                actor,
                args.action,
                args.policy_sha256,
                args.key,
                setup_token=token,
                disclosure_confirmed=args.disclosure_confirmed,
            )
        elif args.command == "runs":
            value = service.list_runs(
                actor, episode_id=args.episode, limit=args.limit, after=args.after
            )
        elif args.command == "inspect":
            value = service.inspect(actor, args.run_id)
        elif args.command == "events":
            value = service.events(actor, args.run_id, after=args.after, limit=args.limit)
        elif args.command == "result":
            value = service.result(actor, args.run_id)
        elif args.command == "source":
            value = service.source(actor, args.run_id, args.source_id)
        elif args.command == "cite":
            value = service.citation(actor, _read_object(args.input, maximum=2048))
        elif args.command == "compare":
            assert token is not None
            request = _read_object(args.input, maximum=65_536)
            if set(request) != {"claims"}:
                raise InvalidInputError("Comparison input must contain exactly the claims array")
            value = service.compare(
                actor,
                args.episode,
                args.need,
                args.context_sha256,
                request["claims"],
                args.key,
                setup_token=token,
            )
        elif args.command == "comparison":
            value = service.comparison(actor, args.evidence_id)
        elif args.command == "cancel":
            value = service.cancel(actor, args.run_id, args.key)
        else:
            from potbot.controller.research_external_worker import ExternalResearchWorker

            policy_provider = (
                (lambda: ExternalResearchPolicy.read(args.policy))
                if args.policy
                else service.policy
            )
            value = ExternalResearchWorker(
                service.runs(actor), service.policy(), policy_provider=policy_provider
            ).run_once(actor, args.worker_id)
        print(json.dumps(value, sort_keys=True, ensure_ascii=True))
        return 0
    except Exception as error:
        mutation = args.command in {
            "need-create",
            "admit",
            "run",
            "compare",
            "cancel",
            "worker-once",
        }
        response = error_response(
            error,
            semantics=OperationSemantics(
                read_only=not mutation, idempotency_key=mutation and args.command != "worker-once"
            ),
        )
        print(json.dumps(response.body, sort_keys=True, ensure_ascii=True))
        return 1
