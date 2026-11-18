"""Operator-admitted portable data in the existing PostgreSQL skill registry.

Imports retain immutable, owner-private source bytes. Installation and control
are explicit signed decisions, separate from genuine agent authoring sessions.
No method executes a package, installs its dependencies, or starts a worker.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from potbot.context.model import document_templates, sha256
from potbot.context.service import native_skill_setup_token
from potbot.context.skill_packages import SkillPackage, validate_resource_path, verify_package
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database, idempotent_command

SCHEMA = "potbot-skill-acquisition/v1"
PAGE_BYTES = 1_572_864
RESOURCE_BYTES = 65_536
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_DENIED = "The acquired skill or package is unavailable in this scope"


def acquired_skill_identity(context: WorkspaceContext, name: str) -> tuple[UUID, UUID, str]:
    namespace = "imported-" + context.principal_id.hex
    full_name = namespace + "." + name
    skill = uuid5(NAMESPACE_URL, f"potbot:acquired-skill:{context.workspace_id}:{full_name}")
    obj = uuid5(NAMESPACE_URL, f"potbot:context:{context.workspace_id}:skill:{full_name}")
    return skill, obj, full_name


def _token_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 4096
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _key(context: WorkspaceContext, value: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise InvalidInputError("A bounded stable skill command key is required")
    return content_hash([str(context.principal_id), value])


def _revision(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**53 - 1:
        raise InvalidInputError("A bounded current skill revision is required")
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("A skill or package UUID is required")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("A lowercase SHA-256 identity is required")
    return value


def _page(limit: int, after: str | None) -> UUID | None:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise InvalidInputError("Skill pages require between one and fifty entries")
    if after is None:
        return None
    try:
        identity = UUID(after)
        if str(identity) != after:
            raise ValueError
        return identity
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("The skill page cursor is invalid") from None


def _skill_row(
    conn: Any, context: WorkspaceContext, identity: UUID, *, owner: bool = False, lock: bool = False
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workspace_context_skills WHERE workspace_id=%s AND id=%s "
        "AND origin='acquired'" + (" FOR UPDATE" if lock else ""),
        (context.workspace_id, _uuid(identity)),
    ).fetchone()
    if row is None or (owner and row["owner_principal_id"] != context.principal_id):
        raise ScopeDeniedError(_DENIED)
    return dict(row)


def _package_row(
    conn: Any, context: WorkspaceContext, identity: UUID, *, owner: bool = False
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workspace_context_skill_packages WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, _uuid(identity)),
    ).fetchone()
    if row is None or (owner and row["owner_principal_id"] != context.principal_id):
        raise ScopeDeniedError(_DENIED)
    value = dict(row)
    if (
        content_hash(value["manifest"]) != value["release_sha256"]
        or content_hash(value["source"]) != value["source_sha256"]
        or value["manifest"].get("content_sha256") != value["content_sha256"]
        or value["manifest"].get("name") != value["name"]
        or value["manifest"].get("description") != value["description"]
    ):
        raise RevisionConflictError("The retained package identity is inconsistent")
    return value


def _package_value(conn: Any, context: WorkspaceContext, row: dict[str, Any]) -> SkillPackage:
    records = conn.execute(
        "SELECT path,sha256,byte_length,content FROM workspace_context_skill_package_files "
        "WHERE workspace_id=%s AND package_id=%s ORDER BY path",
        (context.workspace_id, row["id"]),
    ).fetchall()
    files: dict[str, bytes] = {}
    for record in records:
        data = bytes(record["content"])
        if (
            len(data) != record["byte_length"]
            or hashlib.sha256(data).hexdigest() != record["sha256"]
        ):
            raise RevisionConflictError("Retained package bytes failed their integrity check")
        files[record["path"]] = data
    package = verify_package(row["manifest"], files, source=row["source"])
    if (
        package.release_sha256 != row["release_sha256"]
        or package.content_sha256 != row["content_sha256"]
        or len(files) != row["file_count"]
        or sum(map(len, files.values())) != row["total_bytes"]
    ):
        raise RevisionConflictError("The retained package inventory differs")
    return package


def _release_row(conn: Any, context: WorkspaceContext, skill: UUID, digest: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workspace_context_skill_releases "
        "WHERE workspace_id=%s AND skill_id=%s AND sha256=%s AND package_id IS NOT NULL",
        (context.workspace_id, skill, _digest(digest)),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError(_DENIED)
    return dict(row)


def _control_body(row: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "potbot-acquired-skill-control/v1",
        "workspace_id": str(row["workspace_id"]),
        "owner_principal_id": str(row["owner_principal_id"]),
        "skill_id": str(row["id"]),
        "package_id": str(row["current_package_id"]),
        "release_sha256": row["latest_sha256"],
        "content_sha256": package["content_sha256"],
        "source_sha256": package["source_sha256"],
        "revision": row["control_revision"],
        "state": row["state"],
        "enabled": row["enabled"],
    }


def _mac(row: dict[str, Any], package: dict[str, Any], token: str) -> str:
    return hmac.new(
        token.encode(), canonical_json(_control_body(row, package)).encode(), hashlib.sha256
    ).hexdigest()


def _verify_control(
    conn: Any,
    context: WorkspaceContext,
    row: dict[str, Any],
    setup_token: str | None,
    *,
    active: bool,
) -> dict[str, Any]:
    if (
        not _token_valid(setup_token)
        or row.get("workspace_id") != context.workspace_id
        or row.get("origin") != "acquired"
        or not row.get("published")
        or (active and (row.get("state") != "active" or row.get("enabled") is not True))
        or not isinstance(row.get("control_mac"), str)
    ):
        raise ScopeDeniedError(_DENIED)
    package = _package_row(conn, context, row["current_package_id"])
    if (
        package["owner_principal_id"] != row["owner_principal_id"]
        or package["release_sha256"] != row["latest_sha256"]
        or package["name"] != row["slug"]
        or not hmac.compare_digest(row["control_mac"], _mac(row, package, str(setup_token)))
    ):
        raise ScopeDeniedError(_DENIED)
    decision = conn.execute(
        "SELECT * FROM workspace_context_skill_controls "
        "WHERE workspace_id=%s AND skill_id=%s AND revision=%s",
        (context.workspace_id, row["id"], row["control_revision"]),
    ).fetchone()
    if decision is None or any(
        decision[key] != value
        for key, value in {
            "owner_principal_id": row["owner_principal_id"],
            "mac": row["control_mac"],
            "release_sha256": row["latest_sha256"],
            "package_id": row["current_package_id"],
            "source_sha256": package["source_sha256"],
            "state": row["state"],
            "enabled": row["enabled"],
        }.items()
    ):
        raise ScopeDeniedError(_DENIED)
    return package


def verify_active_control(
    conn: Any, context: WorkspaceContext, row: dict[str, Any], setup_token: str | None
) -> None:
    """Validate current operator admission without reading all resource bytes."""
    _verify_control(conn, context, row, setup_token, active=True)


def verify_active_release(
    conn: Any,
    context: WorkspaceContext,
    skill_id: UUID,
    release_sha256: str,
    setup_token: str | None,
) -> dict[str, Any]:
    """Validate the exact currently enabled package before any Context read."""
    row = _skill_row(conn, context, skill_id)
    if row["latest_sha256"] != _digest(release_sha256):
        raise ScopeDeniedError(_DENIED)
    package_row = _verify_control(conn, context, row, setup_token, active=True)
    release = _release_row(conn, context, skill_id, release_sha256)
    package = _package_value(conn, context, package_row)
    if release["content_sha256"] != package.content_sha256:
        raise RevisionConflictError("Skill release and package content differ")
    return {
        "row": row,
        "release": release,
        "package": package,
        "content": package.content,
        "content_sha256": package.content_sha256,
        "manifest": package.manifest,
        "provenance": package.source,
    }


def _approved_rollback_package(
    conn: Any,
    context: WorkspaceContext,
    row: dict[str, Any],
    release_sha256: str,
    token: str,
) -> dict[str, Any]:
    """An appended release alone is not an operator-approved rollback target."""
    release = _release_row(conn, context, row["id"], release_sha256)
    package = _package_row(conn, context, release["package_id"], owner=True)
    if (
        package["name"] != row["slug"]
        or package["release_sha256"] != release["sha256"]
        or package["content_sha256"] != release["content_sha256"]
    ):
        raise ScopeDeniedError(_DENIED)
    decisions = conn.execute(
        "SELECT * FROM workspace_context_skill_controls "
        "WHERE workspace_id=%s AND skill_id=%s AND owner_principal_id=%s "
        "AND action IN ('install','update') AND release_sha256=%s AND package_id=%s "
        "AND source_sha256=%s AND state='active' AND NOT enabled ORDER BY revision DESC",
        (
            context.workspace_id,
            row["id"],
            context.principal_id,
            release_sha256,
            package["id"],
            package["source_sha256"],
        ),
    ).fetchall()
    for decision in decisions:
        historical = {
            **row,
            "latest_sha256": decision["release_sha256"],
            "current_package_id": decision["package_id"],
            "control_revision": decision["revision"],
            "state": decision["state"],
            "enabled": decision["enabled"],
        }
        if hmac.compare_digest(decision["mac"], _mac(historical, package, token)):
            return package
    raise ScopeDeniedError("The rollback target requires a current reviewed installation")


def _public_package(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "releaseSha256": row["release_sha256"],
        "contentSha256": row["content_sha256"],
        "sourceSha256": row["source_sha256"],
        "source": row["source"],
        "manifest": row["manifest"],
        "createdAt": row["created_at"].isoformat(),
        "files": row["file_count"],
        "bytes": row["total_bytes"],
        "ownerPrincipalId": str(row["owner_principal_id"]),
    }


class SkillAcquisition:
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
            raise ScopeDeniedError("Current operator setup authorization is required")
        return current

    def _view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        package = _package_row(conn, context, row["current_package_id"])
        valid = True
        try:
            _verify_control(conn, context, row, token, active=False)
        except ScopeDeniedError:
            valid = False
        return {
            "id": str(row["id"]),
            "name": row["slug"],
            "namespace": "imported-" + row["owner_principal_id"].hex,
            "origin": "acquired",
            "ownerPrincipalId": str(row["owner_principal_id"]),
            "owned": row["owner_principal_id"] == context.principal_id,
            "contextObjectId": str(row["context_object_id"]),
            "contextName": row["context_name"],
            "revision": row["control_revision"],
            "state": row["state"],
            "enabled": row["enabled"],
            "approvalValid": valid,
            "releaseSha256": row["latest_sha256"],
            "contentSha256": package["content_sha256"],
            "packageId": str(package["id"]),
            "sourceSha256": package["source_sha256"],
            "source": package["source"],
            "manifest": package["manifest"],
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    def acquire(
        self,
        context: WorkspaceContext,
        request: dict[str, Any],
        key: str,
        loader: Callable[[], SkillPackage],
        *,
        connection: Any = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        if (
            not isinstance(request, dict)
            or len(canonical_json(request).encode()) > 4096
            or not callable(loader)
        ):
            raise InvalidInputError("A bounded exact package source is required")
        # The discovery handoff shares this owner's transaction so a revoked
        # source cannot race its exact package receipt into another registry.
        with (
            self.database.transaction(context)
            if connection is None
            else nullcontext(connection) as conn
        ):

            def create() -> dict[str, Any]:
                loaded = loader()
                if not isinstance(loaded, SkillPackage):
                    raise InvalidInputError("The source adapter returned an invalid skill package")
                package = verify_package(loaded.manifest, loaded.files, source=loaded.source)
                identity = uuid4()
                conn.execute(
                    "INSERT INTO workspace_context_skill_packages "
                    "(workspace_id,id,owner_principal_id,name,description,release_sha256,content_sha256,source_sha256,manifest,source,total_bytes,file_count) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        context.principal_id,
                        package.name,
                        package.description,
                        package.release_sha256,
                        package.content_sha256,
                        content_hash(package.source),
                        Jsonb(package.manifest),
                        Jsonb(package.source),
                        sum(map(len, package.files.values())),
                        len(package.files),
                    ),
                )
                for path, data in package.files.items():
                    conn.execute(
                        "INSERT INTO workspace_context_skill_package_files "
                        "(workspace_id,package_id,path,sha256,byte_length,content) VALUES(%s,%s,%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            identity,
                            path,
                            hashlib.sha256(data).hexdigest(),
                            len(data),
                            data,
                        ),
                    )
                return {"package_id": str(identity)}

            receipt = idempotent_command(
                conn, context, "skills.acquire", _key(context, key), request, create
            )
            row = _package_row(conn, context, UUID(receipt["package_id"]), owner=True)
            return {**self._scope(context), "package": _public_package(row)}

    def packages(
        self, context: WorkspaceContext, *, limit: int = 25, after: str | None = None
    ) -> dict[str, Any]:
        cursor = _page(limit, after)
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM workspace_context_skill_packages WHERE workspace_id=%s AND owner_principal_id=%s "
                "AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (context.workspace_id, context.principal_id, cursor, cursor, limit + 1),
            ).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                item = _public_package(dict(row))
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

    def package(self, context: WorkspaceContext, package_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = _package_row(conn, context, package_id, owner=True)
            package = _package_value(conn, context, row)
            content = package.content.encode()[:RESOURCE_BYTES].decode("utf-8", errors="ignore")
            return {
                **self._scope(context),
                "package": _public_package(row),
                "content": content,
                "contentTruncated": len(content.encode()) < len(package.content.encode()),
            }

    def _plan(
        self, conn: Any, context: WorkspaceContext, package_id: UUID, skill_id: UUID | None
    ) -> dict[str, Any]:
        package = _package_row(conn, context, package_id, owner=True)
        identity, _, _ = acquired_skill_identity(context, package["name"])
        row = None
        if skill_id is not None:
            row = _skill_row(conn, context, skill_id, owner=True)
            if row["state"] != "active" or row["slug"] != package["name"] or skill_id != identity:
                raise ScopeDeniedError("The package cannot replace this acquired skill")
        elif conn.execute(
            "SELECT 1 FROM workspace_context_skills WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, identity),
        ).fetchone():
            raise RevisionConflictError(
                "This imported name is installed; review an explicit update"
            )
        previous = (
            _package_row(conn, context, row["current_package_id"], owner=True) if row else None
        )
        old_files = (
            {item["path"]: item["sha256"] for item in previous["manifest"]["files"]}
            if previous
            else {}
        )
        new_files = {item["path"]: item["sha256"] for item in package["manifest"]["files"]}
        result = {
            "packageId": str(package_id),
            "skillId": str(identity),
            "name": package["name"],
            "action": "update" if row else "install",
            "expectedRevision": row["control_revision"] if row else 0,
            "previousReleaseSha256": row["latest_sha256"] if row else None,
            "releaseSha256": package["release_sha256"],
            "sourceSha256": package["source_sha256"],
            "willDisable": True,
            "executable": False,
            "added": sorted(new_files.keys() - old_files.keys()),
            "removed": sorted(old_files.keys() - new_files.keys()),
            "changed": sorted(
                path
                for path in new_files.keys() & old_files.keys()
                if new_files[path] != old_files[path]
            ),
            "sourceChanged": previous is not None
            and previous["source_sha256"] != package["source_sha256"],
            "package": _public_package(package),
        }
        return {
            "sha256": content_hash(
                {
                    "workspace": str(context.workspace_id),
                    "principal": str(context.principal_id),
                    **result,
                }
            ),
            **result,
        }

    def plan(
        self, context: WorkspaceContext, package_id: UUID, *, skill_id: UUID | None = None
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {**self._scope(context), "plan": self._plan(conn, context, package_id, skill_id)}

    @staticmethod
    def _seed_context(conn: Any, context: WorkspaceContext, identity: UUID, name: str) -> None:
        from psycopg.types.json import Jsonb

        conn.execute(
            "INSERT INTO workspace_context_objects(workspace_id,id,kind,name,visibility) VALUES(%s,%s,'skill',%s,'shared')",
            (context.workspace_id, identity, name),
        )
        actor = f"principal:{context.principal_id}"
        for role, text in document_templates("skill", name).items():
            digest = sha256(text)
            conn.execute(
                "INSERT INTO workspace_context_documents(workspace_id,object_id,role,content,revision,sha256) VALUES(%s,%s,%s,%s,1,%s)",
                (context.workspace_id, identity, role, text, digest),
            )
            conn.execute(
                "INSERT INTO workspace_context_revisions(workspace_id,object_id,role,revision,content,sha256,actor,reason) VALUES(%s,%s,%s,1,%s,%s,%s,'initial imported skill context')",
                (context.workspace_id, identity, role, text, digest, actor),
            )
            conn.execute(
                "INSERT INTO workspace_context_events(workspace_id,object_id,role,revision,actor,event_type,payload) VALUES(%s,%s,%s,1,%s,'document.provisioned',%s)",
                (
                    context.workspace_id,
                    identity,
                    role,
                    actor,
                    Jsonb({"sha256": digest, "origin": "acquired"}),
                ),
            )

    @staticmethod
    def _decision(
        conn: Any,
        row: dict[str, Any],
        package: dict[str, Any],
        action: str,
        plan: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO workspace_context_skill_controls(workspace_id,skill_id,revision,owner_principal_id,action,release_sha256,package_id,source_sha256,plan_sha256,state,enabled,mac) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["workspace_id"],
                row["id"],
                row["control_revision"],
                row["owner_principal_id"],
                action,
                row["latest_sha256"],
                row["current_package_id"],
                package["source_sha256"],
                plan,
                row["state"],
                row["enabled"],
                row["control_mac"],
            ),
        )

    def install(
        self,
        context: WorkspaceContext,
        package_id: UUID,
        plan_sha256: str,
        expected_revision: int,
        key: str,
        *,
        skill_id: UUID | None = None,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = {
            "package_id": str(_uuid(package_id)),
            "plan_sha256": _digest(plan_sha256),
            "expected_revision": _revision(expected_revision),
            "skill_id": str(skill_id) if skill_id else None,
        }
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                package_row = _package_row(conn, context, package_id, owner=True)
                identity, context_id, name = acquired_skill_identity(context, package_row["name"])
                lock_key = int.from_bytes(
                    hashlib.sha256(identity.bytes).digest()[:8], "big", signed=True
                )
                conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock_key,))
                old = (
                    _skill_row(conn, context, skill_id, owner=True, lock=True) if skill_id else None
                )
                plan = self._plan(conn, context, package_id, skill_id)
                if plan["sha256"] != plan_sha256 or plan["expectedRevision"] != expected_revision:
                    raise RevisionConflictError("The package plan or installed revision changed")
                package = _package_value(conn, context, package_row)
                existing_release = conn.execute(
                    "SELECT * FROM workspace_context_skill_releases WHERE workspace_id=%s AND skill_id=%s AND sha256=%s",
                    (context.workspace_id, identity, package.release_sha256),
                ).fetchone()
                draft_revision = (
                    (old["draft_revision"] + (0 if existing_release else 1)) if old else 1
                )
                row = {
                    "workspace_id": context.workspace_id,
                    "id": identity,
                    "owner_principal_id": context.principal_id,
                    "latest_sha256": package.release_sha256,
                    "current_package_id": package_id,
                    "control_revision": expected_revision + 1,
                    "state": "active",
                    "enabled": False,
                }
                row["control_mac"] = _mac(row, package_row, token)
                if old is None:
                    conn.execute(
                        "INSERT INTO workspace_context_skills(workspace_id,id,owner_principal_id,slug,context_object_id,context_name,draft_revision,published,latest_sha256,origin,enabled,control_revision,control_mac,current_package_id) VALUES(%s,%s,%s,%s,%s,%s,1,true,%s,'acquired',false,1,%s,%s)",
                        (
                            context.workspace_id,
                            identity,
                            context.principal_id,
                            package.name,
                            context_id,
                            name,
                            package.release_sha256,
                            row["control_mac"],
                            package_id,
                        ),
                    )
                    self._seed_context(conn, context, context_id, name)
                if not existing_release:
                    conn.execute(
                        "INSERT INTO workspace_context_skill_versions(workspace_id,skill_id,revision,content,sha256,package_id) VALUES(%s,%s,%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            identity,
                            draft_revision,
                            package.content,
                            package.content_sha256,
                            package_id,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO workspace_context_skill_releases(workspace_id,skill_id,sha256,content_sha256,source_revision,package_id) VALUES(%s,%s,%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            identity,
                            package.release_sha256,
                            package.content_sha256,
                            draft_revision,
                            package_id,
                        ),
                    )
                if old is not None:
                    conn.execute(
                        "UPDATE workspace_context_skills SET draft_revision=%s,latest_sha256=%s,current_package_id=%s,enabled=false,control_revision=%s,control_mac=%s,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                        (
                            draft_revision,
                            package.release_sha256,
                            package_id,
                            row["control_revision"],
                            row["control_mac"],
                            context.workspace_id,
                            identity,
                        ),
                    )
                self._decision(conn, row, package_row, "update" if old else "install", plan_sha256)
                return {
                    "skill": self._view(
                        conn, context, _skill_row(conn, context, identity, owner=True), token
                    )
                }

            result = idempotent_command(
                conn, context, "skills.install", _key(context, key), request, create
            )
            _skill_row(conn, context, UUID(result["skill"]["id"]), owner=True)
            return {**self._scope(context), **result}

    def control(
        self,
        context: WorkspaceContext,
        skill_id: UUID,
        action: str,
        expected_revision: int,
        key: str,
        *,
        release_sha256: str | None = None,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        if action not in {"activate", "disable", "rollback", "revoke"} or (
            action == "rollback"
        ) != (release_sha256 is not None):
            raise InvalidInputError("Invalid imported skill control action")
        if release_sha256 is not None:
            _digest(release_sha256)
        request = {
            "skill_id": str(_uuid(skill_id)),
            "action": action,
            "expected_revision": _revision(expected_revision),
            "release_sha256": release_sha256,
        }
        with self.database.transaction(context) as conn:
            _skill_row(conn, context, skill_id, owner=True)

            def create() -> dict[str, Any]:
                row = _skill_row(conn, context, skill_id, owner=True, lock=True)
                if row["state"] != "active" or row["control_revision"] != expected_revision:
                    raise RevisionConflictError("The acquired skill was changed or revoked")
                # Rotation or a forged head must be recovered through a freshly
                # reviewed installation. A disable must not launder an invalid
                # head into a valid approval that could then be activated.
                if action != "revoke":
                    _verify_control(conn, context, row, token, active=False)
                if action == "rollback":
                    target = _approved_rollback_package(
                        conn, context, row, str(release_sha256), token
                    )
                    row["latest_sha256"], row["current_package_id"] = (
                        target["release_sha256"],
                        target["id"],
                    )
                package = _package_row(conn, context, row["current_package_id"], owner=True)
                if action != "revoke":
                    _package_value(conn, context, package)
                row["enabled"] = action == "activate"
                row["state"] = "revoked" if action == "revoke" else "active"
                row["control_revision"] += 1
                row["control_mac"] = _mac(row, package, token)
                conn.execute(
                    "UPDATE workspace_context_skills SET latest_sha256=%s,current_package_id=%s,enabled=%s,state=%s,control_revision=%s,control_mac=%s,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                    (
                        row["latest_sha256"],
                        row["current_package_id"],
                        row["enabled"],
                        row["state"],
                        row["control_revision"],
                        row["control_mac"],
                        context.workspace_id,
                        skill_id,
                    ),
                )
                self._decision(conn, row, package, action)
                return {
                    "skill": self._view(conn, context, _skill_row(conn, context, skill_id), token)
                }

            result = idempotent_command(
                conn, context, "skills.control", _key(context, key), request, create
            )
            _skill_row(conn, context, skill_id, owner=True)
            return {**self._scope(context), **result}

    def inspect(self, context: WorkspaceContext, skill_id: UUID) -> dict[str, Any]:
        token = self._token()
        with self.database.transaction(context) as conn:
            row = _skill_row(conn, context, skill_id)
            if row["owner_principal_id"] != context.principal_id:
                verify_active_control(conn, context, row, token)
            return {**self._scope(context), "skill": self._view(conn, context, row, token)}

    def list(
        self, context: WorkspaceContext, *, limit: int = 25, after: str | None = None
    ) -> dict[str, Any]:
        cursor, token = _page(limit, after), self._token()
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM workspace_context_skills WHERE workspace_id=%s AND origin='acquired' AND (owner_principal_id=%s OR (state='active' AND enabled)) AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (context.workspace_id, context.principal_id, cursor, cursor, limit + 1),
            ).fetchall()
            items, size, consumed = [], 0, 0
            last = after
            for raw in rows[:limit]:
                row = dict(raw)
                try:
                    if row["owner_principal_id"] != context.principal_id:
                        verify_active_control(conn, context, row, token)
                    item = self._view(conn, context, row, token)
                except ScopeDeniedError:
                    consumed += 1
                    last = str(row["id"])
                    continue
                count = len(canonical_json(item).encode())
                if size + count > PAGE_BYTES:
                    break
                consumed += 1
                last = str(row["id"])
                items.append(item)
                size += count
            return {
                **self._scope(context),
                "items": items,
                "nextCursor": last if len(rows) > consumed else None,
            }

    def history(
        self,
        context: WorkspaceContext,
        skill_id: UUID,
        *,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or (
                after is not None
                and (not isinstance(after, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", after))
            )
        ):
            raise InvalidInputError("Invalid skill history page")
        cursor = int(after) if after is not None else None
        token = self._token()
        with self.database.transaction(context) as conn:
            row = _skill_row(conn, context, skill_id, owner=True)
            controls = conn.execute(
                "SELECT * FROM workspace_context_skill_controls WHERE workspace_id=%s AND skill_id=%s AND (%s::bigint IS NULL OR revision<%s) ORDER BY revision DESC LIMIT %s",
                (context.workspace_id, skill_id, cursor, cursor, limit + 1),
            ).fetchall()
            result: dict[str, Any] = {
                **self._scope(context),
                "skill": self._view(conn, context, row, token),
                "releases": [],
                "events": [],
                "nextCursor": None,
            }
            seen: set[str] = set()
            for control in controls[:limit]:
                package = _package_row(conn, context, control["package_id"], owner=True)
                event = {
                    "revision": control["revision"],
                    "action": control["action"],
                    "releaseSha256": control["release_sha256"],
                    "packageId": str(control["package_id"]),
                    "sourceSha256": control["source_sha256"],
                    "source": package["source"],
                    "state": control["state"],
                    "enabled": control["enabled"],
                    "createdAt": control["created_at"].isoformat(),
                }
                release_view = None
                if control["release_sha256"] not in seen:
                    release = _release_row(conn, context, skill_id, control["release_sha256"])
                    original = _package_row(conn, context, release["package_id"], owner=True)
                    release_view = {
                        "releaseSha256": release["sha256"],
                        "contentSha256": release["content_sha256"],
                        "packageId": str(release["package_id"]),
                        "sourceSha256": original["source_sha256"],
                        "revision": release["source_revision"],
                        "createdAt": release["created_at"].isoformat(),
                        "current": release["sha256"] == row["latest_sha256"],
                        "manifest": original["manifest"],
                        "source": original["source"],
                    }
                candidate = {
                    **result,
                    "events": [*result["events"], event],
                    "releases": [*result["releases"], *([release_view] if release_view else [])],
                }
                if len(canonical_json(candidate).encode()) > PAGE_BYTES:
                    break
                result = candidate
                seen.add(control["release_sha256"])
            if len(controls) > len(result["events"]):
                result["nextCursor"] = str(result["events"][-1]["revision"])
            return result

    def _resource(
        self, context: WorkspaceContext, package: SkillPackage, path: str, offset: int, length: int
    ) -> dict[str, Any]:
        path = validate_resource_path(path)
        if (
            type(offset) is not int
            or offset < 0
            or type(length) is not int
            or not 1 <= length <= RESOURCE_BYTES
        ):
            raise InvalidInputError("Invalid bounded skill resource range")
        data = package.files.get(path)
        if data is None:
            raise ScopeDeniedError(_DENIED)
        if offset > len(data):
            raise InvalidInputError("Skill resource offset exceeds its length")
        metadata = next(item for item in package.manifest["files"] if item["path"] == path)
        chunk = data[offset : offset + length]
        next_offset = offset + len(chunk)
        return {
            **self._scope(context),
            "resource": {
                "path": path,
                "sha256": metadata["sha256"],
                "size": len(data),
                "mediaType": metadata["media_type"],
            },
            "offset": offset,
            "dataBase64": base64.b64encode(chunk).decode("ascii"),
            "nextOffset": next_offset if next_offset < len(data) else None,
        }

    def package_resource(
        self,
        context: WorkspaceContext,
        package_id: UUID,
        path: str,
        *,
        offset: int = 0,
        length: int = RESOURCE_BYTES,
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = _package_row(conn, context, package_id, owner=True)
            return self._resource(context, _package_value(conn, context, row), path, offset, length)

    def resource(
        self,
        context: WorkspaceContext,
        skill_id: UUID,
        release_sha256: str,
        path: str,
        *,
        offset: int = 0,
        length: int = RESOURCE_BYTES,
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = _skill_row(conn, context, skill_id)
            if row["owner_principal_id"] == context.principal_id:
                release = _release_row(conn, context, skill_id, release_sha256)
                package_id = (
                    row["current_package_id"]
                    if release_sha256 == row["latest_sha256"]
                    else release["package_id"]
                )
                package = _package_value(
                    conn, context, _package_row(conn, context, package_id, owner=True)
                )
            else:
                package = verify_active_release(
                    conn, context, skill_id, release_sha256, self._token()
                )["package"]
            return self._resource(context, package, path, offset, length)
