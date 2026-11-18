"""Review executable acquisition and run its one-shot native host phases."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments

_CONTROLS = (
    "approve-build",
    "approve",
    "reject",
    "rollback",
    "reconcile",
    "repin",
    "postflight",
    "cancel",
)


def _host_scope(command: argparse.ArgumentParser) -> None:
    command.add_argument("--manifest", type=Path, required=True)
    command.add_argument("--workspace-name", required=True)
    command.add_argument("--agent-id", type=UUID, required=True)
    command.add_argument(
        "--recipe-dir",
        type=Path,
        required=True,
        help="private controller recipe directory, outside guest mounts",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("recipes", "list"):
        page = commands.add_parser(name, help="list the current principal's reviewed tool records")
        page.add_argument("--limit", type=int, default=25)
        page.add_argument("--after")
    publish = commands.add_parser(
        "publish-recipe", help="publish and retain a private operator recipe"
    )
    publish.add_argument("recipe", type=Path)
    _host_scope(publish)
    publish.add_argument("--key", required=True)
    publish.add_argument("--confirm", action="store_true", required=True)
    request = commands.add_parser(
        "request", help="request exact public Git source under a published recipe"
    )
    request.add_argument("recipe_id", type=UUID)
    request.add_argument("--commit", required=True)
    request.add_argument("--key", required=True)
    request.add_argument("--confirm", action="store_true", required=True)
    inspect = commands.add_parser("inspect", help="inspect the current evidence and next actions")
    inspect.add_argument("operation_id", type=UUID)
    events = commands.add_parser("events", help="read bounded lifecycle history")
    events.add_argument("operation_id", type=UUID)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=50)
    for action in _CONTROLS:
        control = commands.add_parser(action, help="record an exact reviewed lifecycle decision")
        control.add_argument("operation_id", type=UUID)
        control.add_argument("--revision", type=int, required=True)
        control.add_argument(
            "--digest",
            required=True,
            help="reviewSha256, evidenceSha256, or bindingSha256 from inspect",
        )
        control.add_argument(
            "--key", required=True, help="reuse this key/body after a lost response"
        )
        control.add_argument("--confirm", action="store_true", required=True)
    worker = commands.add_parser(
        "run-once", help="run one eligible approved phase on a qualified native host"
    )
    _host_scope(worker)
    worker.add_argument("--phase", choices=("source", "build", "promote", "action"), required=True)
    worker.add_argument(
        "--operation-id", type=UUID, help="advance only this exact reviewed operation"
    )
    worker.add_argument("--source-dir", type=Path, required=True)
    worker.add_argument("--evidence-dir", type=Path, required=True)
    worker.add_argument("--proposal-dir", type=Path, required=True)
    worker.add_argument("--git", type=Path, required=True, help="exact trusted host Git executable")
    worker.add_argument(
        "--git-sha256", required=True, help="independently reviewed host Git SHA-256"
    )
    return parser


def _token() -> str:
    from potbot.credentials import CredentialStore
    from potbot.domain.errors import ScopeDeniedError

    value = CredentialStore().get("api.setup_token")
    if not isinstance(value, str) or len(value) < 32:
        raise ScopeDeniedError("Tool acquisition requires an independent native setup capability")
    return value


def _host(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    from potbot.domain.errors import ScopeDeniedError
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.operator_cli import parse_manifest
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

    paths = parse_manifest(args.manifest)
    if paths.workspace_id != args.workspace_id:
        raise ScopeDeniedError("Tool acquisition manifest belongs to another workspace")
    ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
    homes = AgentHomeRegistry(ledger, workspace_name=args.workspace_name)
    if homes.lookup(paths.workspace_id, args.agent_id) is None:
        raise ScopeDeniedError("Tool acquisition requires a registered agent")
    return paths, ledger, homes


def _run_host(args: argparse.Namespace, service: Any, context: Any, token: str) -> dict[str, Any]:
    from potbot.api.ui_tool_upgrades import UiToolUpgradeStore
    from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
    from potbot.tools.shared_tool_store import SharedToolStore
    from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager
    from potbot.tools.tool_acquisition_worker import ToolAcquisitionWorker
    from potbot.tools.tool_approval_worker_s11 import ToolApprovalWorker
    from potbot.tools.tool_sources import GitToolSourceStore

    paths, ledger, homes = _host(args)

    def authorized(workspace: UUID, agent: UUID) -> bool:
        return (
            workspace == context.workspace_id
            and agent == args.agent_id
            and homes.lookup(workspace, agent) is not None
        )

    store = SharedToolStore(
        ledger=ledger,
        shared_root=paths.shared_tools,
        authorize=authorized,
        git_executable=str(args.git),
    )
    sources = GitToolSourceStore(
        root=args.source_dir, git_executable=args.git, git_sha256=args.git_sha256
    )
    with AiWorkspaceOperator(
        workspace_id=context.workspace_id, ledger=ledger, runtime=paths.runtime()
    ) as operator:
        manager = SharedToolUpgradeManager(
            store=store,
            runtime=operator.runtime,
            homes=homes,
            authorize=authorized,
            ai_monitor=operator.monitor,
        )
        promotion = ToolApprovalWorker(
            approvals=UiToolUpgradeStore(service.database),
            manager=manager,
            context=context,
            proposal_dir=args.proposal_dir,
            agent_id=args.agent_id,
            setup_token=token,
        )
        worker = ToolAcquisitionWorker(
            service=service,
            context=context,
            agent_id=args.agent_id,
            setup_token=token,
            sources=sources,
            recipe_dir=args.recipe_dir,
            evidence_dir=args.evidence_dir,
            manager=manager,
            homes=homes,
            operator=operator,
            promotion=promotion,
        )
        return worker.run_once(args.phase, operation_id=args.operation_id)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from potbot.api.jobs_cli import database_context
        from potbot.domain.errors import ScopeDeniedError
        from potbot.tools.tool_acquisition import ToolAcquisitionService, load_tool_recipe

        database, context = database_context(args)
        service = ToolAcquisitionService(database)
        command = args.command
        token = (
            _token() if command in {*_CONTROLS, "publish-recipe", "request", "run-once"} else None
        )
        result: dict[str, Any]
        if command == "recipes":
            result = service.recipes(context, limit=args.limit, after=args.after)
        elif command == "list":
            result = service.list(context, limit=args.limit, after=args.after)
        elif command == "inspect":
            result = service.inspect(context, args.operation_id)
        elif command == "events":
            result = service.events(context, args.operation_id, after=args.after, limit=args.limit)
        else:
            assert token is not None
            if command == "publish-recipe":
                from potbot.tools.tool_acquisition_worker import retain_recipe

                paths, _, _ = _host(args)
                for guest in (paths.home, paths.shared_tools):
                    if (
                        args.recipe_dir == guest
                        or args.recipe_dir.is_relative_to(guest)
                        or guest.is_relative_to(args.recipe_dir)
                    ):
                        raise ScopeDeniedError(
                            "Operator recipes must be retained outside guest mounts"
                        )
                recipe = load_tool_recipe(args.recipe)
                retain_recipe(recipe, args.recipe_dir)
                result = service.publish_recipe(
                    context, recipe, args.agent_id, args.key, setup_token=token
                )
            elif command == "request":
                result = service.request(
                    context, args.recipe_id, args.commit, args.key, setup_token=token
                )
            elif command == "run-once":
                result = _run_host(args, service, context, token)
            elif command == "approve-build":
                result = service.approve_build(
                    context,
                    args.operation_id,
                    args.revision,
                    args.digest,
                    args.key,
                    setup_token=token,
                )
            elif command in {"approve", "reject"}:
                result = service.decide(
                    context,
                    args.operation_id,
                    args.revision,
                    args.digest,
                    command == "approve",
                    args.key,
                    setup_token=token,
                )
            else:
                result = service.action(
                    context,
                    args.operation_id,
                    command,
                    args.revision,
                    args.digest,
                    args.key,
                    setup_token=token,
                )
        print(json.dumps(result, sort_keys=True))
        operation = result.get("operation", {})
        return (
            1
            if isinstance(operation, dict) and operation.get("phase") in {"failed", "indeterminate"}
            else 0
        )
    except Exception as exc:
        from potbot.domain.errors import DomainError

        code = exc.code.value if isinstance(exc, DomainError) else "internal_error"
        print(
            json.dumps({"schema": "potbot-tool-acquisition-cli/v1", "error": code}), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
