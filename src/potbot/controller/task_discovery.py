"""Signed task shortlists and exact handoffs over existing capability owners.

This controller owns review history, never an installation, task grant, provider
endpoint, shared vector index or execution scheduler. Native inference is a
single explicit claim through the existing budgeted embedding owner.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.context.service import native_skill_setup_token
from potbot.controller.task_discovery_ranking import (
    normalize_constraints,
    policy_sha256,
    rank_task_candidates,
    semantic_pool,
)
from potbot.controller.task_discovery_sources import (
    collect_task_candidates,
    inspect_task_candidate,
)
from potbot.domain.auth import WorkspaceContext
from potbot.domain.discovery import text_value
from potbot.domain.embeddings import validate_embedding_name
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.memory.embedding_profile import EmbeddingProfile
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.tools.mcp_discovery import bounded_metadata
from potbot.tools.mcp_qualification_host import read_mcp_task_binding

SCHEMA = "potbot-task-discovery/v1"
MAX_ACTIVE_SEARCHES = 16
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ACTIVE = {"requested", "running"}
_TABLES = {
    "search": "task_discovery_searches",
    "result": "task_discovery_results",
    "selection": "task_discovery_selections",
}
_RESULT_FIELDS = {
    "taskBinding",
    "catalogueSha256",
    "typesSha256",
    "policySha256",
    "consumerSha256",
    "query",
    "constraints",
    "mode",
    "items",
    "excluded",
    "terms",
    "inferredClasses",
    "noSuitableCandidate",
    "semantic",
}
_SELECTION_FIELDS = {"taskBinding", "owner", "handoff"}


def _uuid(value: Any) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("Task discovery requires a UUID")
    return value


def _wire_uuid(value: Any) -> UUID:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("Task discovery requires a canonical UUID") from None


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("Task discovery requires an exact SHA-256")
    return value


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidInputError("Task discovery integer is outside its bound")
    return value


def _key(context: WorkspaceContext, value: Any) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise InvalidInputError("Task discovery requires a bounded stable command key")
    return content_hash([str(context.principal_id), value])


def _token_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 4096
        and all(33 <= ord(char) <= 126 for char in value)
    )


def _body(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "workspaceId": str(row["workspace_id"]),
        "principalId": str(row["principal_id"]),
        "id": str(row["id"]),
    }
    if kind == "search":
        result.update(
            episodeId=str(row["episode_id"]),
            revision=row["revision"],
            status=row["status"],
            payload=row["payload"],
            workerToken=str(row["worker_token"]) if row["worker_token"] else None,
        )
    else:
        result.update(searchId=str(row["search_id"]), sha256=row["sha256"])
        if kind == "selection":
            result.update(resultId=str(row["result_id"]), candidateId=row["candidate_id"])
    return result


def _mac(token: str, kind: str, row: dict[str, Any]) -> str:
    return hmac.new(
        token.encode(), canonical_json([SCHEMA, kind, _body(kind, row)]).encode(), hashlib.sha256
    ).hexdigest()


def discovery_consumer_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    modules = (
        "controller/task_discovery.py",
        "controller/task_discovery_sources.py",
        "controller/task_discovery_ranking.py",
        "controller/capability_discovery.py",
        "controller/discovery_installed.py",
        "controller/discovery_ranking.py",
        "controller/operation_catalogue.py",
        "controller/operation_discovery_metadata.py",
        "controller/mcp_discovery.py",
        "controller/mcp_qualification.py",
        "controller/tasks.py",
        "controller/budgets.py",
        "memory/embeddings.py",
        "memory/embedding_profile.py",
        "memory/local_encoder.py",
        "memory/supervised_encoder.py",
        "memory/embedding_worker.py",
        "api/task_discovery_cli.py",
        "context/skill_acquisition.py",
        "context/skill_packages.py",
        "cogs/contracts.py",
        "cogs/registry.py",
        "cogs/builtin.py",
        "models/providers/contracts.py",
        "persistence/artifacts.py",
        "semantics/catalog.py",
        "semantics/catalog.json",
        "semantics/service.py",
        "domain/jobs.py",
        "domain/syn.py",
    )
    pins = {}
    for relative in modules:
        path = root / relative
        if path.resolve(strict=True) != path or not path.is_file():
            raise ScopeDeniedError("Task discovery implementation is redirected")
        before, raw = path.stat(), path.read_bytes()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise StateMismatchError("Task discovery implementation changed during readback")
        pins[relative] = hashlib.sha256(raw).hexdigest()
    return content_hash({"schema": "potbot-task-discovery-consumer/v1", "modules": pins})


def embedding_documents(candidates: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    # Only descriptive data enters local inference. Authority, private arguments,
    # source URLs and qualification verdicts never become model instructions.
    return tuple(sorted((item["id"], item["description"]) for item in candidates))


def refinement_claim_sha256(context: WorkspaceContext, identity: UUID, worker: UUID) -> str:
    """Bind native accounting to one claim without publishing its bearer nonce."""
    return content_hash(
        {
            "schema": "potbot-task-discovery-native-claim/v1",
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "searchId": str(_uuid(identity)),
            "workerToken": str(_uuid(worker)),
        }
    )


class TaskDiscoveryService:
    def __init__(
        self,
        database: Database,
        *,
        setup_token: Any = native_skill_setup_token,
        clock: Any = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        if artifacts is not None and artifacts.db is not database:
            raise InvalidInputError("Task discovery and artifacts must share a database")
        self.database, self.setup_token = database, setup_token
        self.clock = clock
        self.artifacts = artifacts

    def _token(self) -> str | None:
        try:
            value = self.setup_token()
            return value if _token_valid(value) else None
        except DomainError:
            return None

    def _authorized(self, presented: str) -> str:
        token = self._token()
        if (
            token is None
            or not _token_valid(presented)
            or not hmac.compare_digest(token, presented)
        ):
            raise ScopeDeniedError("Task discovery requires current operator setup authority")
        return token

    @staticmethod
    def _scope(context: WorkspaceContext) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        }

    @staticmethod
    def _row(
        conn: Any, context: WorkspaceContext, kind: str, identity: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT * FROM {_TABLES[kind]} WHERE workspace_id=%s AND principal_id=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (context.workspace_id, context.principal_id, _uuid(identity)),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Task discovery record is unavailable in this scope")
        return dict(row)

    @staticmethod
    def _valid(conn: Any, kind: str, row: dict[str, Any], token: str | None) -> bool:
        if token is None or not hmac.compare_digest(row["mac"], _mac(token, kind, row)):
            return False
        if kind != "search":
            return bool(content_hash(row["payload"]) == row["sha256"])
        event = conn.execute(
            "SELECT revision,binding_sha256 FROM task_discovery_events WHERE workspace_id=%s AND principal_id=%s AND search_id=%s ORDER BY revision DESC LIMIT 1",
            (row["workspace_id"], row["principal_id"], row["id"]),
        ).fetchone()
        return bool(
            event
            and event["revision"] == row["revision"]
            and event["binding_sha256"] == content_hash(_body(kind, row))
        )

    @staticmethod
    def _save(
        conn: Any, row: dict[str, Any], token: str, action: str, *, insert: bool = False
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        if len(canonical_json(row["payload"]).encode()) > 1_500_000:
            raise InvalidInputError("Task discovery admission exceeds its byte allowance")
        mac = _mac(token, "search", row)
        if insert:
            saved = conn.execute(
                "INSERT INTO task_discovery_searches(workspace_id,id,principal_id,episode_id,revision,status,payload,worker_token,mac) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    row["workspace_id"],
                    row["id"],
                    row["principal_id"],
                    row["episode_id"],
                    row["revision"],
                    row["status"],
                    Jsonb(row["payload"]),
                    row["worker_token"],
                    mac,
                ),
            ).fetchone()
        else:
            saved = conn.execute(
                "UPDATE task_discovery_searches SET revision=%s,status=%s,payload=%s,worker_token=%s,mac=%s,updated_at=clock_timestamp() WHERE workspace_id=%s AND principal_id=%s AND id=%s RETURNING *",
                (
                    row["revision"],
                    row["status"],
                    Jsonb(row["payload"]),
                    row["worker_token"],
                    mac,
                    row["workspace_id"],
                    row["principal_id"],
                    row["id"],
                ),
            ).fetchone()
        conn.execute(
            "INSERT INTO task_discovery_events(workspace_id,id,principal_id,search_id,revision,action,binding_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                row["workspace_id"],
                uuid4(),
                row["principal_id"],
                row["id"],
                row["revision"],
                action,
                content_hash(_body("search", row)),
            ),
        )
        return dict(saved)

    @staticmethod
    def _expected(row: dict[str, Any], revision: int, digest: str) -> None:
        if _integer(revision, 1, 2**53 - 2) != row["revision"] or _sha(digest) != content_hash(
            _body("search", row)
        ):
            raise RevisionConflictError("Review the current task discovery revision and digest")

    @staticmethod
    def _space(conn: Any, context: WorkspaceContext, identity: UUID, name: str) -> dict[str, Any]:
        validate_embedding_name(name)
        row = conn.execute(
            "SELECT s.id,s.content_hash,s.content FROM embedding_spaces s JOIN embedding_views v ON (v.workspace_id,v.space_id)=(s.workspace_id,s.id) WHERE s.workspace_id=%s AND s.id=%s AND v.embedding_name=%s",
            (context.workspace_id, _uuid(identity), name),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("The named embedding profile is unavailable")
        profile = EmbeddingProfile(**row["content"])
        if profile.fingerprint != row["content_hash"] or len(profile.model) > 512:
            raise StateMismatchError("Retained embedding profile integrity failed")
        return {
            "spaceId": str(row["id"]),
            "embeddingName": name,
            "profileSha256": profile.fingerprint,
            "model": profile.model,
            "revision": profile.model_revision,
            "dimensions": profile.dimensions,
            "maxInputTokens": profile.max_input_tokens,
            "maxBatchSize": profile.max_batch_size,
        }

    def _collect(
        self, context: WorkspaceContext, episode_id: UUID, state_id: UUID
    ) -> dict[str, Any]:
        task = read_mcp_task_binding(
            self.database,
            context,
            episode_id=_uuid(episode_id),
            repository_state_id=_uuid(state_id),
        )
        token = self._token()
        sources = collect_task_candidates(
            self.database,
            context,
            task,
            setup_token=lambda: token,
            clock=self.clock,
            artifacts=self.artifacts,
        )
        with self.database.transaction(context) as conn:
            contract = conn.execute(
                "SELECT content,content_hash FROM contract_revisions WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, UUID(task["contractRevisionId"])),
            ).fetchone()
            if (
                contract is None
                or contract["content_hash"] != task["contractSha256"]
                or content_hash(contract["content"]) != task["contractSha256"]
            ):
                raise StateMismatchError("Task contract changed during discovery")
            capabilities = contract["content"]["capabilities"]
            if (
                not isinstance(capabilities, list)
                or len(capabilities) > 128
                or any(
                    not isinstance(item, str) or not 1 <= len(item) <= 256 for item in capabilities
                )
            ):
                raise InvalidInputError("Task capability declaration exceeds discovery bounds")
            views = conn.execute(
                "SELECT space_id,embedding_name FROM embedding_views WHERE workspace_id=%s ORDER BY space_id,embedding_name LIMIT 25",
                (context.workspace_id,),
            ).fetchall()
            spaces = [
                self._space(conn, context, row["space_id"], row["embedding_name"]) for row in views
            ]
        value = {
            "taskBinding": task,
            "taskStatus": "running",
            "taskCapabilities": capabilities,
            "catalogueSha256": sources["catalogueSha256"],
            "typesSha256": sources["typesSha256"],
            "policySha256": policy_sha256(),
            "consumerSha256": discovery_consumer_sha256(),
            "counts": sources["counts"],
            "embeddingSpaces": spaces,
        }
        # Registering an unrelated model does not change the reviewed candidate
        # set. The selected profile is separately pinned in a hybrid request.
        value["contextSha256"] = content_hash(
            {key: item for key, item in value.items() if key != "embeddingSpaces"}
        )
        return {"context": value, "candidates": sources["items"]}

    def context(
        self, context: WorkspaceContext, episode_id: UUID, state_id: UUID
    ) -> dict[str, Any]:
        return {
            **self._scope(context),
            "context": self._collect(context, episode_id, state_id)["context"],
        }

    def _reasons(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> list[str]:
        if not self._valid(conn, "search", row, token) or row["payload"].get(
            "retiredWithoutAdmission", False
        ):
            return ["admission_invalid"]
        task = row["payload"]["context"]["taskBinding"]
        try:
            current = self._collect(
                context, UUID(task["episodeId"]), UUID(task["repositoryStateId"])
            )["context"]
            if current["contextSha256"] != row["payload"]["context"]["contextSha256"]:
                return ["discovery_context_changed"]
            embedding = row["payload"]["request"]["embedding"]
            if embedding is not None:
                space = self._space(
                    conn, context, UUID(embedding["spaceId"]), embedding["embeddingName"]
                )
                if space["profileSha256"] != embedding["profileSha256"]:
                    return ["embedding_profile_changed"]
        except (DomainError, OSError):
            return ["discovery_context_unavailable"]
        return []

    def _view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        payload = row["payload"]
        saved, request = payload["context"], payload["request"]
        valid = self._valid(conn, "search", row, token) and not payload.get(
            "retiredWithoutAdmission", False
        )
        reasons = self._reasons(conn, context, row, token)
        status = row["status"] if valid or row["status"] in _ACTIVE else "indeterminate"
        actions = ["cancel"] if row["status"] in _ACTIVE else []
        if valid and not reasons and status == "completed" and payload["resultId"] is not None:
            result = self._row(conn, context, "result", UUID(payload["resultId"]))
            if self._valid(conn, "result", result, token) and result["payload"].get("items"):
                actions.append("select")
        return {
            "id": str(row["id"]),
            "revision": row["revision"],
            "bindingSha256": content_hash(_body("search", row)),
            "status": status,
            **{
                key: saved[key]
                for key in (
                    "taskBinding",
                    "contextSha256",
                    "catalogueSha256",
                    "typesSha256",
                    "policySha256",
                    "consumerSha256",
                )
            },
            **{key: request[key] for key in ("query", "constraints", "mode", "limit", "embedding")},
            "candidateCount": len(payload["candidates"]) if valid else 0,
            "resultId": payload["resultId"] if valid else None,
            "resultSha256": payload["resultSha256"] if valid else None,
            "selectedCandidateId": payload["selectedCandidateId"] if valid else None,
            "selectionId": payload["selectionId"] if valid else None,
            "reasonCode": payload["reasonCode"] if valid else "admission_invalid",
            "approvalValid": valid,
            "authorityCurrent": not reasons,
            "reasonCodes": reasons,
            "actions": actions,
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    def searches(
        self,
        context: WorkspaceContext,
        *,
        episode_id: UUID | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        _integer(limit, 1, 25)
        cursor = _wire_uuid(after) if after is not None else None
        if episode_id is not None:
            _uuid(episode_id)
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM task_discovery_searches WHERE workspace_id=%s AND principal_id=%s AND (%s::uuid IS NULL OR episode_id=%s) AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (
                    context.workspace_id,
                    context.principal_id,
                    episode_id,
                    episode_id,
                    cursor,
                    cursor,
                    limit + 1,
                ),
            ).fetchall()
            token = self._token()
            return {
                **self._scope(context),
                "items": [self._view(conn, context, dict(row), token) for row in rows[:limit]],
                "nextCursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    def search(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "search": self._view(
                    conn, context, self._row(conn, context, "search", identity), self._token()
                ),
            }

    def _result_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        if set(row["payload"]) != _RESULT_FIELDS or content_hash(row["payload"]) != row["sha256"]:
            raise StateMismatchError("Task discovery result integrity failed")
        search = self._row(conn, context, "search", row["search_id"])
        valid = self._valid(conn, "result", row, token)
        reasons = self._reasons(conn, context, search, token)
        if not valid:
            reasons.append("result_admission_invalid")
        if (
            search["payload"]["resultId"] != str(row["id"])
            or search["payload"]["resultSha256"] != row["sha256"]
        ):
            reasons.append("result_not_current")
        return {
            **{key: row["payload"][key] for key in _RESULT_FIELDS},
            "id": str(row["id"]),
            "searchId": str(row["search_id"]),
            "sha256": row["sha256"],
            "approvalValid": valid,
            "authorityCurrent": not reasons,
            "reasonCodes": reasons,
            "recordedAt": row["recorded_at"].isoformat(),
        }

    def result(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "result": self._result_view(
                    conn, context, self._row(conn, context, "result", identity), self._token()
                ),
            }

    def _selection_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        if (
            set(row["payload"]) != _SELECTION_FIELDS
            or content_hash(row["payload"]) != row["sha256"]
        ):
            raise StateMismatchError("Task discovery selection integrity failed")
        search = self._row(conn, context, "search", row["search_id"])
        valid = self._valid(conn, "selection", row, token)
        reasons = self._reasons(conn, context, search, token)
        if not valid:
            reasons.append("selection_admission_invalid")
        if (
            search["payload"]["selectionId"] != str(row["id"])
            or search["payload"]["selectedCandidateId"] != row["candidate_id"]
        ):
            reasons.append("selection_not_current")
        result = self._row(conn, context, "result", row["result_id"])
        if (
            not self._valid(conn, "result", result, token)
            or result["search_id"] != search["id"]
            or search["payload"]["resultId"] != str(result["id"])
            or search["payload"]["resultSha256"] != result["sha256"]
            or not any(
                candidate["id"] == row["candidate_id"]
                and {key: candidate[key] for key in ("kind", "ownerId", "bindingId", "ownerSha256")}
                == row["payload"]["owner"]
                for candidate in result["payload"]["items"]
            )
        ):
            reasons.append("selection_result_not_current")
        return {
            **{key: row["payload"][key] for key in _SELECTION_FIELDS},
            "id": str(row["id"]),
            "searchId": str(row["search_id"]),
            "resultId": str(row["result_id"]),
            "candidateId": row["candidate_id"],
            "sha256": row["sha256"],
            "approvalValid": valid,
            "authorityCurrent": not reasons,
            "reasonCodes": reasons,
            "recordedAt": row["recorded_at"].isoformat(),
        }

    def selection(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "selection": self._selection_view(
                    conn, context, self._row(conn, context, "selection", identity), self._token()
                ),
            }

    @staticmethod
    def _request(value: Any) -> dict[str, Any]:
        value = bounded_metadata(value, maximum=8192)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "episodeId",
                "stateId",
                "contextSha256",
                "query",
                "constraints",
                "mode",
                "limit",
                "embedding",
                "confirm",
            }
            or value["confirm"] is not True
        ):
            raise InvalidInputError("Review and confirm the exact task discovery request")
        _wire_uuid(value["episodeId"])
        _wire_uuid(value["stateId"])
        _sha(value["contextSha256"])
        text_value(value["query"], "Task discovery query", 500)
        _integer(value["limit"], 1, 10)
        if not isinstance(value["mode"], str) or value["mode"] not in {"lexical", "hybrid"}:
            raise InvalidInputError("Task discovery mode must be lexical or hybrid")
        embedding = value["embedding"]
        if value["mode"] == "lexical":
            if embedding is not None:
                raise InvalidInputError("Lexical discovery does not accept an embedding profile")
        else:
            if not isinstance(embedding, dict) or set(embedding) != {
                "spaceId",
                "embeddingName",
                "profileSha256",
                "maxCandidates",
                "runtimeBudgetMs",
            }:
                raise InvalidInputError("Hybrid discovery requires an exact registered profile")
            _wire_uuid(embedding["spaceId"])
            validate_embedding_name(embedding["embeddingName"])
            _sha(embedding["profileSha256"])
            _integer(embedding["maxCandidates"], 1, 16)
            _integer(embedding["runtimeBudgetMs"], 1, 120_000)
        return {**value, "constraints": normalize_constraints(value["constraints"])}

    def _retain_result(
        self,
        conn: Any,
        context: WorkspaceContext,
        row: dict[str, Any],
        token: str,
        ranked: dict[str, Any],
        semantic: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        saved, request = row["payload"]["context"], row["payload"]["request"]
        payload = {
            **{
                key: saved[key]
                for key in (
                    "taskBinding",
                    "catalogueSha256",
                    "typesSha256",
                    "policySha256",
                    "consumerSha256",
                )
            },
            **{key: request[key] for key in ("query", "constraints", "mode")},
            **{
                key: ranked[key]
                for key in ("items", "excluded", "terms", "inferredClasses", "noSuitableCandidate")
            },
            "semantic": semantic,
        }
        bounded_metadata(payload, maximum=900_000)
        result = {
            "workspace_id": context.workspace_id,
            "principal_id": context.principal_id,
            "id": uuid4(),
            "search_id": row["id"],
            "payload": payload,
            "sha256": content_hash(payload),
        }
        conn.execute(
            "INSERT INTO task_discovery_results(workspace_id,id,principal_id,search_id,payload,sha256,mac) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                context.workspace_id,
                result["id"],
                context.principal_id,
                row["id"],
                Jsonb(payload),
                result["sha256"],
                _mac(token, "result", result),
            ),
        )
        row["payload"].update(
            resultId=str(result["id"]), resultSha256=result["sha256"], reasonCode=None
        )
        row.update(status="completed", worker_token=None, revision=row["revision"] + 1)
        return self._save(conn, row, token, "completed")

    def create(
        self, context: WorkspaceContext, value: Any, key: str, *, setup_token: str
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = self._request(value)
        command_key = _key(context, key)
        with self.database.transaction(context) as conn:

            def admit() -> dict[str, Any]:
                # Serialize the small pending allowance for this principal. The
                # native budget reservation remains owned by EmbeddingService.
                lock = int.from_bytes(
                    hashlib.sha256(
                        canonical_json(
                            [
                                "task_discovery_pending",
                                str(context.workspace_id),
                                str(context.principal_id),
                            ]
                        ).encode()
                    ).digest()[:8],
                    "big",
                    signed=True,
                )
                conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock,))
                count = conn.execute(
                    "SELECT count(*) AS n FROM task_discovery_searches WHERE workspace_id=%s AND principal_id=%s AND status IN ('requested','running')",
                    (context.workspace_id, context.principal_id),
                ).fetchone()["n"]
                if count >= MAX_ACTIVE_SEARCHES:
                    raise StateMismatchError(
                        "Cancel or finish an active search before creating another"
                    )
                collected = self._collect(
                    context, UUID(request["episodeId"]), UUID(request["stateId"])
                )
                saved, candidates = collected["context"], collected["candidates"]
                if saved["contextSha256"] != request["contextSha256"]:
                    raise RevisionConflictError("Review the current task discovery context")
                embedding = request["embedding"]
                pool = []
                if embedding is not None:
                    space = self._space(
                        conn, context, UUID(embedding["spaceId"]), embedding["embeddingName"]
                    )
                    if space["profileSha256"] != embedding["profileSha256"]:
                        raise RevisionConflictError("Review the exact current embedding profile")
                    pool = semantic_pool(
                        [item for item in candidates if item["description"]],
                        request["query"],
                        request["constraints"],
                        task_capabilities=saved["taskCapabilities"],
                        limit=embedding["maxCandidates"],
                    )
                    documents = embedding_documents(pool)
                    if (
                        len(request["query"].encode())
                        + sum(len(text.encode()) for _, text in documents)
                        > 64 * 1024
                    ):
                        raise InvalidInputError(
                            "Reduce the candidate allowance to fit transient input bounds"
                        )
                row = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": uuid4(),
                    "episode_id": UUID(request["episodeId"]),
                    "revision": 1,
                    "status": "requested",
                    "worker_token": None,
                    "payload": {
                        "context": saved,
                        "request": request,
                        "candidates": candidates,
                        "semanticCandidates": pool,
                        "resultId": None,
                        "resultSha256": None,
                        "selectionId": None,
                        "selectedCandidateId": None,
                        "reasonCode": None,
                    },
                }
                row = self._save(conn, row, token, "requested", insert=True)
                if request["mode"] == "lexical" or not pool:
                    ranked = rank_task_candidates(
                        candidates,
                        request["query"],
                        request["constraints"],
                        task_capabilities=saved["taskCapabilities"],
                        limit=request["limit"],
                    )
                    row = self._retain_result(conn, context, row, token, ranked, None)
                return {**self._scope(context), "search": self._view(conn, context, row, token)}

            # The original body, including aliases and ordering, owns the key;
            # canonical normalization is only the retained ranking input.
            return idempotent_command(
                conn, context, "task.discovery.create", command_key, value, admit
            )

    def cancel(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = {
            "id": str(_uuid(identity)),
            "revision": _integer(revision, 1, 2**53 - 2),
            "digest": _sha(digest),
        }
        with self.database.transaction(context) as conn:

            def stop() -> dict[str, Any]:
                row = self._row(conn, context, "search", identity, lock=True)
                self._expected(row, revision, digest)
                if row["status"] not in _ACTIVE:
                    raise RevisionConflictError("Only an active search can be cancelled")
                if not self._valid(conn, "search", row, token):
                    row["payload"]["retiredWithoutAdmission"] = True
                row["payload"]["reasonCode"] = "search_cancelled"
                row.update(
                    status="cancelled",
                    worker_token=None,
                    revision=self._retirement_revision(conn, row),
                )
                saved = self._save(conn, row, token, "cancelled")
                return {**self._scope(context), "search": self._view(conn, context, saved, token)}

            return idempotent_command(
                conn, context, "task.discovery.cancel", _key(context, key), request, stop
            )

    @staticmethod
    def _retirement_revision(conn: Any, row: dict[str, Any]) -> int:
        # A rollback of a formerly signed row is invalid against its immutable
        # event head. Negative retirement must advance beyond that head rather
        # than collide with a revision the untrusted row omitted.
        head = conn.execute(
            "SELECT max(revision) AS revision FROM task_discovery_events WHERE workspace_id=%s AND principal_id=%s AND search_id=%s",
            (row["workspace_id"], row["principal_id"], row["id"]),
        ).fetchone()["revision"]
        return _integer(max(row["revision"], head or 0) + 1, 1, 2**53 - 2)

    def _current_result(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        if self._reasons(conn, context, row, token) or row["payload"]["resultId"] is None:
            raise ScopeDeniedError(
                "The search is no longer admitted for the current task and sources"
            )
        result = self._row(conn, context, "result", UUID(row["payload"]["resultId"]))
        view = self._result_view(conn, context, result, token)
        if not view["approvalValid"] or not view["authorityCurrent"]:
            raise ScopeDeniedError("The shortlist is no longer current")
        return result

    def select(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        candidate_id: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        token = self._authorized(setup_token)
        request = {
            "id": str(_uuid(identity)),
            "revision": _integer(revision, 1, 2**53 - 2),
            "digest": _sha(digest),
            "candidateId": _sha(candidate_id),
        }
        with self.database.transaction(context) as conn:

            def choose() -> dict[str, Any]:
                row = self._row(conn, context, "search", identity, lock=True)
                self._expected(row, revision, digest)
                if row["status"] != "completed":
                    raise RevisionConflictError("Select once from an exact completed shortlist")
                result = self._current_result(conn, context, row, token)
                candidate = next(
                    (item for item in result["payload"]["items"] if item["id"] == candidate_id),
                    None,
                )
                if candidate is None:
                    raise ScopeDeniedError(
                        "Selection requires an included candidate from this result"
                    )
                payload = {
                    "taskBinding": row["payload"]["context"]["taskBinding"],
                    "owner": {
                        key: candidate[key]
                        for key in ("kind", "ownerId", "bindingId", "ownerSha256")
                    },
                    "handoff": {
                        "kind": candidate["kind"],
                        "identity": candidate["ownerId"],
                        "bindingId": candidate["bindingId"],
                    },
                }
                selected = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": uuid4(),
                    "search_id": identity,
                    "result_id": result["id"],
                    "candidate_id": candidate_id,
                    "payload": payload,
                    "sha256": content_hash(payload),
                }
                retained = conn.execute(
                    "INSERT INTO task_discovery_selections(workspace_id,id,principal_id,search_id,result_id,candidate_id,payload,sha256,mac) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        context.workspace_id,
                        selected["id"],
                        context.principal_id,
                        identity,
                        result["id"],
                        candidate_id,
                        Jsonb(payload),
                        selected["sha256"],
                        _mac(token, "selection", selected),
                    ),
                ).fetchone()
                row["payload"].update(
                    selectionId=str(selected["id"]), selectedCandidateId=candidate_id
                )
                row.update(status="selected", revision=row["revision"] + 1)
                self._save(conn, row, token, "selected")
                return {
                    **self._scope(context),
                    "selection": self._selection_view(conn, context, dict(retained), token),
                }

            return idempotent_command(
                conn, context, "task.discovery.select", _key(context, key), request, choose
            )

    def inspect_candidate(
        self, context: WorkspaceContext, identity: UUID, candidate_id: str
    ) -> dict[str, Any]:
        _sha(candidate_id)
        with self.database.transaction(context) as conn:
            row = self._row(conn, context, "search", identity)
            result = self._current_result(conn, context, row, self._token())
            candidate = next(
                (item for item in result["payload"]["items"] if item["id"] == candidate_id), None
            )
            if candidate is None:
                raise ScopeDeniedError("Inspect a candidate included in this shortlist")
        retained = next(item for item in row["payload"]["candidates"] if item["id"] == candidate_id)
        inspected = inspect_task_candidate(
            self.database,
            context,
            retained,
            setup_token=self.setup_token,
            clock=self.clock,
            artifacts=self.artifacts,
        )
        if any(
            inspected[key] != candidate[key] for key in ("id", "kind", "ownerId", "ownerSha256")
        ):
            raise StateMismatchError("The selected owner changed before inspection")
        detail = bounded_metadata(inspected["detail"], maximum=128 * 1024)
        # Currentness is checked again after lazy owner readback. Never replace
        # retained evidence with a different live candidate under the same ID.
        with self.database.transaction(context) as conn:
            self._current_result(
                conn, context, self._row(conn, context, "search", identity), self._token()
            )
        return {**self._scope(context), "candidate": candidate, "detail": detail}

    def execution_replay(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any] | None:
        token = self._authorized(setup_token)
        request = {
            "id": str(_uuid(identity)),
            "revision": _integer(revision, 1, 2**53 - 2),
            "digest": _sha(digest),
        }
        with self.database.transaction(context) as conn:
            prior = conn.execute(
                "SELECT request_hash FROM command_results WHERE workspace_id=%s AND principal_id=%s AND operation='task.discovery.claim' AND idempotency_key=%s",
                (context.workspace_id, context.principal_id, _key(context, key)),
            ).fetchone()
            if prior is None:
                return None
            if prior["request_hash"] != content_hash(request):
                raise RevisionConflictError(
                    "This native command key belongs to another exact search"
                )
            return {
                **self._scope(context),
                "search": self._view(
                    conn, context, self._row(conn, context, "search", identity), token
                ),
            }

    @staticmethod
    def _native_view(conn: Any, context: WorkspaceContext, row: dict[str, Any]) -> dict[str, Any]:
        episode = conn.execute(
            "SELECT repository_id FROM episodes WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, row["episode_id"]),
        ).fetchone()
        if episode is None:
            raise ScopeDeniedError("The discovery task repository is unavailable")
        return {
            "workerToken": str(row["worker_token"]),
            "repositoryId": str(episode["repository_id"]),
            **{key: row["payload"][key] for key in ("context", "request", "semanticCandidates")},
        }

    def claim(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
        fresh_only: bool = True,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = {
            "id": str(_uuid(identity)),
            "revision": _integer(revision, 1, 2**53 - 2),
            "digest": _sha(digest),
        }
        with self.database.transaction(context) as conn:
            created = False

            def acquire() -> dict[str, Any]:
                nonlocal created
                row = self._row(conn, context, "search", identity, lock=True)
                self._expected(row, revision, digest)
                if (
                    row["status"] != "requested"
                    or row["payload"]["request"]["mode"] != "hybrid"
                    or not row["payload"]["semanticCandidates"]
                ):
                    raise RevisionConflictError(
                        "This search is not waiting for a native refinement"
                    )
                if self._reasons(conn, context, row, token):
                    raise ScopeDeniedError(
                        "The native refinement no longer matches its reviewed task"
                    )
                row.update(status="running", worker_token=uuid4(), revision=row["revision"] + 1)
                row["payload"]["claimedAt"] = (
                    conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"].isoformat()
                )
                saved = self._save(conn, row, token, "claimed")
                created = True
                # Only the claim identity is journalled. Private text already
                # belongs to the principal-scoped admission, not the receipt.
                return {"id": str(identity), "workerToken": str(saved["worker_token"])}

            result = idempotent_command(
                conn, context, "task.discovery.claim", _key(context, key), request, acquire
            )
            row = self._row(conn, context, "search", identity)
            if not created:
                # Even native callers cannot silently dispatch an old claim.
                return {
                    "replay": {
                        **self._scope(context),
                        "search": self._view(conn, context, row, token),
                    }
                }
            if str(row["worker_token"]) != result["workerToken"]:
                raise ScopeDeniedError("The native refinement was fenced before dispatch")
            return self._native_view(conn, context, row)

    def _claimed(
        self, conn: Any, context: WorkspaceContext, identity: UUID, worker: UUID, *, lock: bool
    ) -> tuple[dict[str, Any], str]:
        row = self._row(conn, context, "search", identity, lock=lock)
        token = self._token()
        if (
            row["status"] != "running"
            or row["worker_token"] != _uuid(worker)
            or self._reasons(conn, context, row, token)
        ):
            raise ScopeDeniedError("The native discovery claim has been fenced")
        assert token is not None
        return row, token

    def validate_claim(
        self, context: WorkspaceContext, identity: UUID, worker: UUID
    ) -> dict[str, Any]:
        # This callback may run while another connection holds budget/task
        # locks. It must remain an ordinary read with no FOR UPDATE.
        with self.database.transaction(context) as conn:
            row, _token = self._claimed(conn, context, identity, worker, lock=False)
            return self._native_view(conn, context, row)

    def _semantic_report(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], value: Any
    ) -> tuple[dict[str, int], dict[str, Any]]:
        value = bounded_metadata(value, maximum=32 * 1024)
        fields = {
            "schema",
            "spaceId",
            "embeddingName",
            "profileSha256",
            "querySha256",
            "documentsSha256",
            "scores",
            "calls",
            "inputTokens",
            "elapsedMs",
            "reservationIds",
            "usageOrigin",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != "potbot-transient-ranking/v1"
        ):
            raise InvalidInputError("Native refinement requires the exact embedding result")
        request = row["payload"]["request"]
        embedding = request["embedding"]
        documents = embedding_documents(row["payload"]["semanticCandidates"])
        expected = {key: embedding[key] for key in ("spaceId", "embeddingName", "profileSha256")}
        expected.update(
            querySha256=hashlib.sha256(request["query"].encode()).hexdigest(),
            documentsSha256=content_hash(
                [{"id": identity, "text": text} for identity, text in documents]
            ),
        )
        if any(value[key] != item for key, item in expected.items()):
            raise StateMismatchError("Native refinement inputs differ from their exact admission")
        scores = value["scores"]
        if not isinstance(scores, list) or len(scores) != len(documents):
            raise InvalidInputError("Native refinement must score every admitted candidate")
        by_id = {}
        for score in scores:
            if not isinstance(score, dict) or set(score) != {"id", "scoreMicros"}:
                raise InvalidInputError("Native refinement score is malformed")
            identity = _sha(score["id"])
            if identity in by_id:
                raise InvalidInputError("Native refinement duplicated a candidate")
            by_id[identity] = _integer(score["scoreMicros"], -1_000_000, 1_000_000)
        if set(by_id) != {identity for identity, _text in documents}:
            raise ScopeDeniedError("Native refinement scores contain an unadmitted candidate")
        calls = _integer(value["calls"], 2, 17)
        tokens = _integer(value["inputTokens"], 1, 17 * 32768)
        elapsed = _integer(value["elapsedMs"], 0, 2**53 - 1)
        reservations = value["reservationIds"]
        if (
            not isinstance(reservations, list)
            or len(reservations) != 1
            or value["usageOrigin"] not in {"synthetic", "measured", "provider_reported"}
        ):
            raise InvalidInputError("Native refinement requires its exact settled usage")
        reservation_id = _wire_uuid(reservations[0])
        usage = {"calls": calls, "tokens": tokens, "tool_ms": elapsed}
        reservation = conn.execute(
            "SELECT * FROM budget_reservations WHERE workspace_id=%s AND episode_id=%s AND id=%s",
            (context.workspace_id, row["episode_id"], reservation_id),
        ).fetchone()
        settled = conn.execute(
            "SELECT amounts,origin FROM budget_usage WHERE workspace_id=%s AND episode_id=%s AND reservation_id=%s",
            (context.workspace_id, row["episode_id"], reservation_id),
        ).fetchone()
        if (
            reservation is None
            or settled is None
            or reservation["status"] != "settled"
            or reservation["actual"] != usage
            or settled["amounts"] != usage
            or reservation["origin"] != value["usageOrigin"]
            or settled["origin"] != value["usageOrigin"]
            or not reservation["reservation_key"].startswith("embedding-transient-")
        ):
            raise ScopeDeniedError("Native refinement is missing matching settled task usage")
        evidence = conn.execute(
            "SELECT details FROM evidence_events WHERE workspace_id=%s AND episode_id=%s AND event_type='embedding_transient_ranking' AND details->>'reservation_id'=%s",
            (context.workspace_id, row["episode_id"], str(reservation_id)),
        ).fetchall()
        expected_evidence = {
            "schema": "potbot-transient-ranking-evidence/v1",
            "space_id": embedding["spaceId"],
            "embedding_name": embedding["embeddingName"],
            "profile_hash": embedding["profileSha256"],
            "query_hash": expected["querySha256"],
            "documents_hash": expected["documentsSha256"],
            "document_count": len(documents),
            "outcome": "completed",
            "known_calls": calls,
            "known_input_tokens": tokens,
            "usage_origin": value["usageOrigin"],
            "scores_sha256": content_hash(sorted(scores, key=lambda item: item["id"])),
            "request_binding_sha256": refinement_claim_sha256(
                context, row["id"], row["worker_token"]
            ),
        }
        if (
            len(evidence) != 1
            or any(
                evidence[0]["details"].get(key) != item for key, item in expected_evidence.items()
            )
            or reservation["created_at"] < datetime.fromisoformat(row["payload"]["claimedAt"])
        ):
            raise ScopeDeniedError(
                "Native refinement does not have the current claim's input-bound evidence"
            )
        summary = {key: value[key] for key in fields - {"schema", "scores"}}
        summary["candidateCount"] = len(documents)
        return {identity: max(0, score) for identity, score in by_id.items()}, summary

    def complete(
        self, context: WorkspaceContext, identity: UUID, worker: UUID, value: Any
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row, token = self._claimed(conn, context, identity, worker, lock=True)
            scores, summary = self._semantic_report(conn, context, row, value)
            request, saved = row["payload"]["request"], row["payload"]["context"]
            ranked = rank_task_candidates(
                row["payload"]["candidates"],
                request["query"],
                request["constraints"],
                task_capabilities=saved["taskCapabilities"],
                semantic_scores=scores,
                limit=request["limit"],
            )
            row = self._retain_result(conn, context, row, token, ranked, summary)
            return {**self._scope(context), "search": self._view(conn, context, row, token)}

    def fail(
        self, context: WorkspaceContext, identity: UUID, worker: UUID, reason: str
    ) -> dict[str, Any]:
        if reason not in {
            "search_failed",
            "search_cancelled",
            "search_indeterminate",
            "authority_changed",
        }:
            raise InvalidInputError("Unsupported native discovery failure reason")
        with self.database.transaction(context) as conn:
            row = self._row(conn, context, "search", identity, lock=True)
            token = self._token()
            if row["status"] != "running" or row["worker_token"] != _uuid(worker):
                return {**self._scope(context), "search": self._view(conn, context, row, token)}
            if token is None:
                raise ScopeDeniedError(
                    "Setup authority is unavailable; cancel the old claim after recovery"
                )
            if not self._valid(conn, "search", row, token):
                row["payload"]["retiredWithoutAdmission"] = True
            row["payload"]["reasonCode"] = reason
            row.update(
                status="indeterminate"
                if reason == "search_indeterminate"
                else "cancelled"
                if reason == "search_cancelled"
                else "failed",
                worker_token=None,
                revision=self._retirement_revision(conn, row),
            )
            saved = self._save(conn, row, token, "failed")
            return {**self._scope(context), "search": self._view(conn, context, saved, token)}
