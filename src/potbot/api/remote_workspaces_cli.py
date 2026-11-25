"""Native probe/plan commands and an explicit operator-only revocation handoff."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.remote_workspaces import RemoteNode
from potbot.tools.remote_preflight import (
    HostObservation,
    ReleasePin,
    observation_receipt,
    provisioning_manifest,
)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInputError("Duplicate JSON keys are not allowed in reviewed plans.")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise InvalidInputError("Nonfinite JSON values are not allowed in reviewed plans.")


def _public_uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise InvalidInputError("Public workspace and node identities must be UUID strings.")
    return UUID(value)


def plan_from_file(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        raw = source.read(65_537)
    if len(raw) > 65_536:
        raise InvalidInputError("A provisioning plan input cannot exceed 64 KiB.")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    required = {"node", "observation", "releases", "target_workspace_id", "project_root"}
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - {"minimum_disk_bytes"}
    ):
        raise InvalidInputError(
            "Plan input must contain exactly the documented public planning fields."
        )
    registration = dict(value["node"])
    for key in ("id", "workspace_id", "owner_principal_id"):
        registration[key] = _public_uuid(registration[key])
    node = RemoteNode(**registration)
    observation = HostObservation(**value["observation"])
    if not isinstance(value["releases"], list):
        raise InvalidInputError("Selected releases must be a JSON array.")
    releases = tuple(ReleasePin(**item) for item in value["releases"])
    return provisioning_manifest(
        node,
        observation,
        releases,
        target_workspace_id=_public_uuid(value["target_workspace_id"]),
        project_root=value["project_root"],
        minimum_disk_bytes=value.get("minimum_disk_bytes", 1_073_741_824),
    )


def main(argv: Sequence[str] | None = None) -> int:
    if argv and argv[0] == "supervise-node":
        from potbot.tools.node_supervisor import main as supervise

        return supervise(argv[1:])
    parser = argparse.ArgumentParser(prog="potbot remote-workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="print native facts on this host without changing it")
    commands.add_parser("supervise-node", help="manage one native node worker service")
    plan = commands.add_parser("plan", help="build a deterministic plan from supplied public facts")
    plan.add_argument("--input", type=Path, required=True)
    revoke = commands.add_parser(
        "apply-revocation", help="apply one reviewed intent using native admin credentials"
    )
    revoke.add_argument("--workspace-id", type=UUID, required=True)
    revoke.add_argument("--owner-principal-id", type=UUID, required=True)
    revoke.add_argument("--request-id", type=UUID, required=True)
    revoke.add_argument("--schema", default="potbot")
    revoke.add_argument("--confirm", required=True, action="store_true")
    inventory = commands.add_parser(
        "inventory", help="inspect explicit project roots without loading or executing"
    )
    inventory.add_argument("--profile", type=Path, required=True)
    inventory.add_argument("--workspace-id", type=UUID, required=True)
    inventory.add_argument("--kind", choices=("models", "tools"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "apply-revocation":
            from potbot.controller.swarm_requests import apply_revocation
            from potbot.credentials import resolve_postgres_dsn
            from potbot.persistence.database import admin_connection

            with admin_connection(resolve_postgres_dsn("admin"), args.schema) as connection:
                result = apply_revocation(
                    connection,
                    workspace_id=args.workspace_id,
                    owner_principal_id=args.owner_principal_id,
                    request_id=args.request_id,
                )
        elif args.command == "inventory":
            from potbot.tools.local_inventory import InventoryProfile, inspect_local

            result = inspect_local(
                InventoryProfile.read(args.profile, args.workspace_id), args.kind
            )
        else:
            result = (
                observation_receipt() if args.command == "probe" else plan_from_file(args.input)
            )
        print(json.dumps({"result": result}, sort_keys=True, allow_nan=False))
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.to_dict()}, sort_keys=True))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        print(
            json.dumps(
                {
                    "error": {
                        "code": "invalid_input",
                        "message": "The public provisioning input is invalid or unavailable.",
                    }
                },
                sort_keys=True,
            )
        )
    except Exception:
        # Native DB/credential libraries may carry private connection diagnostics.
        print(
            json.dumps(
                {
                    "error": {
                        "code": "capability_unavailable",
                        "message": "Native operator revocation unavailable; no success confirmed.",
                    }
                }
            )
        )
    return 1
