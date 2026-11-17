"""Transactional agent skill authoring and workspace-wide, digest-pinned discovery.

This service borrows an authenticated context-session transaction. It never
opens an admin connection, executes skill text or accepts a filesystem path.
Published code and live MODEL/MEMORY documents have separate revision histories.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from potbot.context.model import document_templates, sha256
from potbot.context.rpc_contract import MAX_FRAME_BYTES, SCHEMA, SkillRequest
from potbot.context.service import WorkspaceContextService, native_skill_setup_token
from potbot.context.skills import validate_skill
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.context.sessions import ContextSession

_DENIED = "Skill is unavailable or the operation is not authorized"


def skill_identity(workspace_id: UUID, agent_id: UUID, slug: str) -> tuple[UUID, UUID, str]:
    """An agent cannot squat an operator or another agent's namespace."""
    import re

    if (
        not isinstance(workspace_id, UUID)
        or not isinstance(agent_id, UUID)
        or not isinstance(slug, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", slug)
    ):
        raise InvalidInputError("Invalid authored skill identity")
    name = f"agent-{agent_id.hex}.{slug}"
    identity = uuid5(NAMESPACE_URL, f"potbot:authored-skill:{workspace_id}:{name}")
    obj = uuid5(NAMESPACE_URL, f"potbot:context:{workspace_id}:skill:{name}")
    return identity, obj, name


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    origin = row.get("origin", "authored")
    namespace = (
        "imported-" + row["owner_principal_id"].hex
        if origin == "acquired"
        else "agent-" + row["owner_agent_id"].hex
    )
    return {
        "skill_id": str(row["id"]),
        "name": row["slug"],
        "namespace": namespace,
        "context_name": row["context_name"],
        "context_object_id": str(row["context_object_id"]),
        "revision": row["draft_revision"],
        "release_sha256": row["latest_sha256"],
        "state": row["state"],
        "origin": origin,
        "enabled": row.get("enabled", True),
        "trusted": False,
        "automatically_activated": False,
    }


def published_skills(
    conn: Any,
    context: WorkspaceContext,
    *,
    limit: int = 25,
    after: UUID | None = None,
    include_acquired: bool = True,
    setup_token: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    if (
        type(limit) is not int
        or not 1 <= limit <= 50
        or (after is not None and not isinstance(after, UUID))
        or type(include_acquired) is not bool
    ):
        raise InvalidInputError("Invalid skill page")
    rows = conn.execute(
        "SELECT * FROM workspace_context_skills WHERE workspace_id=%s AND published "
        "AND state='active' AND (origin='authored' OR (enabled AND %s)) "
        "AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
        (context.workspace_id, include_acquired, after, after, limit + 1),
    ).fetchall()
    page = rows[:limit]
    visible = []
    token: str | None = None
    resolved = False
    for result in page:
        row = dict(result)
        if row.get("origin", "authored") == "acquired":
            if not include_acquired:
                continue
            from potbot.context.skill_acquisition import verify_active_control

            if not resolved:
                token = (setup_token or native_skill_setup_token)()
                resolved = True
            try:
                verify_active_control(conn, context, row, token)
            except (ScopeDeniedError, RevisionConflictError, InvalidInputError):
                continue
        visible.append(_summary(row))
    return {
        "skills": visible,
        # Advance over examined rows even when no imported row is authorized.
        "next": str(page[-1]["id"]) if len(rows) > limit else None,
    }


def load_skill(
    conn: Any,
    context: WorkspaceContext,
    service: WorkspaceContextService,
    skill_id: UUID,
    release_sha256: str,
) -> dict[str, Any]:
    """Read an explicit immutable release plus one coherent live context triple.

    Returned text is data for an explicitly selected skill, not a higher-priority
    instruction or permission to execute anything. No implicit 'latest' loading.
    Acquired packages require that exact digest to remain current and enabled.
    An update or rollback rejects later old-digest reads; it never substitutes
    different bytes and cannot recall bytes already returned to the caller.
    """
    row = conn.execute(
        "SELECT k.*,r.source_revision,v.content,v.sha256 AS content_sha256 "
        "FROM workspace_context_skills k JOIN workspace_context_skill_releases r "
        "ON r.workspace_id=k.workspace_id AND r.skill_id=k.id "
        "JOIN workspace_context_skill_versions v ON v.workspace_id=r.workspace_id "
        "AND v.skill_id=r.skill_id AND v.revision=r.source_revision "
        "WHERE k.workspace_id=%s AND k.id=%s AND r.sha256=%s "
        "AND k.published AND k.state='active'",
        (context.workspace_id, skill_id, release_sha256),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError(_DENIED)
    if row.get("origin", "authored") == "acquired":
        from potbot.context.skill_acquisition import verify_active_release

        verified = verify_active_release(
            conn, context, skill_id, release_sha256, service.skill_setup_token()
        )
        row = verified["row"]
        docs = service.read_triple(context, "skill", row["context_name"])
        result = {
            **_summary(dict(row)),
            "revision": verified["release"]["source_revision"],
            "release_sha256": release_sha256,
            "sha256": release_sha256,
            "content_sha256": verified["content_sha256"],
            "content": verified["content"],
            "manifest": verified["manifest"],
            "provenance": verified["provenance"],
            "context": docs,
            "classification": "untrusted-skill-data",
            "resource_access": "context-rpc",
        }
        # Preserve complete original text and its digest. A response that cannot
        # fit the existing Context frame fails before the session transaction
        # commits; the caller can still read SKILL.md through bounded ranges.
        envelope = {
            "schema": SCHEMA,
            "request_id": "00000000-0000-0000-0000-000000000000",
            "ok": True,
            "result": result,
        }
        if (
            len(
                json.dumps(
                    envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
            )
            > MAX_FRAME_BYTES
        ):
            raise InvalidInputError(
                "Skill load exceeds the Context frame; read SKILL.md resource ranges"
            )
        return result
    if row["content_sha256"] != release_sha256 or sha256(row["content"]) != release_sha256:
        raise RevisionConflictError("Published skill integrity check failed")
    docs = service.read_triple(context, "skill", row["context_name"])
    return {
        **_summary(dict(row)),
        "revision": row["source_revision"],
        "release_sha256": release_sha256,
        "sha256": release_sha256,
        "content": row["content"],
        "context": docs,
        "classification": "untrusted-skill-data",
    }


def read_skill_resource(
    conn: Any,
    context: WorkspaceContext,
    service: WorkspaceContextService,
    request: SkillRequest,
) -> dict[str, Any]:
    """Read verified acquired bytes through the current authenticated session only."""
    from potbot.context.skill_acquisition import verify_active_release

    if request.method != "skills.read":
        raise InvalidInputError("A pinned skill resource request is required")
    assert request.skill_id is not None and request.release_sha256 is not None
    assert request.resource_path is not None
    assert request.offset is not None and request.max_bytes is not None
    verified = verify_active_release(
        conn,
        context,
        request.skill_id,
        request.release_sha256,
        service.skill_setup_token(),
    )
    package = verified["package"]
    data = package.files.get(request.resource_path)
    if not isinstance(data, bytes):
        raise ScopeDeniedError(_DENIED)
    if request.offset > len(data):
        raise InvalidInputError("Skill resource range starts after the resource")
    part = data[request.offset : request.offset + request.max_bytes]
    end = request.offset + len(part)
    return {
        "skill_id": str(request.skill_id),
        "release_sha256": request.release_sha256,
        "revision": verified["release"]["source_revision"],
        "resource_path": request.resource_path,
        "resource_sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "offset": request.offset,
        "next_offset": end,
        "eof": end == len(data),
        "encoding": "base64",
        "data": base64.b64encode(part).decode("ascii"),
        "range_sha256": hashlib.sha256(part).hexdigest(),
        "classification": "untrusted-skill-data",
    }


class SessionSkillCatalogue:
    """No independent commits: call only inside ContextSessionStore.execute()."""

    def __init__(
        self,
        conn: Any,
        grant: ContextSession,
        session: dict[str, Any],
        service: WorkspaceContextService,
    ):
        self.conn, self.grant, self.session, self.service = conn, grant, session, service
        self.context = grant.principal

    def _row(self, skill_id: UUID, *, write: bool) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM workspace_context_skills WHERE workspace_id=%s AND id=%s"
            + (" FOR UPDATE" if write else ""),
            (self.context.workspace_id, skill_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError(_DENIED)
        row = dict(row)
        owns = (
            row.get("origin", "authored") == "authored"
            and row["owner_principal_id"] == self.context.principal_id
            and row["owner_agent_id"] == self.grant.agent_id
        )
        # Agent isolation is checked even when the host login owns both agents.
        if (write and not owns) or (not write and not row["published"] and not owns):
            raise ScopeDeniedError(_DENIED)
        return row

    def authorize(self, request: SkillRequest) -> None:
        # Recheck ownership before idempotent replay. An archived skill's original
        # receipt can be recovered, but execute() refuses any new mutation to it.
        if request.method in ("skills.save", "skills.publish", "skills.archive"):
            assert request.skill_id is not None
            self._row(request.skill_id, write=True)

    def context_target(self, skill_id: UUID, *, write: bool) -> tuple[str, UUID]:
        row = self._row(skill_id, write=write)
        if row["state"] != "active":
            raise ScopeDeniedError(_DENIED)
        if row.get("origin", "authored") == "acquired":
            from potbot.context.skill_acquisition import verify_active_control

            verify_active_control(self.conn, self.context, row, self.service.skill_setup_token())
        # For writes the row lock serializes with archival/revocation. Other
        # agents may read published skill context, but cannot alter the author's.
        return row["context_name"], row["context_object_id"]

    def _version(self, row: dict[str, Any]) -> dict[str, Any]:
        version = self.conn.execute(
            "SELECT revision,content,sha256 FROM workspace_context_skill_versions "
            "WHERE workspace_id=%s AND skill_id=%s AND revision=%s",
            (self.context.workspace_id, row["id"], row["draft_revision"]),
        ).fetchone()
        if version is None or sha256(version["content"]) != version["sha256"]:
            raise RevisionConflictError("Skill draft history is inconsistent")
        return dict(version)

    def _insert_version(self, identity: UUID, revision: int, content: str) -> str:
        checksum = sha256(content)
        self.conn.execute(
            "INSERT INTO workspace_context_skill_versions "
            "(workspace_id,skill_id,revision,content,sha256,session_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                self.context.workspace_id,
                identity,
                revision,
                content,
                checksum,
                self.grant.session_id,
            ),
        )
        return checksum

    def _seed_context(self, object_id: UUID, name: str) -> None:
        from psycopg.types.json import Jsonb

        context = self.context
        # Metadata was inserted first with a deferred FK. RLS admits ONLY that
        # exact shared skill context object, not arbitrary shared primitives.
        self.conn.execute(
            "INSERT INTO workspace_context_objects (workspace_id,id,kind,name,visibility) "
            "VALUES (%s,%s,'skill',%s,'shared')",
            (context.workspace_id, object_id, name),
        )
        actor = f"principal:{context.principal_id}"
        for role, text in document_templates("skill", name).items():
            digest = sha256(text)
            self.conn.execute(
                "INSERT INTO workspace_context_documents "
                "(workspace_id,object_id,role,content,revision,sha256) VALUES (%s,%s,%s,%s,1,%s)",
                (context.workspace_id, object_id, role, text, digest),
            )
            self.conn.execute(
                "INSERT INTO workspace_context_revisions "
                "(workspace_id,object_id,role,revision,content,sha256,actor,reason) "
                "VALUES (%s,%s,%s,1,%s,%s,%s,'initial authored skill context')",
                (context.workspace_id, object_id, role, text, digest, actor),
            )
            self.conn.execute(
                "INSERT INTO workspace_context_events "
                "(workspace_id,object_id,role,revision,actor,event_type,payload) "
                "VALUES (%s,%s,%s,1,%s,'document.provisioned',%s)",
                (
                    context.workspace_id,
                    object_id,
                    role,
                    actor,
                    Jsonb(
                        {
                            "sha256": digest,
                            "agent_id": str(self.grant.agent_id),
                            "run_id": str(self.grant.run_id),
                            "session_id": str(self.grant.session_id),
                        }
                    ),
                ),
            )

    def execute(self, request: SkillRequest) -> dict[str, Any]:
        context = self.context
        if request.method == "skills.list":
            return published_skills(
                self.conn,
                context,
                limit=request.limit,
                after=request.after,
                setup_token=self.service.skill_setup_token,
            )
        if request.method == "skills.load":
            assert request.skill_id is not None and request.release_sha256 is not None
            return load_skill(
                self.conn, context, self.service, request.skill_id, request.release_sha256
            )
        if request.method == "skills.read":
            return read_skill_resource(self.conn, context, self.service, request)
        if request.method == "skills.create":
            assert request.name is not None and request.content is not None
            validate_skill(request.name, request.content)
            identity, object_id, name = skill_identity(
                context.workspace_id, self.grant.agent_id, request.name
            )
            row = self.conn.execute(
                "INSERT INTO workspace_context_skills "
                "(workspace_id,id,owner_principal_id,owner_agent_id,agent_object_id,slug,"
                "context_object_id,context_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (workspace_id,owner_agent_id,slug) DO NOTHING RETURNING *",
                (
                    context.workspace_id,
                    identity,
                    context.principal_id,
                    self.grant.agent_id,
                    self.session["agent_object_id"],
                    request.name,
                    object_id,
                    name,
                ),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("Skill name already exists in this agent's namespace")
            self._seed_context(object_id, name)
            digest = self._insert_version(identity, 1, request.content)
            return {**_summary(dict(row)), "sha256": digest, "reason": request.reason}
        assert request.skill_id is not None
        row = self._row(request.skill_id, write=True)
        if row["state"] != "active":
            raise ScopeDeniedError(_DENIED)
        if row["draft_revision"] != request.expected_revision:
            raise RevisionConflictError("Skill draft revision is stale")
        if request.method == "skills.save":
            assert request.content is not None
            validate_skill(row["slug"], request.content)
            revision = row["draft_revision"] + 1
            digest = self._insert_version(row["id"], revision, request.content)
            self.conn.execute(
                "UPDATE workspace_context_skills SET draft_revision=%s,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (revision, context.workspace_id, row["id"]),
            )
            row["draft_revision"] = revision
            return {**_summary(row), "sha256": digest, "reason": request.reason}
        if request.method == "skills.publish":
            version = self._version(row)
            self.conn.execute(
                "INSERT INTO workspace_context_skill_releases "
                "(workspace_id,skill_id,sha256,content_sha256,source_revision,session_id) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (workspace_id,skill_id,sha256) DO NOTHING",
                (
                    context.workspace_id,
                    row["id"],
                    version["sha256"],
                    version["sha256"],
                    version["revision"],
                    self.grant.session_id,
                ),
            )
            self.conn.execute(
                "UPDATE workspace_context_skills SET published=true,latest_sha256=%s,"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (version["sha256"], context.workspace_id, row["id"]),
            )
            row["latest_sha256"] = version["sha256"]
            return {**_summary(row), "sha256": version["sha256"], "reason": request.reason}
        if request.method == "skills.archive":
            self.conn.execute(
                "UPDATE workspace_context_skills SET state='archived',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, row["id"]),
            )
            row["state"] = "archived"
            return {**_summary(row), "reason": request.reason}
        raise InvalidInputError("Unsupported skill operation")
