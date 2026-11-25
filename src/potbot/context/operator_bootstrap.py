"""Resolve the ordinary operator's restricted login, never administrative credentials."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from potbot.context.projection import ContextProjectionManager
from potbot.context.runtime_integration import context_parent
from potbot.context.service import WorkspaceContextService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError


def _managed_context_scope(paths: Any, location: str) -> tuple[UUID, UUID, str]:
    """Read WSL's scoped receipt beside its private controller ledger.

    This contains identity/schema only; database authority still comes from the
    restricted login below. Native Windows configuration paths are never used.
    """
    from potbot.persistence.database import _identifier

    if os.name != "posix":
        raise ScopeDeniedError("Managed operator context scope requires a POSIX host")
    try:
        path = Path(location)
        expected = Path(paths.controller_ledger).parent / "operator-context.json"
        if not path.is_absolute() or path != expected or path.resolve(strict=True) != path:
            raise ValueError
        owner = os.geteuid()
        parent = path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != owner or parent.st_mode & 0o077:
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > 8192
            ):
                raise ValueError
            document = json.loads(stream.read(8193))
        if (
            not isinstance(document, dict)
            or set(document) != {"profile", "workspace_id", "principal_id", "schema"}
            or document["profile"] != "potbot.operator-context/1"
            or any(not isinstance(document[name], str)
                   for name in ("workspace_id", "principal_id", "schema"))
        ):
            raise ValueError
        workspace, principal = UUID(document["workspace_id"]), UUID(document["principal_id"])
        if str(workspace) != document["workspace_id"] or str(principal) != document["principal_id"]:
            raise ValueError
        return workspace, principal, _identifier(document["schema"])
    except (OSError, ValueError, TypeError, KeyError, InvalidInputError):
        raise ScopeDeniedError("Managed operator context scope is unavailable or invalid") from None


def default_context_manager(
    paths: Any,
    *,
    principal_id: UUID | None = None,
    parent: Path | None = None,
    schema: str | None = None,
    shared_skills: Path | None = None,
) -> ContextProjectionManager:
    """Authenticate against session_user before allocating the private projection root.

    Native service children inherit their existing service configuration's scope.
    Standalone operators use --context-schema/POTBOT_CONTEXT_SCHEMA or 'potbot'.
    No agent-supplied UUID, DSN or directory enters this path.
    """
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from potbot.credentials import resolve_postgres_dsn
    from potbot.persistence.database import Database, _identifier, _restricted_login

    managed_scope = os.environ.get("POTBOT_OPERATOR_CONTEXT_CONFIG")
    if managed_scope and os.environ.get("POTBOT_OPERATOR_REQUIRED") != "1":
        raise ScopeDeniedError("Managed context scope requires a managed operator")
    if os.environ.get("POTBOT_OPERATOR_REQUIRED") == "1":
        if managed_scope:
            workspace, configured_principal, configured_schema = _managed_context_scope(
                paths, managed_scope
            )
        else:
            from potbot.service import ServiceConfig, config_path

            config = ServiceConfig.load(config_path())
            workspace = UUID(config.workspace_id)
            configured_principal, configured_schema = UUID(config.principal_id), config.schema
        if workspace != paths.workspace_id:
            raise ScopeDeniedError("Operator service scope differs from manifest")
        if schema is not None and schema != configured_schema:
            raise ScopeDeniedError("Operator service schema differs from configuration")
        if principal_id is not None and principal_id != configured_principal:
            raise ScopeDeniedError("Operator service principal differs from configuration")
        principal_id, schema = configured_principal, configured_schema
    schema = _identifier(schema or os.environ.get("POTBOT_CONTEXT_SCHEMA", "potbot"))
    database = Database(resolve_postgres_dsn(), schema)
    with psycopg.connect(
        database.dsn,
        row_factory=dict_row,
        connect_timeout=5,
        options="-c search_path=pg_catalog -c statement_timeout=30000 -c lock_timeout=10000",
    ) as conn:
        conn.execute(
            sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(sql.Identifier(schema))
        )
        login = conn.execute("SELECT session_user AS role").fetchone()
        if login is None:
            raise ScopeDeniedError("Operator login identity is unavailable")
        _restricted_login(conn, schema, login["role"])
        identities = conn.execute(
            "SELECT p.id FROM principals p JOIN workspace_memberships m ON m.principal_id=p.id "
            "WHERE p.database_role=session_user AND m.workspace_id=%s",
            (paths.workspace_id,),
        ).fetchall()
    if len(identities) != 1 or (principal_id is not None and identities[0]["id"] != principal_id):
        raise ScopeDeniedError("Operator login has no matching workspace identity")
    principal = WorkspaceContext(paths.workspace_id, identities[0]["id"])
    service = WorkspaceContextService(database)
    # Verify source state and migration readiness before any filesystem write.
    service.read_triple(principal, "sandbox", str(principal.workspace_id))
    with database.transaction(principal) as conn:
        conn.execute("SELECT 1 FROM workspace_context_sources LIMIT 0")
    from dataclasses import replace

    runtime = replace(paths.runtime(), shared_skills=shared_skills)
    if parent is None:
        parent = context_parent(
            SimpleNamespace(path=paths.controller_ledger), runtime, paths.workspace_id
        )
    else:
        parent = Path(parent)
        # ContextProjectionManager validates ownership/mode; also reject overlap
        # before it can publish a snapshot.
        for area in (
            runtime.rootfs,
            runtime.home,
            runtime.model_staging,
            runtime.shared_models,
            runtime.shared_tools,
            runtime.shared_skills,
            runtime.ai_config_dir,
            runtime.ai_history_dir,
        ):
            if area is not None:
                other = Path(area).resolve(strict=True)
                if parent == other or parent.is_relative_to(other) or other.is_relative_to(parent):
                    raise ScopeDeniedError("Context storage overlaps a guest mount")
    from potbot.context.skill_projection import _directory

    _directory(parent)
    if parent.stat().st_mode & 0o077:
        raise ScopeDeniedError("Operator context directory must be private")
    return ContextProjectionManager(service=service, principal=principal, parent=parent)
