#!/usr/bin/env python3
"""S11 host-side publication and one-shot approved tool activation.

Do not run concurrently with the long-lived S7 operator: this command owns
its own watcher and is deliberately scoped to a maintenance window. No HTTP
handler instantiates this worker. Runtime repin is an independently reviewed
operator action; this command never rewrites an operator manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from potbot.domain.errors import DomainError, InvalidInputError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--principal-id", type=UUID, required=True)
    parser.add_argument("--agent-id", type=UUID, required=True)
    parser.add_argument("--operation-id", type=UUID)
    parser.add_argument("--schema", default="potbot")
    parser.add_argument("--proposal-dir", type=Path, required=True)
    parser.add_argument(
        "--plan", type=Path, help="The original independently approved S9 test plan"
    )
    parser.add_argument("--confirm-reconciled", action="store_true")
    parser.add_argument(
        "action",
        choices=(
            "publish",
            "run-once",
            "confirm-pins",
            "postflight",
            "status",
            "reconcile-activation",
        ),
    )
    args = parser.parse_args()
    try:
        from potbot.api.ui_tool_upgrades import UiToolUpgradeStore
        from potbot.credentials import CredentialStore, resolve_postgres_dsn
        from potbot.domain.auth import WorkspaceContext
        from potbot.persistence.database import Database
        from potbot.tools.agent_homes import AgentHomeRegistry
        from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
        from potbot.tools.operator_cli import parse_manifest
        from potbot.tools.shared_tool_store import SharedToolStore
        from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager
        from potbot.tools.tool_approval_worker_s11 import ToolApprovalWorker
        from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

        paths = parse_manifest(args.manifest)
        if args.action != "run-once" and args.operation_id is None:
            raise InvalidInputError("Operation ID required for publish/repin")
        context = WorkspaceContext(paths.workspace_id, args.principal_id)
        database = Database(resolve_postgres_dsn("application"), args.schema)
        approvals = UiToolUpgradeStore(database)
        ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
        homes = AgentHomeRegistry(ledger, workspace_name=args.workspace_name)

        def authorized(workspace: UUID, agent: UUID) -> bool:
            return (
                workspace == paths.workspace_id
                and agent == args.agent_id
                and homes.lookup(workspace, agent) is not None
            )

        store = SharedToolStore(ledger=ledger, shared_root=paths.shared_tools, authorize=authorized)
        if args.action == "status":
            result = approvals.inspect(context, args.operation_id)
        elif args.action == "publish":
            manager = SharedToolUpgradeManager(
                store=store, runtime=paths.runtime(), homes=homes, authorize=authorized
            )
            tested = manager.inspect(
                workspace_id=paths.workspace_id,
                agent_id=args.agent_id,
                operation_id=args.operation_id,
            )
            if tested["state"] != "ready":
                raise InvalidInputError("Only a tested, ready S9 upgrade can be published")
            result = approvals.publish_test(context, agent_id=args.agent_id, tested=tested)
        else:
            with AiWorkspaceOperator(
                workspace_id=paths.workspace_id, ledger=ledger, runtime=paths.runtime()
            ) as operator:
                manager = SharedToolUpgradeManager(
                    store=store,
                    runtime=operator.runtime,
                    homes=homes,
                    authorize=authorized,
                    ai_monitor=operator.monitor,
                )
                worker = ToolApprovalWorker(
                    approvals=approvals,
                    manager=manager,
                    context=context,
                    proposal_dir=args.proposal_dir,
                    agent_id=args.agent_id,
                    setup_token=CredentialStore().get("api.setup_token"),
                )
                if args.action == "run-once":
                    result = worker.run_once()
                elif args.action == "confirm-pins":
                    result = worker.confirm_repin(
                        operation=args.operation_id, agent_id=args.agent_id
                    )
                elif args.action == "reconcile-activation":
                    result = worker.release_authority().reconcile_activation(
                        operation=args.operation_id, confirm=args.confirm_reconciled
                    )
                else:
                    if args.plan is None:
                        raise InvalidInputError(
                            "Postflight requires the original approved test plan"
                        )
                    from tool_upgrade_s9 import _plan

                    _, suite, _ = _plan(args.plan)
                    authority = worker.release_authority()
                    operator.bind_tool_release_authority(authority)
                    result = authority.postflight(operation=args.operation_id, suite=suite)
        print(json.dumps({"schema": "potbot-s11-worker/v1", "result": result}, sort_keys=True))
        return 0
    except Exception as exc:
        code = exc.code.value if isinstance(exc, DomainError) else "internal_error"
        print(json.dumps({"schema": "potbot-s11-worker/v1", "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
