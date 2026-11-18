"""Reviewed source/build admission using the existing S11/S12 operation owner.

HTTP callers queue signed decisions only. Host workers independently reread
private recipes, exact Git source and S9/S10/S12 evidence before any effect.
This service neither executes a command nor grants an installed Cog permission.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from potbot.context.service import native_skill_setup_token
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database, idempotent_command
from potbot.tools.shared_tool_store import SharedToolDefinition
from potbot.tools.tool_acquisition_recipe import ToolRecipe as ToolRecipe
from potbot.tools.tool_acquisition_recipe import load_tool_recipe as load_tool_recipe
from potbot.tools.tool_sources import source_identity
from potbot.tools.tool_stage_s10 import command_digest

SCHEMA = "potbot-tool-acquisitions/v1"
PAGE_BYTES = 1_572_864
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DENIED = "Tool acquisition is unavailable in this principal and workspace scope"
_HOST_ACTIONS = {"rollback", "reconcile", "repin", "postflight"}


def _uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("Tool acquisition requires an exact UUID")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise InvalidInputError("Tool acquisition requires an exact SHA-256")
    return value


def _number(value: object, maximum: int = 2**53 - 2) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise InvalidInputError("Tool acquisition requires a bounded integer")
    return value


def _token_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 4096
        and all(33 <= ord(char) <= 126 for char in value)
    )


def _key(context: WorkspaceContext, key: str) -> str:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise InvalidInputError("A stable bounded acquisition command key is required")
    return content_hash([str(context.principal_id), key])


def _sign(token: str, purpose: str, value: dict[str, Any]) -> str:
    return hmac.new(
        token.encode("ascii"), canonical_json({"schema": purpose, **value}).encode(), hashlib.sha256
    ).hexdigest()


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_id": str(row["workspace_id"]),
        "id": str(row["id"]),
        "principal_id": str(row["principal_id"]),
        "agent_id": str(row["agent_id"]),
    }


def _recipe_signature(row: dict[str, Any], token: str) -> str:
    return _sign(
        token,
        "potbot-tool-recipe-approved/v1",
        {**_identity(row), "recipe_sha256": row["recipe_sha256"]},
    )


def _request_signature(row: dict[str, Any], token: str) -> str:
    return _sign(
        token,
        "potbot-tool-source-request/v1",
        {
            **_identity(row),
            "recipe_id": str(row["recipe_id"]),
            "recipe_sha256": row["recipe_sha256"],
            "slug": row["slug"],
            "commit": row["new_commit"],
            "revision": 1,
        },
    )


def _build_signature(row: dict[str, Any], authorization: dict[str, Any], token: str) -> str:
    return _sign(
        token,
        "potbot-tool-build-approved/v1",
        {
            **_identity(row),
            "recipe_sha256": row["recipe_sha256"],
            "review_sha256": row["review_sha256"],
            "source_sha256": row["source_receipt"]["source_sha256"],
            "revision": authorization["revision"],
            "approver": authorization["principal_id"],
        },
    )


def _action_signature(row: dict[str, Any], action: dict[str, Any], token: str) -> str:
    return _sign(
        token,
        "potbot-tool-host-action/v1",
        {
            **_identity(row),
            "action": action["action"],
            "revision": action["revision"],
            "binding_sha256": action["binding_sha256"],
            "approver": action["principal_id"],
            "acquisition_phase": action["acquisition_phase"],
            "operation_state": action["operation_state"],
            "abandon_only": action["abandon_only"],
            "recipe_sha256": row["recipe_sha256"],
            "new_commit": row["new_commit"],
            "source_sha256": content_hash(row["source_receipt"]),
            "review_sha256": row["review_sha256"],
        },
    )


def _row(
    conn: Any, context: WorkspaceContext, operation: UUID, *, lock: bool = False
) -> dict[str, Any]:
    result = conn.execute(
        "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND id=%s "
        "AND principal_id=%s AND acquisition_phase!='none'" + (" FOR UPDATE" if lock else ""),
        (context.workspace_id, _uuid(operation), context.principal_id),
    ).fetchone()
    if result is None:
        raise ScopeDeniedError(_DENIED)
    return dict(result)


def _recipe_row(conn: Any, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ui_tool_acquisition_recipes WHERE workspace_id=%s AND id=%s AND principal_id=%s",
        (context.workspace_id, _uuid(identity), context.principal_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError(_DENIED)
    result = dict(row)
    recipe = ToolRecipe.from_dict(result["content"])
    if recipe.digest != result["recipe_sha256"]:
        raise StateMismatchError("The retained operator recipe identity changed")
    return result


def _verified_recipe(row: dict[str, Any], token: str | None) -> ToolRecipe:
    if not _token_valid(token) or not hmac.compare_digest(
        row["approval_mac"], _recipe_signature(row, str(token))
    ):
        raise ScopeDeniedError("The recipe requires current independent operator approval")
    recipe = ToolRecipe.from_dict(row["content"])
    if recipe.digest != row["recipe_sha256"]:
        raise StateMismatchError("The operator recipe no longer matches its exact identity")
    return recipe


def _source(value: dict[str, Any], row: dict[str, Any], recipe: ToolRecipe) -> dict[str, Any]:
    fields = {
        "schema",
        "operation_id",
        "repository",
        "commit",
        "git_tree",
        "tree_sha256",
        "files",
        "bytes",
        "source_sha256",
        "storage_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema"] != "potbot-tool-source/v1"
    ):
        raise InvalidInputError("An exact independently retained Git source receipt is required")
    if (
        value["operation_id"] != str(row["id"])
        or value["repository"] != recipe.repository
        or value["commit"] != row["new_commit"]
    ):
        raise ScopeDeniedError("Source receipt differs from the requested operation")
    source_identity(value["repository"], value["commit"])
    if not isinstance(value["git_tree"], str) or not _COMMIT.fullmatch(value["git_tree"]):
        raise InvalidInputError("Source receipt has no exact Git tree identity")
    for field in ("tree_sha256", "source_sha256", "storage_sha256"):
        _sha(value[field])
    if not 1 <= _number(value["files"], 8192) or not 0 <= _number(value["bytes"], 128 * 1024**2):
        raise InvalidInputError("Source receipt exceeds its inventory allowance")
    identity = {
        key: value[key]
        for key in ("repository", "commit", "git_tree", "tree_sha256", "files", "bytes")
    }
    expected = hashlib.sha256(
        (
            json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode()
    ).hexdigest()
    if expected != value["source_sha256"]:
        raise StateMismatchError("Source receipt digest does not match its inventory")
    return dict(value)


def _execution(value: dict[str, Any], row: dict[str, Any], recipe: ToolRecipe) -> dict[str, Any]:
    fields = {
        "agent_directory",
        "build_root",
        "build_argv",
        "command_sha256",
        "image_digest",
        "qualification_sha256",
        "lockfiles",
        "old_definition",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidInputError("An exact observed execution contract is required")
    rendered = recipe.render(operation_id=row["id"], agent_directory=value["agent_directory"])
    if any(value[key] != expected for key, expected in rendered.items()):
        raise ScopeDeniedError("The rendered build command differs from the private recipe")
    _sha(value["image_digest"])
    _sha(value["qualification_sha256"])
    locks = value["lockfiles"]
    if not isinstance(locks, list) or len(locks) != len(recipe.lockfile_paths):
        raise InvalidInputError("Execution contract lacks the exact dependency-lock inventory")
    checked = []
    for item in locks:
        if not isinstance(item, list | tuple) or len(item) != 2 or not isinstance(item[0], str):
            raise InvalidInputError("Dependency locks require exact path and digest pairs")
        checked.append([item[0], _sha(item[1])])
    if [item[0] for item in checked] != list(recipe.lockfile_paths):
        raise ScopeDeniedError("Dependency lock paths differ from the admitted recipe")
    old = value["old_definition"]
    if old is not None:
        if not isinstance(old, dict):
            raise InvalidInputError(
                "Predecessor must be an observed registered definition or absence"
            )
        definition = SharedToolDefinition.from_dict(old)
        if (
            canonical_json(definition.to_dict()) != canonical_json(old)
            or definition.slug != recipe.slug
            or definition.repository != recipe.repository
            or definition.commit == row["new_commit"]
        ):
            raise ScopeDeniedError(
                "Acquisition cannot replace an unrelated publisher or identical revision"
            )
    return {**value, "lockfiles": checked}


def _review(row: dict[str, Any], recipe: ToolRecipe) -> dict[str, Any] | None:
    if row.get("source_receipt") is None or row.get("execution_contract") is None:
        return None
    source = _source(row["source_receipt"], row, recipe)
    execution = _execution(row["execution_contract"], row, recipe)
    old = execution["old_definition"]
    result = {
        "kind": "install" if old is None else "upgrade",
        "oldCommit": old["commit"] if old else None,
        "newCommit": row["new_commit"],
        "sourceSha256": source["source_sha256"],
        "recipeSha256": recipe.digest,
        "commandSha256": execution["command_sha256"],
        "imageDigest": execution["image_digest"],
        "qualificationSha256": execution["qualification_sha256"],
        "buildRoot": execution["build_root"],
        "buildArgv": execution["build_argv"],
        "lockfiles": execution["lockfiles"],
        "network": "denied",
        "automaticActivation": False,
        "requiresPostflight": True,
    }
    digest = content_hash(
        {
            **_identity(row),
            "recipe_id": str(row["recipe_id"]),
            "source": source,
            "execution": execution,
        }
    )
    return {"sha256": digest, **result}


def _binding(row: dict[str, Any]) -> str:
    return content_hash(
        {
            **_identity(row),
            "recipe_sha256": row["recipe_sha256"],
            "revision": row["revision"],
            "phase": row["acquisition_phase"],
            "state": row["state"],
            "review_sha256": row["review_sha256"],
            "evidence_sha256": row["evidence_sha256"],
            "release_phase": row["release_phase"],
            "release_generation": row["release_generation"],
            "release_binding": row["release_binding"],
            "release_evidence": row["release_evidence"],
            "host_action": row["host_action"],
        }
    )


def _allowed(row: dict[str, Any], valid: bool) -> list[str]:
    host = row.get("host_action")
    if host and host["status"] == "queued":
        return []
    if host and host["status"] == "running":
        if row["release_phase"] in {"released", "withdrawn"}:
            return ["reconcile"] if valid else []
        if row["release_phase"] == "testing" or host["action"] == "postflight":
            return ["rollback"] if valid else []
        return ["reconcile"] if valid or _abandon_action(host) else []
    phase = row["acquisition_phase"]
    if phase in {"sourcing", "building", "indeterminate"}:
        return ["reconcile"]
    if row["acquisition_worker_token"] is not None:
        return []
    if phase in {"source_queued", "review", "build_queued", "failed"}:
        return (["approve-build"] if phase == "review" and valid else []) + ["cancel"]
    if phase != "tested":
        return []
    state, released = row["state"], row["release_phase"]
    if state == "awaiting_approval":
        return ["approve", "reject"] if valid else ["cancel"]
    if state == "approved":
        return ["cancel"]
    if state == "rejected":
        return ["reconcile"]
    if not valid:
        return []
    if state in {"executing", "indeterminate"}:
        return ["reconcile"] if released == "none" else ["rollback"]
    if state in {"awaiting_repin", "active", "rolled_back"}:
        if released == "withdrawn":
            return []
        result = ["rollback"] if state != "rolled_back" else []
        if released == "none":
            result.extend(["repin", "reconcile"])
        elif released == "repinned":
            result.append("postflight")
        return result
    return []


def _abandon_action(host: Any) -> bool:
    return (
        isinstance(host, dict)
        and host.get("action") == "reconcile"
        and host.get("abandon_only") is True
        and host.get("operation_state")
        in {"acquiring", "awaiting_approval", "approved", "rejected"}
        and host.get("acquisition_phase") != "none"
    )


def _pagination(limit: int, after: str | None) -> UUID | None:
    if not 1 <= _number(limit, 50):
        raise InvalidInputError("Acquisition pages require one to fifty entries")
    if after is None:
        return None
    try:
        value = UUID(after)
        if str(value) != after:
            raise ValueError
        return value
    except (TypeError, ValueError, AttributeError):
        raise InvalidInputError("Invalid acquisition page cursor") from None


class ToolAcquisitionService:
    def __init__(
        self, database: Database, *, setup_token_resolver: Callable[[], str | None] | None = None
    ):
        self.database = database
        self.setup_token_resolver = setup_token_resolver or native_skill_setup_token

    @staticmethod
    def _scope(context: WorkspaceContext) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        }

    def _token(self) -> str | None:
        try:
            value = self.setup_token_resolver()
            return value if _token_valid(value) else None
        except Exception:
            return None

    def _authorized(self, presented: str) -> str:
        current = self._token()
        if (
            not _token_valid(presented)
            or current is None
            or not hmac.compare_digest(presented, current)
        ):
            raise ScopeDeniedError("Current independent operator setup authorization is required")
        return current

    @staticmethod
    def _audit(
        conn: Any,
        context: WorkspaceContext,
        row: dict[str, Any],
        action: str,
        prior: str,
        evidence: str,
    ) -> None:
        from potbot.api.ui_tool_upgrades import UiToolUpgradeStore

        UiToolUpgradeStore._audit(
            conn, context, row["id"], action, prior, row["state"], row["revision"], evidence
        )

    @staticmethod
    def _public_recipe(row: dict[str, Any], token: str | None) -> dict[str, Any]:
        recipe = ToolRecipe.from_dict(row["content"])
        valid = True
        try:
            _verified_recipe(row, token)
        except ScopeDeniedError:
            valid = False
        return {
            "id": str(row["id"]),
            "name": recipe.name,
            "description": recipe.description,
            "slug": recipe.slug,
            "repository": recipe.repository,
            "recipeSha256": recipe.digest,
            "agentId": str(row["agent_id"]),
            "ownerPrincipalId": str(row["principal_id"]),
            "createdAt": row["created_at"].isoformat(),
            "build": {
                "commandSha256": command_digest(recipe.build_argv),
                "timeoutMs": recipe.timeout_ms,
                "network": "denied",
            },
            "tests": [
                {"name": case.name, "kind": case.kind, "timeoutMs": case.timeout_ms}
                for case in recipe.tests.cases
            ],
            "cleanupPaths": list(recipe.cleanup_paths),
            "lockfilePaths": list(recipe.lockfile_paths),
            "artifactPaths": list(recipe.artifact_paths),
            "approvalValid": valid,
        }

    def _public(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        recipe_row = _recipe_row(conn, context, row["recipe_id"])
        recipe = ToolRecipe.from_dict(recipe_row["content"])
        review = _review(row, recipe)
        if review is not None and review["sha256"] != row["review_sha256"]:
            raise StateMismatchError("The reviewed source/build binding changed")
        valid = self._public_recipe(recipe_row, token)["approvalValid"]
        if valid:
            valid = hmac.compare_digest(
                row["source_request_mac"], _request_signature(row, str(token))
            )
        phase = row["acquisition_phase"]
        if phase == "tested":
            phase = row["state"]
            if row["release_phase"] in {
                "repinned",
                "testing",
                "withdrawn",
                "failed",
                "indeterminate",
            }:
                phase = row["release_phase"]
        installed = "unknown"
        if row["acquisition_kind"] == "install" and row["state"] in {
            "acquiring",
            "awaiting_approval",
            "approved",
            "rejected",
        }:
            installed = "absent"
        elif row["state"] in {
            "executing",
            "awaiting_repin",
            "active",
            "indeterminate",
            "rolled_back",
        }:
            installed = "held"
        if row["release_phase"] == "released":
            installed = "released"
        elif row["release_phase"] == "withdrawn":
            installed = "absent"
        build = row["build_receipt"]
        public_build = (
            None
            if build is None
            else {
                "commandSha256": build["command_sha256"],
                "isolationIdentity": build["isolation_identity"],
                "treeSha256": build["inventory"]["tree_sha256"],
                "files": build["inventory"]["files_and_directories"],
                "bytes": build["inventory"]["bytes"],
            }
        )
        host = row["host_action"]
        return {
            "id": str(row["id"]),
            "ownerPrincipalId": str(row["principal_id"]),
            "owned": True,
            "agentId": str(row["agent_id"]),
            "slug": row["slug"],
            "kind": row["acquisition_kind"],
            "repository": recipe.repository,
            "oldCommit": row["old_commit"],
            "newCommit": row["new_commit"],
            "revision": row["revision"],
            "phase": phase,
            "recipe": self._public_recipe(recipe_row, token),
            "source": row["source_receipt"],
            "review": review,
            "build": public_build,
            "tests": row["tests"],
            "evidenceSha256": row["evidence_sha256"],
            "suiteSha256": row["suite_sha256"],
            "releaseState": row["release_phase"],
            "releaseGeneration": row["release_generation"],
            "releaseBindingSha256": content_hash(row["release_binding"])
            if row["release_binding"]
            else None,
            "releaseEvidenceSha256": content_hash(row["release_evidence"])
            if row["release_evidence"]
            else None,
            "installedState": installed,
            "hostAction": {"action": host["action"], "status": host["status"]} if host else None,
            "allowedActions": _allowed(row, valid),
            "bindingSha256": _binding(row),
            "errorCode": row["acquisition_error"],
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    def _envelope(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        return {**self._scope(context), "operation": self._public(conn, context, row, token)}

    def publish_recipe(
        self,
        context: WorkspaceContext,
        recipe: ToolRecipe,
        agent_id: UUID,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        token = self._authorized(setup_token)
        _uuid(agent_id)
        if not isinstance(recipe, ToolRecipe):
            raise InvalidInputError("Only an independently loaded operator recipe can be published")
        recipe = ToolRecipe.from_dict(recipe.to_dict())
        request = {"agent_id": str(agent_id), "recipe": recipe.to_dict()}
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                row = {
                    "workspace_id": context.workspace_id,
                    "id": uuid4(),
                    "principal_id": context.principal_id,
                    "agent_id": agent_id,
                    "recipe_sha256": recipe.digest,
                }
                saved = conn.execute(
                    "INSERT INTO ui_tool_acquisition_recipes(workspace_id,id,principal_id,agent_id,recipe_sha256,content,approval_mac) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        context.workspace_id,
                        row["id"],
                        context.principal_id,
                        agent_id,
                        recipe.digest,
                        Jsonb(recipe.to_dict()),
                        _recipe_signature(row, token),
                    ),
                ).fetchone()
                return {**self._scope(context), "recipe": self._public_recipe(dict(saved), token)}

            return idempotent_command(
                conn, context, "tools.recipe.publish", _key(context, key), request, create
            )

    def recipes(
        self, context: WorkspaceContext, *, limit: int = 25, after: str | None = None
    ) -> dict[str, Any]:
        cursor, token = _pagination(limit, after), self._token()
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM ui_tool_acquisition_recipes WHERE workspace_id=%s AND principal_id=%s AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (context.workspace_id, context.principal_id, cursor, cursor, limit + 1),
            ).fetchall()
            items, size = [], 0
            for raw in rows[:limit]:
                item = self._public_recipe(dict(raw), token)
                count = len(canonical_json(item).encode())
                if size + count > PAGE_BYTES:
                    break
                items.append(item)
                size += count
            return {
                **self._scope(context),
                "items": items,
                "nextCursor": items[-1]["id"] if len(rows) > len(items) else None,
            }

    def request(
        self, context: WorkspaceContext, recipe_id: UUID, commit: str, key: str, *, setup_token: str
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        token = self._authorized(setup_token)
        _uuid(recipe_id)
        with self.database.transaction(context) as conn:
            recipe_row = _recipe_row(conn, context, recipe_id)
            recipe = ToolRecipe.from_dict(recipe_row["content"])
            source_identity(recipe.repository, commit)
            request = {"recipe_id": str(recipe_id), "commit": commit}

            def create() -> dict[str, Any]:
                _verified_recipe(recipe_row, token)
                identity = uuid4()
                row = {
                    "workspace_id": context.workspace_id,
                    "id": identity,
                    "principal_id": context.principal_id,
                    "agent_id": recipe_row["agent_id"],
                    "recipe_id": recipe_id,
                    "recipe_sha256": recipe.digest,
                    "slug": recipe.slug,
                    "new_commit": commit,
                }
                saved = conn.execute(
                    "INSERT INTO ui_tool_upgrade_approvals(workspace_id,id,principal_id,agent_id,slug,old_commit,new_commit,evidence_sha256,suite_sha256,passed_tests,tests,revision,state,acquisition_phase,recipe_id,recipe_sha256,source_request_mac) "
                    "VALUES(%s,%s,%s,%s,%s,NULL,%s,NULL,%s,0,%s,1,'acquiring','source_queued',%s,%s,%s) RETURNING *",
                    (
                        context.workspace_id,
                        identity,
                        context.principal_id,
                        recipe_row["agent_id"],
                        recipe.slug,
                        commit,
                        recipe.tests.digest,
                        Jsonb([]),
                        recipe_id,
                        recipe.digest,
                        _request_signature(row, token),
                    ),
                ).fetchone()
                self._audit(
                    conn, context, dict(saved), "source_requested", "none", content_hash(request)
                )
                return self._envelope(conn, context, dict(saved), token)

            return idempotent_command(
                conn, context, "tools.acquisition.request", _key(context, key), request, create
            )

    def list(
        self, context: WorkspaceContext, *, limit: int = 25, after: str | None = None
    ) -> dict[str, Any]:
        cursor, token = _pagination(limit, after), self._token()
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND principal_id=%s AND acquisition_phase!='none' AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (context.workspace_id, context.principal_id, cursor, cursor, limit + 1),
            ).fetchall()
            items, size = [], 0
            for raw in rows[:limit]:
                item = self._public(conn, context, dict(raw), token)
                count = len(canonical_json(item).encode())
                if size + count > PAGE_BYTES:
                    break
                items.append(item)
                size += count
            return {
                **self._scope(context),
                "items": items,
                "nextCursor": items[-1]["id"] if len(rows) > len(items) else None,
            }

    def inspect(self, context: WorkspaceContext, operation_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return self._envelope(conn, context, _row(conn, context, operation_id), self._token())

    def events(
        self, context: WorkspaceContext, operation_id: UUID, *, after: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        _number(after, 2**53 - 1)
        _pagination(limit, None)
        with self.database.transaction(context) as conn:
            _row(conn, context, operation_id)
            rows = conn.execute(
                "SELECT * FROM ui_tool_upgrade_audit WHERE workspace_id=%s AND upgrade_id=%s AND revision>%s ORDER BY revision,id LIMIT %s",
                (context.workspace_id, operation_id, after, limit + 1),
            ).fetchall()
            items = [
                {
                    "revision": row["revision"],
                    "action": row["action"],
                    "priorState": row["prior_state"],
                    "nextState": row["next_state"],
                    "evidenceSha256": row["evidence_sha256"],
                    "recordedAt": row["recorded_at"].isoformat(),
                }
                for row in rows[:limit]
            ]
            return {
                **self._scope(context),
                "operationId": str(operation_id),
                "items": items,
                "nextCursor": items[-1]["revision"] if items else after,
                "hasMore": len(rows) > limit,
            }

    def _authority(
        self,
        conn: Any,
        context: WorkspaceContext,
        row: dict[str, Any],
        token: str,
        *,
        abandon_only: bool = False,
    ) -> ToolRecipe:
        recipe_row = _recipe_row(conn, context, row["recipe_id"])
        recipe = (
            ToolRecipe.from_dict(recipe_row["content"])
            if abandon_only
            else _verified_recipe(recipe_row, token)
        )
        if (
            recipe_row["agent_id"] != row["agent_id"]
            or recipe.digest != row["recipe_sha256"]
            or recipe.slug != row["slug"]
            or (
                not abandon_only
                and not hmac.compare_digest(
                    row["source_request_mac"], _request_signature(row, token)
                )
            )
        ):
            raise ScopeDeniedError(
                "The source request lacks the exact current recipe authorization"
            )
        return recipe

    def approve_build(
        self,
        context: WorkspaceContext,
        operation_id: UUID,
        revision: int,
        review_sha256: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        token = self._authorized(setup_token)
        request = {
            "id": str(_uuid(operation_id)),
            "revision": _number(revision),
            "review_sha256": _sha(review_sha256),
        }
        with self.database.transaction(context) as conn:
            _row(conn, context, operation_id)

            def create() -> dict[str, Any]:
                row = _row(conn, context, operation_id, lock=True)
                recipe = self._authority(conn, context, row, token)
                review = _review(row, recipe)
                if (
                    row["acquisition_phase"] != "review"
                    or row["revision"] != revision
                    or review is None
                    or review["sha256"] != review_sha256
                    or row["review_sha256"] != review_sha256
                ):
                    raise RevisionConflictError(
                        "The exact source review or current revision changed"
                    )
                authorization = {
                    "revision": revision + 1,
                    "principal_id": str(context.principal_id),
                }
                authorization["mac"] = _build_signature(row, authorization, token)
                saved = conn.execute(
                    "UPDATE ui_tool_upgrade_approvals SET acquisition_phase='build_queued',build_authorization=%s,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                    (Jsonb(authorization), context.workspace_id, operation_id),
                ).fetchone()
                self._audit(
                    conn, context, dict(saved), "build_approved", row["state"], review_sha256
                )
                return self._envelope(conn, context, dict(saved), token)

            return idempotent_command(
                conn,
                context,
                "tools.acquisition.approve_build",
                _key(context, key),
                request,
                create,
            )

    def decide(
        self,
        context: WorkspaceContext,
        operation_id: UUID,
        revision: int,
        evidence_sha256: str,
        approve: bool,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        from potbot.api.ui_tool_upgrades import _approval_mac

        token = self._authorized(setup_token)
        if type(approve) is not bool:
            raise InvalidInputError("An explicit promotion decision is required")
        request = {
            "id": str(_uuid(operation_id)),
            "revision": _number(revision),
            "evidence_sha256": _sha(evidence_sha256),
            "approve": approve,
        }
        with self.database.transaction(context) as conn:
            _row(conn, context, operation_id)

            def create() -> dict[str, Any]:
                row = _row(conn, context, operation_id, lock=True)
                self._authority(conn, context, row, token)
                if (
                    row["acquisition_phase"] != "tested"
                    or row["state"] != "awaiting_approval"
                    or row["revision"] != revision
                    or row["evidence_sha256"] != evidence_sha256
                ):
                    raise RevisionConflictError(
                        "The measured tool tests or promotion revision changed"
                    )
                decision = "approved" if approve else "rejected"
                mac = (
                    _approval_mac(token, row, context.principal_id, revision + 1)
                    if approve
                    else None
                )
                saved = conn.execute(
                    "UPDATE ui_tool_upgrade_approvals SET state=%s,approval_principal_id=%s,approval_mac=%s,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                    (decision, context.principal_id, mac, context.workspace_id, operation_id),
                ).fetchone()
                self._audit(conn, context, dict(saved), decision, row["state"], evidence_sha256)
                return self._envelope(conn, context, dict(saved), token)

            return idempotent_command(
                conn, context, "tools.acquisition.decide", _key(context, key), request, create
            )

    def action(
        self,
        context: WorkspaceContext,
        operation_id: UUID,
        action: str,
        revision: int,
        binding_sha256: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        token = self._authorized(setup_token)
        if not isinstance(action, str) or action not in _HOST_ACTIONS | {"cancel"}:
            raise InvalidInputError("Unsupported acquisition host action")
        request = {
            "id": str(_uuid(operation_id)),
            "action": action,
            "revision": _number(revision),
            "binding_sha256": _sha(binding_sha256),
        }
        with self.database.transaction(context) as conn:
            _row(conn, context, operation_id)

            def create() -> dict[str, Any]:
                row = _row(conn, context, operation_id, lock=True)
                if row["revision"] != revision or _binding(row) != binding_sha256:
                    raise RevisionConflictError("The current tool action binding changed")
                valid = True
                try:
                    self._authority(conn, context, row, token)
                except ScopeDeniedError:
                    valid = False
                if action not in _allowed(row, valid):
                    raise StateMismatchError(
                        "This action is not permitted in the observed acquisition state"
                    )
                if action == "cancel":
                    before_test = row["acquisition_phase"] != "tested"
                    saved = conn.execute(
                        "UPDATE ui_tool_upgrade_approvals SET state=%s,acquisition_phase=%s,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                        (
                            "acquiring" if before_test else "rejected",
                            "cancelled" if before_test else "tested",
                            context.workspace_id,
                            operation_id,
                        ),
                    ).fetchone()
                    event = "acquisition_cancelled"
                else:
                    queued = {
                        "action": action,
                        "status": "queued",
                        "revision": revision + 1,
                        "binding_sha256": binding_sha256,
                        "principal_id": str(context.principal_id),
                        "acquisition_phase": row["acquisition_phase"],
                        "operation_state": row["state"],
                        "abandon_only": action == "reconcile"
                        and row["state"]
                        in {"acquiring", "awaiting_approval", "approved", "rejected"},
                    }
                    queued["mac"] = _action_signature(row, queued, token)
                    phase = row["acquisition_phase"]
                    if phase in {"sourcing", "building"}:
                        phase = "indeterminate"
                    state = "indeterminate" if row["state"] == "executing" else row["state"]
                    release = (
                        "indeterminate"
                        if row["release_phase"] == "testing"
                        else row["release_phase"]
                    )
                    saved = conn.execute(
                        "UPDATE ui_tool_upgrade_approvals SET host_action=%s,acquisition_phase=%s,state=%s,release_phase=%s,acquisition_worker_token=NULL,worker_token=NULL,postflight_token=NULL,acquisition_error=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                        (Jsonb(queued), phase, state, release, context.workspace_id, operation_id),
                    ).fetchone()
                    event = "host_action_requested"
                self._audit(conn, context, dict(saved), event, row["state"], binding_sha256)
                return self._envelope(conn, context, dict(saved), token)

            return idempotent_command(
                conn, context, "tools.acquisition.action", _key(context, key), request, create
            )

    def claim(
        self,
        context: WorkspaceContext,
        agent_id: UUID,
        kind: str,
        *,
        setup_token: str,
        operation_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        from psycopg.types.json import Jsonb

        token = self._authorized(setup_token)
        _uuid(agent_id)
        if kind not in {"source", "build", "action"}:
            raise InvalidInputError("Unsupported acquisition worker kind")
        if operation_id is not None:
            _uuid(operation_id)
        predicate = {
            "source": "acquisition_phase='source_queued'",
            "build": "acquisition_phase='build_queued'",
            "action": "host_action->>'status'='queued'",
        }[kind]
        with self.database.transaction(context) as conn:
            raw = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND principal_id=%s AND agent_id=%s AND acquisition_worker_token IS NULL AND "
                + predicate
                + " AND (%s::uuid IS NULL OR id=%s) ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1",
                (context.workspace_id, context.principal_id, agent_id, operation_id, operation_id),
            ).fetchone()
            if raw is None:
                return None
            row = dict(raw)
            host = row["host_action"]
            recipe = self._authority(
                conn, context, row, token, abandon_only=kind == "action" and _abandon_action(host)
            )
            if kind == "source":
                if row["revision"] != 1 or row["source_receipt"] is not None:
                    raise ScopeDeniedError("The source request was already consumed")
                phase, event = "sourcing", "source_claimed"
            elif kind == "build":
                authorization = row["build_authorization"]
                review = _review(row, recipe)
                if (
                    not isinstance(authorization, dict)
                    or authorization.get("revision") != row["revision"]
                    or authorization.get("principal_id") != str(context.principal_id)
                    or not isinstance(authorization.get("mac"), str)
                    or review is None
                    or review["sha256"] != row["review_sha256"]
                    or not hmac.compare_digest(
                        authorization["mac"], _build_signature(row, authorization, token)
                    )
                ):
                    raise ScopeDeniedError(
                        "The build lacks an unconsumed current exact review approval"
                    )
                phase, event = "building", "build_claimed"
            else:
                if (
                    not isinstance(host, dict)
                    or host.get("revision") != row["revision"]
                    or host.get("principal_id") != str(context.principal_id)
                    or not isinstance(host.get("mac"), str)
                    or not hmac.compare_digest(host["mac"], _action_signature(row, host, token))
                ):
                    raise ScopeDeniedError("The host action lacks current exact operator approval")
                host = {**host, "status": "running"}
                phase, event = row["acquisition_phase"], "host_action_claimed"
            worker = uuid4()
            saved = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET acquisition_phase=%s,acquisition_worker_token=%s,host_action=%s,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                (phase, worker, Jsonb(host) if host else None, context.workspace_id, row["id"]),
            ).fetchone()
            self._audit(
                conn,
                context,
                dict(saved),
                event,
                row["state"],
                row["review_sha256"] or row["recipe_sha256"],
            )
            return {
                "id": row["id"],
                "agent_id": agent_id,
                "worker_token": worker,
                "kind": kind,
                "recipe_id": row["recipe_id"],
                "recipe_sha256": row["recipe_sha256"],
                "recipe": recipe.to_dict(),
                "new_commit": row["new_commit"],
                "old_commit": row["old_commit"],
                "source_receipt": row["source_receipt"],
                "execution": row["execution_contract"],
                "action": host["action"] if kind == "action" else None,
                "claim_revision": saved["revision"],
                "acquisition_phase": host["acquisition_phase"] if kind == "action" else phase,
                "operation_state": host["operation_state"] if kind == "action" else row["state"],
                "abandon_only": _abandon_action(host) if kind == "action" else False,
            }

    def _claimed(
        self, conn: Any, context: WorkspaceContext, claim: dict[str, Any], kind: str
    ) -> tuple[dict[str, Any], ToolRecipe, str]:
        if not isinstance(claim, dict) or claim.get("kind") != kind:
            raise ScopeDeniedError("An exact active host claim is required")
        row = _row(conn, context, _uuid(claim.get("id")), lock=True)
        if (
            row["agent_id"] != claim.get("agent_id")
            or row["acquisition_worker_token"] != _uuid(claim.get("worker_token"))
            or (kind != "action" and row["revision"] != claim.get("claim_revision"))
            or (kind == "source" and row["acquisition_phase"] != "sourcing")
            or (kind == "build" and row["acquisition_phase"] != "building")
            or (
                kind == "action"
                and (
                    not row["host_action"]
                    or row["host_action"]["status"] != "running"
                    or row["host_action"]["action"] != claim.get("action")
                    or row["host_action"]["acquisition_phase"] != claim.get("acquisition_phase")
                    or row["host_action"]["operation_state"] != claim.get("operation_state")
                    or _abandon_action(row["host_action"]) != claim.get("abandon_only")
                )
            )
        ):
            raise StateMismatchError("The acquisition worker claim was fenced")
        token = self._token()
        if token is None:
            raise ScopeDeniedError("The current operator capability is unavailable")
        recipe = self._authority(
            conn,
            context,
            row,
            token,
            abandon_only=kind == "action" and _abandon_action(row["host_action"]),
        )
        if any(
            claim.get(key) != value
            for key, value in {
                "recipe_id": row["recipe_id"],
                "recipe_sha256": row["recipe_sha256"],
                "recipe": recipe.to_dict(),
                "new_commit": row["new_commit"],
                "old_commit": row["old_commit"],
                "source_receipt": row["source_receipt"],
                "execution": row["execution_contract"],
            }.items()
        ):
            raise ScopeDeniedError("The worker payload differs from its retained operation claim")
        if kind == "build":
            authorization = row["build_authorization"]
            if not isinstance(authorization, dict) or not hmac.compare_digest(
                authorization["mac"], _build_signature(row, authorization, token)
            ):
                raise ScopeDeniedError("The build approval is no longer current")
        if kind == "action" and not hmac.compare_digest(
            row["host_action"]["mac"], _action_signature(row, row["host_action"], token)
        ):
            raise ScopeDeniedError("The host action approval is no longer current")
        return row, recipe, token

    def validate_claim(
        self, context: WorkspaceContext, claim: dict[str, Any], *, connection: Any = None
    ) -> None:
        """Recheck admission under the native tool lease immediately before work.

        S10 and S9 call this inside their existing write lease. A recovery
        decision can fence a delayed worker, while its conflicting host lease
        prevents abandonment of an attempt already admitted to run.
        """
        if not isinstance(claim, dict) or claim.get("kind") not in {"source", "build", "action"}:
            raise ScopeDeniedError("An exact active host claim is required")
        if connection is not None:
            self._claimed(connection, context, claim, claim["kind"])
        else:
            with self.database.transaction(context) as conn:
                self._claimed(conn, context, claim, claim["kind"])

    def complete_source(
        self,
        context: WorkspaceContext,
        claim: dict[str, Any],
        source_receipt: dict[str, Any],
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            row, recipe, token = self._claimed(conn, context, claim, "source")
            source = _source(source_receipt, row, recipe)
            execution = _execution(execution, row, recipe)
            proposed = {**row, "source_receipt": source, "execution_contract": execution}
            review = _review(proposed, recipe)
            assert review is not None
            saved = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET source_receipt=%s,execution_contract=%s,review_sha256=%s,acquisition_kind=%s,old_commit=%s,acquisition_phase='review',acquisition_worker_token=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                (
                    Jsonb(source),
                    Jsonb(execution),
                    review["sha256"],
                    review["kind"],
                    review["oldCommit"],
                    context.workspace_id,
                    row["id"],
                ),
            ).fetchone()
            self._audit(
                conn, context, dict(saved), "source_ready", row["state"], source["source_sha256"]
            )
            return self._envelope(conn, context, dict(saved), token)

    def complete_build(
        self,
        context: WorkspaceContext,
        claim: dict[str, Any],
        staging: dict[str, Any],
        tested: dict[str, Any],
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            row, recipe, token = self._claimed(conn, context, claim, "build")
            if not isinstance(staging, dict) or not isinstance(tested, dict):
                raise InvalidInputError("Independent S10 and S9 evidence is required")
            expected = {
                "workspace_id": str(context.workspace_id),
                "agent_id": str(row["agent_id"]),
                "operation_id": str(row["id"]),
                "slug": row["slug"],
                "new_commit": row["new_commit"],
                "expected_old_commit": row["old_commit"],
                "command_sha256": row["execution_contract"]["command_sha256"],
            }
            if staging.get("schema") != "potbot-tool-stage-s10/v1" or any(
                staging.get(key) != value for key, value in expected.items()
            ):
                raise ScopeDeniedError("Staged build evidence differs from the approved operation")
            _sha(staging.get("isolation_identity"))
            inventory = staging.get("inventory")
            if not isinstance(inventory, dict):
                raise InvalidInputError("Staging requires a bounded observed inventory")
            _sha(inventory.get("tree_sha256"))
            _number(inventory.get("files_and_directories"), 50_000)
            _number(inventory.get("bytes"), 16 * 1024**3)
            if (
                tested.get("id") != str(row["id"])
                or tested.get("slug") != row["slug"]
                or tested.get("state") != "ready"
                or tested.get("old_commit") != row["old_commit"]
                or tested.get("new_commit") != row["new_commit"]
                or tested.get("suite_sha256") != recipe.tests.digest
            ):
                raise ScopeDeniedError("S9 tested evidence differs from the exact staged operation")
            evidence = _sha(tested.get("test_evidence_sha256"))
            results = tested.get("test_results")
            if not isinstance(results, list) or len(results) != len(recipe.tests.cases):
                raise StateMismatchError("All independently measured recipe tests are required")
            safe = []
            for case, result in zip(recipe.tests.cases, results, strict=True):
                if (
                    not isinstance(result, dict)
                    or result.get("name") != case.name
                    or result.get("kind") != case.kind
                    or result.get("passed") is not True
                ):
                    raise StateMismatchError("The measured tool test suite did not pass")
                elapsed = _number(result.get("elapsed_ms"), 180_000)
                safe.append(
                    {"name": case.name, "kind": case.kind, "elapsedMs": elapsed, "passed": True}
                )
            # Only the immutable bounded S10 receipt is retained, never raw
            # subprocess output, environment values or caller-supplied paths.
            receipt = {
                **expected,
                "schema": staging["schema"],
                "isolation_identity": staging["isolation_identity"],
                "inventory": {
                    key: inventory[key] for key in ("tree_sha256", "files_and_directories", "bytes")
                },
            }
            saved = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET acquisition_phase='tested',state='awaiting_approval',build_receipt=%s,evidence_sha256=%s,tests=%s,passed_tests=%s,acquisition_worker_token=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                (Jsonb(receipt), evidence, Jsonb(safe), len(safe), context.workspace_id, row["id"]),
            ).fetchone()
            self._audit(conn, context, dict(saved), "build_tested", row["state"], evidence)
            return self._envelope(conn, context, dict(saved), token)

    def complete_action(
        self, context: WorkspaceContext, claim: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            not isinstance(result, dict)
            or result.get("id") != str(claim.get("id"))
            or len(canonical_json(result).encode()) > 131_072
        ):
            raise InvalidInputError("A bounded exact host action readback is required")
        with self.database.transaction(context) as conn:
            row, _, token = self._claimed(conn, context, claim, "action")
            # S12 writes release state itself. S9 filesystem rollback/abandon
            # returns independent readback which must clear the prior PG release
            # claim; no restored or absent executable is released here.
            transition = ""
            if result.get("state") == "aborted":
                if claim["action"] != "reconcile":
                    raise StateMismatchError("Only native reconciliation can confirm abandonment")
                transition = (
                    "state='acquiring',acquisition_phase='cancelled',worker_token=NULL,"
                    "release_phase='none',release_binding=NULL,release_evidence=NULL,"
                    "release_mac=NULL,postflight_token=NULL,"
                )
            elif (
                result.get("state") == "rolled_back"
                and claim["action"] in {"rollback", "reconcile"}
                and row["release_phase"] != "withdrawn"
                and result.get("releaseState") not in {"repinned", "released", "withdrawn"}
            ):
                transition = (
                    "state='rolled_back',worker_token=NULL,release_phase='none',"
                    "release_binding=NULL,release_evidence=NULL,release_mac=NULL,postflight_token=NULL,"
                )
            saved = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET "
                + transition
                + "host_action=NULL,acquisition_worker_token=NULL,acquisition_error=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                (context.workspace_id, row["id"]),
            ).fetchone()
            self._audit(
                conn,
                context,
                dict(saved),
                "host_action_finished",
                row["state"],
                content_hash(result),
            )
            return self._envelope(conn, context, dict(saved), token)

    def fail(
        self, context: WorkspaceContext, claim: dict[str, Any], code: str, *, uncertain: bool
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        if (
            not isinstance(code, str)
            or not _CODE.fullmatch(code)
            or type(uncertain) is not bool
            or not isinstance(claim, dict)
        ):
            raise InvalidInputError("A bounded host failure classification is required")
        with self.database.transaction(context) as conn:
            row = _row(conn, context, _uuid(claim.get("id")), lock=True)
            if row["acquisition_worker_token"] != _uuid(claim.get("worker_token")) or row[
                "agent_id"
            ] != claim.get("agent_id"):
                raise StateMismatchError("A different worker owns this acquisition outcome")
            state = "indeterminate" if uncertain else "failed"
            host = row["host_action"]
            phase = row["acquisition_phase"]
            if claim.get("kind") == "action":
                if (
                    not isinstance(host, dict)
                    or host["action"] != claim.get("action")
                    or host["status"] != "running"
                ):
                    raise StateMismatchError("The host action claim changed")
                host = {**host, "status": state}
                event = "host_action_failed"
            else:
                if claim.get("kind") not in {"source", "build"} or phase not in {
                    "sourcing",
                    "building",
                }:
                    raise StateMismatchError("The source/build claim changed")
                phase = state
                event = "acquisition_indeterminate" if uncertain else "acquisition_failed"
            saved = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET acquisition_phase=%s,host_action=%s,acquisition_worker_token=NULL,acquisition_error=%s,revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                (phase, Jsonb(host) if host else None, code, context.workspace_id, row["id"]),
            ).fetchone()
            self._audit(
                conn,
                context,
                dict(saved),
                event,
                row["state"],
                content_hash({"code": code, "uncertain": uncertain}),
            )
            return self._envelope(conn, context, dict(saved), self._token())
