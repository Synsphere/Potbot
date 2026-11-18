"""Signed discovery observations and exact handoffs to existing acquisition owners.

GET paths perform no network or process work. Explicit source refresh and exact
review may retrieve bounded data; a separate configured host measures one
retained review using the existing sandbox supervisor. Nothing here installs,
activates, schedules task execution or issues a capability grant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from potbot.context.service import native_skill_setup_token
from potbot.domain.auth import WorkspaceContext
from potbot.domain.discovery import ADAPTERS, KINDS, RegistryCandidate, text_value
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database, idempotent_command

SCHEMA = "potbot-discovery/v1"
PAGE_BYTES = 1_572_864
SNAPSHOT_BYTES = 2_097_152
MAX_SOURCES = 16
MAX_CANDIDATES = 256
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TABLES = {
    "source": "discovery_sources",
    "publisher": "discovery_publishers",
    "check": "discovery_checks",
}
_DENIED = "Discovery evidence is unavailable in this scope"


def _uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("A discovery UUID is required")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("An exact lowercase discovery SHA-256 is required")
    return value


def _key(context: WorkspaceContext, value: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise InvalidInputError("A bounded stable discovery command key is required")
    return content_hash([str(context.principal_id), value])


def _token_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 4096
        and all(33 <= ord(c) <= 126 for c in value)
    )


def native_registry_token() -> str | None:
    """Only the dedicated registry OIDC slot may reach the fixed skills.sh API."""
    try:
        from potbot.service_credentials import ServiceCredentials

        value = ServiceCredentials().store.get("discovery.skills_sh.oidc")
        return value if _token_valid(value) else None
    except Exception:
        return None


def _mac(token: str, purpose: str, value: dict[str, Any]) -> str:
    return hmac.new(
        token.encode(),
        canonical_json(["potbot-discovery/v1", purpose, value]).encode(),
        hashlib.sha256,
    ).hexdigest()


def source_config(value: Any) -> dict[str, Any]:
    fields = {"adapter", "name", "ttlSeconds", "curatedManifestSha256"}
    if not isinstance(value, dict) or set(value) != fields or value.get("adapter") not in ADAPTERS:
        raise InvalidInputError("Discovery source configuration is invalid")
    text_value(value["name"], "Source name", 96)
    if type(value["ttlSeconds"]) is not int or not 60 <= value["ttlSeconds"] <= 86400:
        raise InvalidInputError("Source freshness must be between 60 and 86400 seconds")
    if value["adapter"] == "curated":
        _sha(value["curatedManifestSha256"])
    elif value["curatedManifestSha256"] is not None:
        raise InvalidInputError("Only a curated source has a local manifest identity")
    return dict(value)


def _body(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "workspaceId": str(row["workspace_id"]),
        "principalId": str(row["principal_id"]),
        "id": str(row["id"]),
        "revision": row["revision"],
        "state": row["state"],
    }
    if kind == "source":
        result.update(
            config=row["config"],
            snapshotId=str(row["snapshot_id"]) if row["snapshot_id"] else None,
            query=row["query"],
            lastErrorCode=row["last_error_code"],
        )
    elif kind == "publisher":
        result.update(sourceId=str(row["source_id"]), name=row["name"], evidence=row["evidence"])
    else:
        result.update(
            sourceId=str(row["source_id"]),
            publisherId=str(row["publisher_id"]),
            snapshotId=str(row["snapshot_id"]),
            agentId=str(row["agent_id"]),
            payload=row["payload"],
            workerToken=str(row["worker_token"]) if row["worker_token"] else None,
        )
    return result


def _snapshot_body(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspaceId": str(row["workspace_id"]),
        "principalId": str(row["principal_id"]),
        "id": str(row["id"]),
        "sourceId": str(row["source_id"]),
        "configSha256": row["config_sha256"],
        "sha256": row["sha256"],
        "fetchedAt": row["fetched_at"].isoformat(),
        "expiresAt": row["expires_at"].isoformat(),
    }


class DiscoveryService:
    def __init__(
        self,
        database: Database,
        *,
        setup_token: Callable[[], str | None] = native_skill_setup_token,
        sources: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database, self.setup_token, self.source_client = database, setup_token, sources
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _scope(context: WorkspaceContext) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        }

    def _token(self) -> str | None:
        try:
            result = self.setup_token()
            return result if _token_valid(result) else None
        except Exception:
            return None

    def _authorized(self, presented: str) -> str:
        expected = self._token()
        if (
            not expected
            or not _token_valid(presented)
            or not hmac.compare_digest(expected, presented)
        ):
            raise ScopeDeniedError(
                "Discovery controls require the current independent setup capability"
            )
        return expected

    def _client(self) -> Any:
        if self.source_client is not None:
            return self.source_client
        from potbot.controller.registry_sources import RegistrySources

        return RegistrySources(skills_sh_token=native_registry_token)

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
            raise ScopeDeniedError(_DENIED)
        return dict(row)

    @staticmethod
    def _valid(conn: Any, kind: str, row: dict[str, Any], token: str | None) -> bool:
        if not token or not hmac.compare_digest(row["mac"], _mac(token, kind, _body(kind, row))):
            return False
        event = conn.execute(
            "SELECT revision,binding_sha256 FROM discovery_events WHERE workspace_id=%s AND subject_kind=%s AND subject_id=%s ORDER BY revision DESC LIMIT 1",
            (row["workspace_id"], kind, row["id"]),
        ).fetchone()
        return bool(
            event
            and event["revision"] == row["revision"]
            and event["binding_sha256"] == content_hash(_body(kind, row))
        )

    @staticmethod
    def _save(
        conn: Any, kind: str, row: dict[str, Any], token: str, action: str, *, insert: bool = False
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        fields = {
            "source": ("config", "revision", "state", "snapshot_id", "query", "last_error_code"),
            "publisher": ("source_id", "name", "evidence", "revision", "state"),
            "check": (
                "source_id",
                "publisher_id",
                "snapshot_id",
                "agent_id",
                "revision",
                "state",
                "payload",
                "worker_token",
            ),
        }[kind]
        row["mac"] = _mac(token, kind, _body(kind, row))
        columns = (*fields, "mac")
        values = [Jsonb(row[k]) if k in {"config", "payload"} else row[k] for k in columns]
        if insert:
            columns = ("workspace_id", "id", "principal_id", *columns)
            values = [row["workspace_id"], row["id"], row["principal_id"], *values]
            statement = f"INSERT INTO {_TABLES[kind]}({','.join(columns)}) VALUES({','.join('%s' for _ in columns)}) RETURNING *"
        else:
            statement = f"UPDATE {_TABLES[kind]} SET " + ",".join(f"{k}=%s" for k in columns)
            statement += ",updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s AND principal_id=%s RETURNING *"
            values.extend((row["workspace_id"], row["id"], row["principal_id"]))
        saved = dict(conn.execute(statement, values).fetchone())
        conn.execute(
            "INSERT INTO discovery_events(workspace_id,id,principal_id,subject_kind,subject_id,revision,action,binding_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["workspace_id"],
                uuid4(),
                row["principal_id"],
                kind,
                row["id"],
                row["revision"],
                action,
                content_hash(_body(kind, saved)),
            ),
        )
        return saved

    @staticmethod
    def _expected(kind: str, row: dict[str, Any], revision: int, digest: str) -> None:
        if type(revision) is not int or not 0 <= revision < 2**53 - 1:
            raise InvalidInputError("A bounded exact discovery revision is required")
        if row["revision"] != revision or content_hash(_body(kind, row)) != _sha(digest):
            raise RevisionConflictError("Discovery review changed; inspect its current revision")

    def _snapshot(
        self,
        conn: Any,
        context: WorkspaceContext,
        source: dict[str, Any],
        token: str | None,
        identity: UUID | None = None,
    ) -> dict[str, Any] | None:
        identity = identity or source["snapshot_id"]
        if identity is None:
            return None
        row = conn.execute(
            "SELECT * FROM discovery_snapshots WHERE workspace_id=%s AND principal_id=%s AND source_id=%s AND id=%s",
            (context.workspace_id, context.principal_id, source["id"], identity),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError(_DENIED)
        value = dict(row)
        if (
            not token
            or content_hash(value["payload"]) != value["sha256"]
            or value["config_sha256"] != content_hash(source["config"])
            or not hmac.compare_digest(value["mac"], _mac(token, "snapshot", _snapshot_body(value)))
        ):
            raise ScopeDeniedError("The discovery snapshot has no current independent signature")
        return value

    def _source_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        config = source_config(row["config"])
        valid = self._valid(conn, "source", row, token)
        try:
            snapshot = self._snapshot(conn, context, row, token)
        except ScopeDeniedError:
            snapshot = None
        return {
            "id": str(row["id"]),
            "name": config["name"],
            "adapter": config["adapter"],
            "revision": row["revision"],
            "state": row["state"],
            "configSha256": content_hash(config),
            "bindingSha256": content_hash(_body("source", row)),
            "approvalValid": valid,
            "snapshotId": str(row["snapshot_id"]) if row["snapshot_id"] else None,
            "snapshotSha256": snapshot["sha256"] if snapshot else None,
            "fetchedAt": snapshot["fetched_at"].isoformat() if snapshot else None,
            "expiresAt": snapshot["expires_at"].isoformat() if snapshot else None,
            "query": row["query"],
            "lastErrorCode": row["last_error_code"],
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
            "actions": []
            if row["state"] == "revoked"
            else (["disable"] if row["state"] == "active" else ["enable"]) + ["revoke"]
            if valid
            else ["revoke"],
        }

    def _publisher_view(self, conn: Any, row: dict[str, Any], token: str | None) -> dict[str, Any]:
        valid = self._valid(conn, "publisher", row, token)
        return {
            "id": str(row["id"]),
            "sourceId": str(row["source_id"]),
            "name": row["name"],
            "revision": row["revision"],
            "state": row["state"],
            "bindingSha256": content_hash(_body("publisher", row)),
            "approvalValid": valid,
            "evidence": row["evidence"],
            "updatedAt": row["updated_at"].isoformat(),
            "actions": [
                action
                for action, target in (
                    ("approve", "approved"),
                    ("block", "blocked"),
                    ("revoke", "revoked"),
                )
                if row["state"] != "revoked"
                and row["state"] != target
                and (valid or action == "revoke")
            ],
        }

    @staticmethod
    def _page(limit: int, after: str | None) -> UUID | None:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidInputError("Discovery pages require between one and fifty entries")
        if after is None:
            return None
        try:
            if str(UUID(after)) != after:
                raise ValueError
            return UUID(after)
        except (ValueError, TypeError, AttributeError):
            raise InvalidInputError("A canonical discovery page cursor is required") from None

    def add_source(
        self, context: WorkspaceContext, config: dict[str, Any], key: str, *, setup_token: str
    ) -> dict[str, Any]:
        token, config = self._authorized(setup_token), source_config(config)
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                # Serialize bounded source admission for this owner.
                conn.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                    (f"potbot:discovery:sources:{context.workspace_id}:{context.principal_id}",),
                )
                if (
                    conn.execute(
                        "SELECT count(*) AS n FROM discovery_sources WHERE workspace_id=%s "
                        "AND principal_id=%s AND state<>'revoked'",
                        (context.workspace_id, context.principal_id),
                    ).fetchone()["n"]
                    >= MAX_SOURCES
                ):
                    raise InvalidInputError("The discovery source allowance is exhausted")
                row = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": uuid4(),
                    "config": config,
                    "revision": 1,
                    "state": "active",
                    "snapshot_id": None,
                    "query": "",
                    "last_error_code": None,
                }
                saved = self._save(conn, "source", row, token, "source_admitted", insert=True)
                return {
                    **self._scope(context),
                    "source": self._source_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "discovery.source.add", _key(context, key), config, create
            )

    def sources(
        self, context: WorkspaceContext, *, limit: int = 25, after: str | None = None
    ) -> dict[str, Any]:
        cursor, token = self._page(limit, after), self._token()
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM discovery_sources WHERE workspace_id=%s AND principal_id=%s AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (context.workspace_id, context.principal_id, cursor, cursor, limit + 1),
            ).fetchall()
            items = [self._source_view(conn, context, dict(row), token) for row in rows[:limit]]
            return {
                **self._scope(context),
                "items": items,
                "nextCursor": items[-1]["id"] if len(rows) > limit else None,
            }

    def source(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "source": self._source_view(
                    conn, context, self._row(conn, context, "source", identity), self._token()
                ),
            }

    def publishers(
        self,
        context: WorkspaceContext,
        *,
        source_id: UUID | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        cursor, token = self._page(limit, after), self._token()
        if source_id is not None:
            _uuid(source_id)
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM discovery_publishers WHERE workspace_id=%s AND principal_id=%s AND (%s::uuid IS NULL OR source_id=%s) AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (
                    context.workspace_id,
                    context.principal_id,
                    source_id,
                    source_id,
                    cursor,
                    cursor,
                    limit + 1,
                ),
            ).fetchall()
            items = [self._publisher_view(conn, dict(row), token) for row in rows[:limit]]
            return {
                **self._scope(context),
                "items": items,
                "nextCursor": items[-1]["id"] if len(rows) > limit else None,
            }

    def control(
        self,
        context: WorkspaceContext,
        kind: str,
        identity: UUID,
        action: str,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        choices = {
            "source": {"enable": "active", "disable": "disabled", "revoke": "revoked"},
            "publisher": {"approve": "approved", "block": "blocked", "revoke": "revoked"},
        }
        if kind not in choices or action not in choices[kind]:
            raise InvalidInputError("Unsupported discovery control")
        request = {
            "id": str(_uuid(identity)),
            "action": action,
            "revision": revision,
            "digest": _sha(digest),
        }
        with self.database.transaction(context) as conn:

            def change() -> dict[str, Any]:
                row = self._row(conn, context, kind, identity)
                if kind == "publisher":
                    self._row(conn, context, "source", row["source_id"], lock=True)
                row = self._row(conn, context, kind, identity, lock=True)
                self._expected(kind, row, revision, digest)
                if action != "revoke" and not self._valid(conn, kind, row, token):
                    raise ScopeDeniedError(
                        "Nonterminal controls require a current independent signature"
                    )
                if row["state"] == "revoked":
                    raise ScopeDeniedError(
                        "A revoked source or publisher requires a new explicit source admission"
                    )
                if kind == "publisher":
                    source = self._row(conn, context, "source", row["source_id"], lock=True)
                    if action == "approve" and (
                        not self._valid(conn, "source", source, token)
                        or source["state"] != "active"
                    ):
                        raise ScopeDeniedError(
                            "Publisher review requires a current admitted source"
                        )
                # A new key may withdraw old authority but cannot restore an
                # expired or forged positive admission from database fields.
                latest = conn.execute(
                    "SELECT revision,binding_sha256 FROM discovery_events WHERE workspace_id=%s AND subject_kind=%s AND subject_id=%s ORDER BY revision DESC LIMIT 1",
                    (context.workspace_id, kind, identity),
                ).fetchone()
                if (
                    not latest
                    or latest["revision"] != row["revision"]
                    or latest["binding_sha256"] != content_hash(_body(kind, row))
                ):
                    raise ScopeDeniedError("Discovery control history is inconsistent")
                row.update(state=choices[kind][action], revision=row["revision"] + 1)
                saved = self._save(conn, kind, row, token, action)
                view = (
                    self._source_view(conn, context, saved, token)
                    if kind == "source"
                    else self._publisher_view(conn, saved, token)
                )
                return {**self._scope(context), kind: view}

            return idempotent_command(
                conn, context, f"discovery.{kind}.control", _key(context, key), request, change
            )

    def refresh(
        self,
        context: WorkspaceContext,
        identity: UUID,
        query: str,
        key: str,
        *,
        setup_token: str,
        curated_manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        text_value(query, "Registry refresh query", 256, empty=True)
        request = {
            "sourceId": str(_uuid(identity)),
            "query": query,
            "curatedManifestSha256": curated_manifest_sha256,
        }
        with self.database.transaction(context) as conn:

            def retrieve() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                source = self._row(conn, context, "source", identity, lock=True)
                if source["state"] != "active" or not self._valid(conn, "source", source, token):
                    raise ScopeDeniedError("Source refresh requires a current source admission")
                config = source_config(source["config"])
                if config["curatedManifestSha256"] != curated_manifest_sha256:
                    raise RevisionConflictError(
                        "The curated source differs from its admitted manifest"
                    )
                candidates: list[dict[str, Any]] = []
                pages: list[dict[str, Any]] = []
                cursor: str | None = None
                seen: set[str] = set()
                identities: set[tuple[str, str | None]] = set()
                error: str | None = None
                try:
                    for _ in range(10):
                        page = (
                            self._client()
                            .search(config["adapter"], query, cursor=cursor, limit=50)
                            .to_dict()
                        )
                        if page["status"] != "ok":
                            error = page["status"]
                            break
                        for raw in page["candidates"]:
                            candidate = RegistryCandidate.from_dict(raw)
                            value = candidate.to_dict()
                            if value["adapter"] != config["adapter"]:
                                raise InvalidInputError("Registry page changed adapter identity")
                            if (
                                config["adapter"] == "curated"
                                and value["sourceSha256"] != config["curatedManifestSha256"]
                            ):
                                raise RevisionConflictError(
                                    "Curated bytes differ from the admitted source manifest"
                                )
                            name = (value["canonicalId"], value["version"])
                            if name in identities:
                                raise InvalidInputError(
                                    "Registry pagination duplicated an exact identity"
                                )
                            identities.add(name)
                            candidates.append(value)
                        pages.append(
                            {
                                k: page[k]
                                for k in ("sourceSha256", "etag", "fetchedAt", "nextCursor")
                            }
                        )
                        if (
                            len(candidates) > MAX_CANDIDATES
                            or len(canonical_json(candidates).encode()) > SNAPSHOT_BYTES
                        ):
                            raise InvalidInputError(
                                "Discovery collection exceeds its explicit allowance"
                            )
                        cursor = page["nextCursor"]
                        if cursor is None:
                            break
                        if cursor in seen:
                            raise InvalidInputError("Registry pagination did not advance")
                        seen.add(cursor)
                    else:
                        raise InvalidInputError(
                            "Registry pagination exceeds its explicit allowance"
                        )
                except DomainError as failure:
                    error = str(failure.code)
                if error:
                    source.update(
                        revision=source["revision"] + 1, query=query, last_error_code=error
                    )
                    saved = self._save(conn, "source", source, token, "refresh_failed")
                    return {
                        **self._scope(context),
                        "source": self._source_view(conn, context, saved, token),
                    }
                now = self.clock()
                payload = {
                    "adapter": config["adapter"],
                    "query": query,
                    "candidates": candidates,
                    "pages": pages,
                    "complete": True,
                }
                if len(canonical_json(payload).encode()) > SNAPSHOT_BYTES:
                    raise InvalidInputError("Discovery snapshot exceeds its byte allowance")
                snapshot = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": uuid4(),
                    "source_id": source["id"],
                    "config_sha256": content_hash(config),
                    "sha256": content_hash(payload),
                    "fetched_at": now,
                    "expires_at": now + timedelta(seconds=config["ttlSeconds"]),
                }
                conn.execute(
                    "INSERT INTO discovery_snapshots(workspace_id,id,principal_id,source_id,config_sha256,payload,sha256,mac,fetched_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        snapshot["id"],
                        context.principal_id,
                        source["id"],
                        snapshot["config_sha256"],
                        Jsonb(payload),
                        snapshot["sha256"],
                        _mac(token, "snapshot", _snapshot_body(snapshot)),
                        now,
                        snapshot["expires_at"],
                    ),
                )
                for observed_candidate in candidates:
                    publisher = observed_candidate["publisher"]
                    current = conn.execute(
                        "SELECT id FROM discovery_publishers WHERE workspace_id=%s AND source_id=%s AND name=%s",
                        (context.workspace_id, source["id"], publisher["id"]),
                    ).fetchone()
                    if current is None:
                        self._save(
                            conn,
                            "publisher",
                            {
                                "workspace_id": context.workspace_id,
                                "principal_id": context.principal_id,
                                "id": uuid4(),
                                "source_id": source["id"],
                                "name": publisher["id"],
                                "evidence": publisher["evidence"],
                                "revision": 0,
                                "state": "unreviewed",
                            },
                            token,
                            "publisher_observed",
                            insert=True,
                        )
                source.update(
                    snapshot_id=snapshot["id"],
                    revision=source["revision"] + 1,
                    query=query,
                    last_error_code=None,
                )
                saved = self._save(conn, "source", source, token, "source_refreshed")
                return {
                    **self._scope(context),
                    "source": self._source_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "discovery.source.refresh", _key(context, key), request, retrieve
            )

    @staticmethod
    def _candidate_id(source: UUID, snapshot: UUID, digest: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"potbot:discovery:{source}:{snapshot}:{digest}")

    def _publisher_for(
        self,
        conn: Any,
        context: WorkspaceContext,
        source_id: UUID,
        name: str,
        *,
        lock: bool = False,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM discovery_publishers WHERE workspace_id=%s AND principal_id=%s AND source_id=%s AND name=%s"
            + (" FOR UPDATE" if lock else ""),
            (context.workspace_id, context.principal_id, source_id, name),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError(_DENIED)
        return dict(row)

    def _candidate_view(
        self,
        conn: Any,
        context: WorkspaceContext,
        source: dict[str, Any],
        snapshot: dict[str, Any],
        candidate: RegistryCandidate,
        token: str | None,
        *,
        identity: UUID | None = None,
    ) -> dict[str, Any]:
        observed = candidate.to_dict()
        publisher = self._publisher_for(conn, context, source["id"], observed["publisher"]["id"])
        source_valid, publisher_valid = (
            self._valid(conn, "source", source, token),
            self._valid(conn, "publisher", publisher, token),
        )
        stale = (
            source["snapshot_id"] != snapshot["id"]
            or snapshot["expires_at"] <= self.clock()
            or snapshot["fetched_at"] > self.clock()
            or source["last_error_code"] is not None
        )
        reasons = []
        if not source_valid:
            reasons.append("source_approval_invalid")
        if source["state"] != "active":
            reasons.append("source_not_active")
        if not publisher_valid or publisher["state"] != "approved":
            reasons.append("publisher_not_admitted")
        if stale:
            reasons.append("source_snapshot_stale")
        resolvable_summary = (
            observed["kind"] == "skill"
            and observed["adapter"] in {"clawhub", "skills_sh"}
            and observed["status"] in {"active", "unresolved"}
            and observed["distribution"]["kind"] == "inspect_only"
        )
        if observed["status"] != "active" and not resolvable_summary:
            reasons.append("candidate_" + observed["status"])
        if not observed["version"] and not resolvable_summary:
            reasons.append("exact_version_required")
        if observed["distribution"]["kind"] == "inspect_only" and not resolvable_summary:
            reasons.append("distribution_inspection_only")
        can_review = not reasons
        if resolvable_summary:
            reasons.append("exact_resolution_required")
        return {
            "id": str(
                identity or self._candidate_id(source["id"], snapshot["id"], candidate.sha256)
            ),
            "sourceId": str(source["id"]),
            "snapshotId": str(snapshot["id"]),
            "candidateSha256": candidate.sha256,
            "observation": observed,
            "sourceState": source["state"],
            "publisherId": str(publisher["id"]),
            "publisherState": publisher["state"],
            "sourceApprovalValid": source_valid,
            "publisherApprovalValid": publisher_valid,
            "stale": stale,
            "canReview": can_review,
            "reasonCodes": reasons,
            "executable": False,
            "score": None,
            "scoreReasons": [],
        }

    def _all_candidates(
        self, conn: Any, context: WorkspaceContext, token: str | None, source_id: UUID | None = None
    ) -> list[tuple[dict[str, Any], dict[str, Any], RegistryCandidate]]:
        if source_id is not None:
            _uuid(source_id)
        rows = conn.execute(
            "SELECT * FROM discovery_sources WHERE workspace_id=%s AND principal_id=%s "
            "AND state<>'revoked' AND (%s::uuid IS NULL OR id=%s) ORDER BY id LIMIT %s",
            (context.workspace_id, context.principal_id, source_id, source_id, MAX_SOURCES + 1),
        ).fetchall()
        if len(rows) > MAX_SOURCES:
            raise ScopeDeniedError("Discovery source inventory exceeds its admitted allowance")
        result: list[tuple[dict[str, Any], dict[str, Any], RegistryCandidate]] = []
        for row in rows:
            source = dict(row)
            try:
                snapshot = self._snapshot(conn, context, source, token)
            except ScopeDeniedError:
                continue
            if snapshot is None:
                continue
            values = snapshot["payload"].get("candidates")
            if not isinstance(values, list) or len(values) > MAX_CANDIDATES:
                raise ScopeDeniedError("Discovery snapshot inventory is inconsistent")
            result.extend(
                (source, snapshot, RegistryCandidate.from_dict(value)) for value in values
            )
        return result

    def _find_candidate(
        self, conn: Any, context: WorkspaceContext, identity: UUID, token: str | None
    ) -> tuple[dict[str, Any], dict[str, Any], RegistryCandidate]:
        _uuid(identity)
        for source, snapshot, candidate in self._all_candidates(conn, context, token):
            if self._candidate_id(source["id"], snapshot["id"], candidate.sha256) == identity:
                return source, snapshot, candidate
        raise ScopeDeniedError("The candidate is no longer in a current retained snapshot")

    def candidates(
        self,
        context: WorkspaceContext,
        *,
        query: str = "",
        kind: str | None = None,
        source_id: UUID | None = None,
        limit: int = 25,
        after: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from potbot.controller.discovery_installed import installed_skill_preference
        from potbot.controller.discovery_ranking import rank_candidates

        self._page(limit, None)
        text_value(query, "Discovery query", 500, empty=True)
        if kind is not None and kind not in KINDS:
            raise InvalidInputError("Unknown discovery candidate kind")
        with self.database.transaction(context) as conn:
            token = self._token()
            observed = self._all_candidates(conn, context, token, source_id)
            views = [
                self._candidate_view(conn, context, source, snapshot, candidate, token)
                for source, snapshot, candidate in observed
                if kind is None or candidate.to_dict()["kind"] == kind
            ]
            publisher_ids = sorted({UUID(view["publisherId"]) for view in views})
            publisher_heads = conn.execute(
                "SELECT * FROM discovery_publishers WHERE workspace_id=%s AND principal_id=%s "
                "AND id=ANY(%s::uuid[]) ORDER BY id",
                (context.workspace_id, context.principal_id, publisher_ids),
            ).fetchall()
            installed, installed_bindings = (
                installed_skill_preference(
                    conn,
                    context,
                    token,
                    {
                        view["candidateSha256"]: RegistryCandidate.from_dict(view["observation"])
                        for view in views
                    }.values(),
                )
                if query or constraints
                else ([], [])
            )
            fingerprint = content_hash(
                {
                    "scope": [str(context.workspace_id), str(context.principal_id)],
                    "q": query,
                    "kind": kind,
                    "sourceId": str(source_id) if source_id else None,
                    "constraints": constraints,
                    "installed": installed_bindings,
                    "heads": sorted(
                        {
                            (str(s["id"]), content_hash(_body("source", s)), str(p["id"]))
                            for s, p, _ in observed
                        }
                    ),
                    "publishers": [
                        content_hash(_body("publisher", dict(row))) for row in publisher_heads
                    ],
                    "authority": sorted(
                        (
                            view["id"],
                            view["sourceApprovalValid"],
                            view["publisherApprovalValid"],
                            view["stale"],
                        )
                        for view in views
                    ),
                }
            )
            offset = 0
            if after is not None:
                try:
                    if len(after) > 512:
                        raise ValueError
                    cursor = json.loads(base64.urlsafe_b64decode(after + "=" * (-len(after) % 4)))
                    if (
                        set(cursor) != {"fingerprint", "offset"}
                        or cursor["fingerprint"] != fingerprint
                    ):
                        raise ValueError
                    offset = cursor["offset"]
                    if type(offset) is not int or not 0 <= offset <= MAX_SOURCES * MAX_CANDIDATES:
                        raise ValueError
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise RevisionConflictError(
                        "Discovery results changed; restart this result page"
                    ) from None
            if query or constraints:
                # Admission belongs to a source observation. Identical content
                # in a blocked source cannot deny an independently admitted copy.
                views = [
                    view
                    for view in views
                    if view["sourceApprovalValid"]
                    and view["sourceState"] == "active"
                    and view["publisherApprovalValid"]
                    and view["publisherState"] not in {"blocked", "revoked"}
                    and not view["stale"]
                ]
                unique = {
                    view["candidateSha256"]: RegistryCandidate.from_dict(view["observation"])
                    for view in views
                }
                ranking = rank_candidates(
                    list(unique.values()),
                    query,
                    constraints=constraints,
                    installed_sha256=installed,
                    limit=MAX_SOURCES * MAX_CANDIDATES,
                )
                ranked = {item["candidateSha256"]: item for item in ranking["items"]}
                views = [view for view in views if view["candidateSha256"] in ranked]
                for view in views:
                    score = ranked[view["candidateSha256"]]
                    view.update(score=score["score"], scoreReasons=score["reasons"])
                views.sort(key=lambda view: (-view["score"], view["candidateSha256"], view["id"]))
            else:
                views.sort(key=lambda view: view["id"])
            items, size = [], 0
            for view in views[offset : offset + limit]:
                count = len(canonical_json(view).encode())
                if size + count > PAGE_BYTES:
                    break
                items.append(view)
                size += count
            next_offset = offset + len(items)
            cursor = (
                base64.urlsafe_b64encode(
                    canonical_json({"fingerprint": fingerprint, "offset": next_offset}).encode()
                )
                .decode()
                .rstrip("=")
                if next_offset < len(views)
                else None
            )
            return {**self._scope(context), "items": items, "nextCursor": cursor}

    def candidate(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            token = self._token()
            source, snapshot, candidate = self._find_candidate(conn, context, identity, token)
            return {
                **self._scope(context),
                "candidate": self._candidate_view(
                    conn, context, source, snapshot, candidate, token
                ),
            }

    def _admission(
        self,
        conn: Any,
        context: WorkspaceContext,
        source_id: UUID,
        publisher_id: UUID,
        snapshot_id: UUID,
        token: str | None,
        *,
        lock: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
        # All decisions take source then publisher locks, matching refresh and
        # revocation. These locks span each short committing handoff transaction.
        source = self._row(conn, context, "source", source_id, lock=lock)
        publisher = self._row(conn, context, "publisher", publisher_id, lock=lock)
        if (
            not self._valid(conn, "source", source, token)
            or source["state"] != "active"
            or not self._valid(conn, "publisher", publisher, token)
            or publisher["state"] != "approved"
            or publisher["source_id"] != source_id
            or source["snapshot_id"] != snapshot_id
            or source["last_error_code"] is not None
        ):
            raise ScopeDeniedError("Discovery source or publisher admission is no longer current")
        snapshot = self._snapshot(conn, context, source, token, snapshot_id)
        if snapshot is None or not snapshot["fetched_at"] <= self.clock() < snapshot["expires_at"]:
            raise RevisionConflictError("The discovery snapshot expired; refresh and review again")
        binding = content_hash(
            {
                "source": _body("source", source),
                "publisher": _body("publisher", publisher),
                "snapshot": _snapshot_body(snapshot),
            }
        )
        return source, publisher, snapshot, binding

    def create_check(
        self,
        context: WorkspaceContext,
        candidate_id: UUID,
        snapshot_id: UUID,
        candidate_sha256: str,
        agent_id: UUID,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        from potbot.context.skill_acquisition import SkillAcquisition
        from potbot.context.skill_eligibility import parse_requirements, requirements_digest

        token = self._authorized(setup_token)
        request = {
            "candidateId": str(_uuid(candidate_id)),
            "snapshotId": str(_uuid(snapshot_id)),
            "candidateSha256": _sha(candidate_sha256),
            "agentId": str(_uuid(agent_id)),
        }
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                source, snapshot, selected = self._find_candidate(
                    conn, context, candidate_id, token
                )
                if selected.sha256 != candidate_sha256 or snapshot["id"] != snapshot_id:
                    raise RevisionConflictError(
                        "The requested candidate differs from the reviewed snapshot"
                    )
                value = selected.to_dict()
                publisher = self._publisher_for(
                    conn, context, source["id"], value["publisher"]["id"]
                )
                source, publisher, snapshot, binding = self._admission(
                    conn, context, source["id"], publisher["id"], snapshot_id, token, lock=True
                )
                view = self._candidate_view(conn, context, source, snapshot, selected, token)
                if not view["canReview"]:
                    raise ScopeDeniedError("This candidate has no admitted exact acquisition route")
                # A confirmed summary inspection acquires data only. An exact
                # version remains exact; registries without versioned releases
                # produce an immutable inspected file snapshot for the NEXT
                # explicit handoff decision, never an implicit latest install.
                resolved = (
                    selected
                    if value["adapter"] == "curated"
                    else self._client().inspect(value["adapter"], value["canonicalId"])
                    if value["version"] is None
                    else self._client().resolve(
                        value["adapter"], value["canonicalId"], value["version"]
                    )
                )
                resolved = RegistryCandidate.from_dict(resolved.to_dict())
                exact = resolved.to_dict()
                if (
                    any(
                        exact[k] != value[k]
                        for k in ("adapter", "canonicalId", "kind", "publisher")
                    )
                    or (value["version"] is not None and exact["version"] != value["version"])
                    or exact["status"] != "active"
                ):
                    raise RevisionConflictError("Registry resolution changed the selected identity")
                if (
                    value["distribution"]["kind"] != "inspect_only"
                    and resolved.sha256 != selected.sha256
                ):
                    raise RevisionConflictError(
                        "An exact release changed after the retained source snapshot"
                    )
                check_id = uuid4()
                distribution = exact["distribution"]
                package_id: str | None = None
                package_release: str | None = None
                package_source: str | None = None
                recipe_id: str | None = None
                requirements: list[dict[str, Any]] = [
                    {"kind": item["kind"], "name": item["name"]} for item in exact["requirements"]
                ]
                if exact["kind"] == "skill" and distribution["kind"] in {
                    "github_skill",
                    "clawhub_skill",
                    "skills_sh_snapshot",
                }:
                    loaded = self._client().fetch_skill(resolved)
                    # Source-specific adapters verify every permitted file. The
                    # existing owner retains complete original bytes and source.
                    package = SkillAcquisition(
                        self.database, setup_token_resolver=self.setup_token
                    ).acquire(
                        context,
                        {"discoveryCheckId": str(check_id), "candidateSha256": resolved.sha256},
                        "discovery-package-" + check_id.hex,
                        lambda: loaded,
                        connection=conn,
                    )
                    package_id = package["package"]["id"]
                    package_release = loaded.release_sha256
                    package_source = content_hash(loaded.source)
                    requirements = [
                        {"kind": item["kind"], "name": item["name"]}
                        for item in loaded.manifest["requirements"]
                    ]
                elif exact["kind"] == "tool" and distribution["kind"] == "tool_recipe":
                    from potbot.tools.tool_acquisition import _recipe_row, _verified_recipe

                    recipe_id = distribution["recipeId"]
                    recipe_row = _recipe_row(conn, context, UUID(recipe_id))
                    recipe = _verified_recipe(recipe_row, token)
                    if (
                        recipe_row["agent_id"] != agent_id
                        or recipe.repository.removesuffix(".git") != distribution["repository"]
                        or exact["version"] != distribution["commit"]
                    ):
                        raise ScopeDeniedError(
                            "Candidate source or agent differs from the approved executable recipe"
                        )
                else:
                    raise ScopeDeniedError("This distribution remains inspection-only")
                requirements = parse_requirements(requirements)
                payload = {
                    "candidateId": str(candidate_id),
                    "requestedCandidateSha256": candidate_sha256,
                    "candidate": exact,
                    "candidateSha256": resolved.sha256,
                    "sourceBindingSha256": binding,
                    "requirements": requirements,
                    "requirementsSha256": requirements_digest(requirements),
                    "packageId": package_id,
                    "packageReleaseSha256": package_release,
                    "packageSourceSha256": package_source,
                    "recipeId": recipe_id,
                    "measurement": None,
                    "target": None,
                    "reasonCode": None,
                }
                row = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": check_id,
                    "source_id": source["id"],
                    "publisher_id": publisher["id"],
                    "snapshot_id": snapshot_id,
                    "agent_id": agent_id,
                    "revision": 1,
                    "state": "awaiting_measurement",
                    "payload": payload,
                    "worker_token": None,
                }
                saved = self._save(conn, "check", row, token, "review_prepared", insert=True)
                return {
                    **self._scope(context),
                    "check": self._check_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "discovery.check.create", _key(context, key), request, create
            )

    def _check_authority(
        self,
        conn: Any,
        context: WorkspaceContext,
        row: dict[str, Any],
        token: str | None,
        *,
        lock: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._valid(conn, "check", row, token):
            raise ScopeDeniedError("The discovery check has no current independent authorization")
        source, _publisher, snapshot, binding = self._admission(
            conn,
            context,
            row["source_id"],
            row["publisher_id"],
            row["snapshot_id"],
            token,
            lock=lock,
        )
        if row["payload"]["sourceBindingSha256"] != binding:
            raise RevisionConflictError("Discovery admission changed after this exact review")
        if not any(
            RegistryCandidate.from_dict(value).sha256 == row["payload"]["requestedCandidateSha256"]
            for value in snapshot["payload"]["candidates"]
        ):
            raise ScopeDeniedError("The selected candidate is no longer in its source snapshot")
        return source, snapshot

    def _measurement_current(self, row: dict[str, Any]) -> bool:
        from potbot.context.skill_eligibility import parse_eligibility_report

        report = row["payload"].get("measurement")
        if report is None:
            return False
        try:
            report = parse_eligibility_report(report)
            return (
                datetime.fromisoformat(report["checkedAt"])
                <= self.clock()
                < datetime.fromisoformat(report["expiresAt"])
            )
        except (ValueError, KeyError, DomainError):
            return False

    def _check_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        payload = row["payload"]
        if not self._valid(conn, "check", row, token):
            # Retained public evidence remains readable after key rotation;
            # immutable event binding still prevents unreviewed row replacement.
            event = conn.execute(
                "SELECT revision,binding_sha256 FROM discovery_events WHERE workspace_id=%s AND subject_kind='check' AND subject_id=%s ORDER BY revision DESC LIMIT 1",
                (context.workspace_id, row["id"]),
            ).fetchone()
            if (
                not event
                or event["revision"] != row["revision"]
                or event["binding_sha256"] != content_hash(_body("check", row))
            ):
                raise ScopeDeniedError("Discovery check integrity is inconsistent")
        candidate = RegistryCandidate.from_dict(payload["candidate"])
        source = self._row(conn, context, "source", row["source_id"])
        # Immutable snapshot fields are safe metadata after rotation; current
        # authority always uses _snapshot and its independent signature above.
        snapshot = conn.execute(
            "SELECT * FROM discovery_snapshots WHERE workspace_id=%s AND principal_id=%s AND id=%s",
            (context.workspace_id, context.principal_id, row["snapshot_id"]),
        ).fetchone()
        if snapshot is None:
            raise ScopeDeniedError(_DENIED)
        view = self._candidate_view(
            conn,
            context,
            source,
            dict(snapshot),
            candidate,
            token,
            identity=UUID(payload["candidateId"]),
        )
        reasons = []
        valid = True
        try:
            self._check_authority(conn, context, row, token)
        except DomainError:
            valid = False
            reasons.append("discovery_binding_stale")
        current = self._measurement_current(row)
        if payload["measurement"] is not None and not current:
            reasons.append("measurement_expired")
        if payload["reasonCode"]:
            reasons.append(payload["reasonCode"])
        actions = []
        if row["state"] in {"awaiting_measurement", "measuring", "measured", "failed"}:
            actions.append("cancel")
        if row["state"] == "measured" and valid and current:
            actions.append("handoff")
        return {
            "id": str(row["id"]),
            "candidateId": payload["candidateId"],
            "sourceId": str(row["source_id"]),
            "snapshotId": str(row["snapshot_id"]),
            "requestedCandidateSha256": payload["requestedCandidateSha256"],
            "candidateSha256": payload["candidateSha256"],
            "revision": row["revision"],
            "bindingSha256": content_hash(_body("check", row)),
            "agentId": str(row["agent_id"]),
            "status": row["state"],
            "candidate": view,
            "requirements": payload["requirements"],
            "packageId": payload["packageId"],
            "recipeId": payload["recipeId"],
            "measurement": payload["measurement"],
            "measurementCurrent": current and valid,
            "target": payload["target"],
            "reasonCodes": reasons,
            "actions": actions,
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    def check(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "check": self._check_view(
                    conn, context, self._row(conn, context, "check", identity), self._token()
                ),
            }

    def checks(
        self, context: WorkspaceContext, *, limit: int = 25, after: str | None = None
    ) -> dict[str, Any]:
        cursor, token = self._page(limit, after), self._token()
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM discovery_checks WHERE workspace_id=%s AND principal_id=%s AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                (context.workspace_id, context.principal_id, cursor, cursor, limit + 1),
            ).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                item = self._check_view(conn, context, dict(row), token)
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

    def measurement_replay(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any] | None:
        """Recover a prior exact host command without configuring its runtime."""
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:
            prior = conn.execute(
                "SELECT request_hash FROM command_results WHERE workspace_id=%s "
                "AND principal_id=%s AND operation='discovery.measure.claim' AND idempotency_key=%s",
                (context.workspace_id, context.principal_id, _key(context, key)),
            ).fetchone()
            if prior is None:
                return None
            if prior["request_hash"] != content_hash(request):
                raise RevisionConflictError("This measurement key belongs to another exact request")
            row = self._row(conn, context, "check", identity)
            return {**self._scope(context), "check": self._check_view(conn, context, row, token)}

    def claim_measurement(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
        fresh_only: bool = False,
    ) -> dict[str, Any]:
        """Authorize one explicit native maintenance attempt, never a queue poll."""
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:
            created = False

            def claim() -> dict[str, Any]:
                nonlocal created
                row = self._row(conn, context, "check", identity, lock=True)
                self._expected("check", row, revision, digest)
                self._check_authority(conn, context, row, token, lock=True)
                if row["state"] != "awaiting_measurement":
                    raise RevisionConflictError(
                        "This review already has a native measurement attempt"
                    )
                row.update(state="measuring", worker_token=uuid4(), revision=row["revision"] + 1)
                row = self._save(conn, "check", row, token, "measurement_claimed")
                created = True
                return {
                    "id": str(row["id"]),
                    "workerToken": str(row["worker_token"]),
                    "agentId": str(row["agent_id"]),
                    "candidateSha256": row["payload"]["candidateSha256"],
                    "sourceBindingSha256": row["payload"]["sourceBindingSha256"],
                    "requirements": row["payload"]["requirements"],
                }

            result = idempotent_command(
                conn, context, "discovery.measure.claim", _key(context, key), request, claim
            )
            if fresh_only and not created:
                row = self._row(conn, context, "check", identity)
                return {
                    "replay": {
                        **self._scope(context),
                        "check": self._check_view(conn, context, row, token),
                    }
                }
            return result

    def validate_measurement(
        self,
        context: WorkspaceContext,
        identity: UUID,
        worker_token: UUID,
        *,
        connection: Any = None,
    ) -> dict[str, Any]:
        from contextlib import nullcontext

        _uuid(worker_token)
        with (
            self.database.transaction(context)
            if connection is None
            else nullcontext(connection) as conn
        ):
            row = self._row(conn, context, "check", identity, lock=True)
            if row["state"] != "measuring" or row["worker_token"] != worker_token:
                raise ScopeDeniedError(
                    "The native dependency measurement claim is no longer current"
                )
            self._check_authority(conn, context, row, self._token(), lock=True)
            return row

    def complete_measurement(
        self, context: WorkspaceContext, identity: UUID, worker_token: UUID, report: Any
    ) -> dict[str, Any]:
        from potbot.context.skill_eligibility import parse_eligibility_report

        value = parse_eligibility_report(report)
        with self.database.transaction(context) as conn:
            row = self.validate_measurement(context, identity, worker_token, connection=conn)
            expected = {
                "checkId": str(identity),
                "agentId": str(row["agent_id"]),
                "candidateSha256": row["payload"]["candidateSha256"],
                "sourceBindingSha256": row["payload"]["sourceBindingSha256"],
                "requirementsSha256": row["payload"]["requirementsSha256"],
            }
            if (
                any(value[k] != item for k, item in expected.items())
                or value["bindings"]["workspaceId"] != str(context.workspace_id)
                or value["bindings"]["principalId"] != str(context.principal_id)
            ):
                raise ScopeDeniedError(
                    "Dependency evidence differs from the exact authorized review"
                )
            now = self.clock()
            if (
                not datetime.fromisoformat(value["checkedAt"])
                <= now
                < datetime.fromisoformat(value["expiresAt"])
            ):
                raise RevisionConflictError("Dependency evidence is already stale")
            token = self._token()
            if token is None:
                raise ScopeDeniedError("The native setup capability is unavailable")
            self._check_authority(conn, context, row, token, lock=True)
            row["payload"] = {**row["payload"], "measurement": value, "reasonCode": None}
            row.update(state="measured", worker_token=None, revision=row["revision"] + 1)
            saved = self._save(conn, "check", row, token, "measurement_recorded")
            return {**self._scope(context), "check": self._check_view(conn, context, saved, token)}

    def fail_measurement(
        self, context: WorkspaceContext, identity: UUID, worker_token: UUID, reason: str
    ) -> dict[str, Any]:
        if reason not in {
            "measurement_failed",
            "measurement_indeterminate",
            "measurement_cancelled",
        }:
            raise InvalidInputError("Unknown dependency measurement failure classification")
        with self.database.transaction(context) as conn:
            row = self._row(conn, context, "check", identity, lock=True)
            token = self._token()
            if (
                token is None
                or row["state"] != "measuring"
                or row["worker_token"] != _uuid(worker_token)
                or not self._valid(conn, "check", row, token)
            ):
                raise ScopeDeniedError("The measurement claim has already been fenced")
            row["payload"] = {**row["payload"], "reasonCode": reason}
            row.update(state="failed", worker_token=None, revision=row["revision"] + 1)
            saved = self._save(conn, "check", row, token, reason)
            return {**self._scope(context), "check": self._check_view(conn, context, saved, token)}

    def action(
        self,
        context: WorkspaceContext,
        identity: UUID,
        action: str,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        if action not in {"handoff", "cancel"}:
            raise InvalidInputError("Unsupported discovery check action")
        request = {
            "id": str(_uuid(identity)),
            "action": action,
            "revision": revision,
            "digest": _sha(digest),
        }
        with self.database.transaction(context) as conn:

            def change() -> dict[str, Any]:
                row = self._row(conn, context, "check", identity, lock=True)
                self._expected("check", row, revision, digest)
                if action == "cancel":
                    if row["state"] in {"handed_off", "cancelled"}:
                        raise RevisionConflictError("The discovery review is already terminal")
                    # Cancellation withdraws pending authority, including after
                    # key rotation, and never asserts that a process stopped.
                    event = conn.execute(
                        "SELECT revision,binding_sha256 FROM discovery_events WHERE workspace_id=%s AND subject_kind='check' AND subject_id=%s ORDER BY revision DESC LIMIT 1",
                        (context.workspace_id, identity),
                    ).fetchone()
                    if (
                        not event
                        or event["revision"] != revision
                        or event["binding_sha256"] != digest
                    ):
                        raise ScopeDeniedError("The discovery review history is inconsistent")
                    row.update(state="cancelled", worker_token=None, revision=row["revision"] + 1)
                    row["payload"] = {**row["payload"], "reasonCode": "measurement_cancelled"}
                else:
                    self._check_authority(conn, context, row, token, lock=True)
                    if row["state"] != "measured" or not self._measurement_current(row):
                        raise RevisionConflictError(
                            "A current completed prerequisite measurement is required"
                        )
                    payload = row["payload"]
                    if payload["packageId"] is not None:
                        from potbot.context.skill_acquisition import _package_row, _package_value

                        package_row = _package_row(
                            conn, context, UUID(payload["packageId"]), owner=True
                        )
                        package = _package_value(conn, context, package_row)
                        if (
                            package.release_sha256 != payload["packageReleaseSha256"]
                            or content_hash(package.source) != payload["packageSourceSha256"]
                        ):
                            raise RevisionConflictError(
                                "The retained package differs from the measured review"
                            )
                        target = {"kind": "skill_package", "id": payload["packageId"]}
                    elif payload["recipeId"] is not None:
                        from potbot.tools.tool_acquisition import (
                            ToolAcquisitionService,
                            _recipe_row,
                            _verified_recipe,
                        )

                        distribution = payload["candidate"]["distribution"]
                        recipe_row = _recipe_row(conn, context, UUID(payload["recipeId"]))
                        recipe = _verified_recipe(recipe_row, token)
                        if (
                            recipe_row["agent_id"] != row["agent_id"]
                            or recipe.repository.removesuffix(".git") != distribution["repository"]
                        ):
                            raise ScopeDeniedError(
                                "The executable recipe no longer matches this review"
                            )
                        operation = ToolAcquisitionService(
                            self.database, setup_token_resolver=self.setup_token
                        ).request(
                            context,
                            UUID(payload["recipeId"]),
                            distribution["commit"],
                            "discovery-handoff-" + identity.hex,
                            setup_token=token,
                            connection=conn,
                        )
                        target = {"kind": "tool_acquisition", "id": operation["operation"]["id"]}
                    else:
                        raise ScopeDeniedError("This candidate has no acquisition handoff owner")
                    row["payload"] = {**payload, "target": target}
                    row.update(state="handed_off", worker_token=None, revision=row["revision"] + 1)
                saved = self._save(conn, "check", row, token, action)
                return {
                    **self._scope(context),
                    "check": self._check_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "discovery.check.action", _key(context, key), request, change
            )
