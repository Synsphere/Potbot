"""Signed, scoped task qualification over the existing MCP execution gateway.

This owner retains review and evidence. It does not create tasks, task grants,
provider credentials, installations, processes or a second execution scheduler.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from potbot.context.service import native_skill_setup_token
from potbot.controller.mcp_discovery import McpDiscoveryService, installed_identity
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal, CapabilityContext
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database, idempotent_command
from potbot.tools.mcp_discovery import bounded_metadata
from potbot.tools.mcp_discovery_host import McpInstalledProfile
from potbot.tools.mcp_protocol import validate_arguments
from potbot.tools.mcp_qualification_host import (
    McpQualificationSuite,
    qualification_consumer_sha256,
    read_mcp_qualification_suite,
    read_mcp_task_binding,
)

SCHEMA = "potbot-mcp-qualification/v1"
MAX_SUITES = 32
_TABLES = {
    kind: "mcp_qualification_" + name
    for kind, name in (("suite", "suites"), ("attempt", "attempts"), ("report", "reports"))
}
_SHA = re.compile(r"[a-f0-9]{64}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_CASE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_ACTIVE = {"requested", "running", "recovering"}
_FAILURES = {
    "qualification_failed",
    "qualification_indeterminate",
    "qualification_cancelled",
    "authority_changed",
    "assertion_mismatch",
    "invalid_result_evidence",
    "tool_failed",
    "report_incomplete",
}
_CHECK_REASONS = {
    "pointer_missing",
    "value_mismatch",
    "value_type_mismatch",
    "text_not_found",
    "length_mismatch",
}
_PREPARED = {
    "schema",
    "caseId",
    "caseSha256",
    "phase",
    "candidateId",
    "leaseOwner",
    "leaseGeneration",
    "actionId",
    "reservationId",
    "inputArtifactId",
    "inputSha256",
    "storageSha256",
    "nativePins",
}
_NATIVE_PINS = {
    "runtimeSha256",
    "imageSha256",
    "qualificationSha256",
    "agentHomeRevision",
    "consumerSha256",
    "configurationSha256",
    "referenceSha256",
}
_FINISHED = _PREPARED | {
    "runId",
    "resultArtifactId",
    "resultSha256",
    "receiptSha256",
    "invocationSha256",
    "resultCheck",
    "processesStopped",
    "isolationSha256",
    "elapsedMs",
    "reasonCode",
}


def _uuid(value: Any) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError("An exact qualification UUID is required")
    return value


def _wire_uuid(value: Any) -> UUID:
    try:
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("An exact canonical qualification UUID is required") from None


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("An exact lowercase SHA-256 is required")
    return value


def _key(context: WorkspaceContext, value: Any) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise InvalidInputError("A bounded stable qualification command key is required")
    return content_hash([str(context.principal_id), value])


def _token_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 4096
        and all(33 <= ord(c) <= 126 for c in value)
    )


def _body(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "workspaceId": str(row["workspace_id"]),
        "principalId": str(row["principal_id"]),
        "id": str(row["id"]),
    }
    if kind == "report":
        return {
            **result,
            "suiteId": str(row["suite_id"]),
            "attemptId": str(row["attempt_id"]),
            "sha256": row["sha256"],
        }
    result.update(revision=row["revision"], state=row["state"])
    if kind == "suite":
        result.update(
            bindingId=str(row["binding_id"]),
            config=row["config"],
            activeAttemptId=str(row["active_attempt_id"]) if row["active_attempt_id"] else None,
            lastAttemptId=str(row["last_attempt_id"]) if row["last_attempt_id"] else None,
            lastReportId=str(row["last_report_id"]) if row["last_report_id"] else None,
        )
    else:
        result.update(
            suiteId=str(row["suite_id"]),
            payload=row["payload"],
            workerToken=str(row["worker_token"]) if row["worker_token"] else None,
        )
    return result


def _mac(token: str, kind: str, value: Any) -> str:
    return hmac.new(
        token.encode(), canonical_json([SCHEMA, kind, value]).encode(), hashlib.sha256
    ).hexdigest()


def _page(limit: int, after: str | None) -> UUID | None:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise InvalidInputError("Qualification page limit must be between 1 and 50")
    return _wire_uuid(after) if after is not None else None


class McpQualificationService:
    def __init__(
        self,
        database: Database,
        *,
        setup_token: Callable[[], str | None] = native_skill_setup_token,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database, self.setup_token = database, setup_token
        self.clock = clock or (lambda: datetime.now(UTC))
        self.discovery = McpDiscoveryService(database, setup_token=setup_token, clock=self.clock)

    @staticmethod
    def _scope(context: WorkspaceContext) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        }

    def _token(self) -> str | None:
        try:
            value = self.setup_token()
            return value if _token_valid(value) else None
        except Exception:
            return None

    def _authorized(self, presented: str) -> str:
        token = self._token()
        if not token or not _token_valid(presented) or not hmac.compare_digest(token, presented):
            raise ScopeDeniedError(
                "Qualification controls require the current independent setup capability"
            )
        return token

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
            raise ScopeDeniedError("The qualification record is unavailable in this scope")
        return dict(row)

    @staticmethod
    def _valid(conn: Any, kind: str, row: dict[str, Any], token: str | None) -> bool:
        if not token or not hmac.compare_digest(row["mac"], _mac(token, kind, _body(kind, row))):
            return False
        if kind == "report":
            return bool(content_hash(row["payload"]) == row["sha256"])
        event = conn.execute(
            "SELECT revision,binding_sha256 FROM mcp_qualification_events WHERE workspace_id=%s AND principal_id=%s AND subject_kind=%s AND subject_id=%s ORDER BY revision DESC LIMIT 1",
            (row["workspace_id"], row["principal_id"], kind, row["id"]),
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

        fields = (
            (
                "binding_id",
                "config",
                "revision",
                "state",
                "active_attempt_id",
                "last_attempt_id",
                "last_report_id",
            )
            if kind == "suite"
            else ("suite_id", "revision", "state", "payload", "worker_token")
        )
        row["mac"] = _mac(token, kind, _body(kind, row))
        values = [
            Jsonb(row[name]) if name in {"config", "payload"} else row[name] for name in fields
        ]
        if insert:
            names = ("workspace_id", "id", "principal_id", *fields, "mac")
            saved = conn.execute(
                f"INSERT INTO {_TABLES[kind]} ({','.join(names)}) VALUES ({','.join(['%s'] * len(names))}) RETURNING *",
                (row["workspace_id"], row["id"], row["principal_id"], *values, row["mac"]),
            ).fetchone()
        else:
            saved = conn.execute(
                f"UPDATE {_TABLES[kind]} SET "
                + ",".join(f"{name}=%s" for name in fields)
                + ",mac=%s,updated_at=clock_timestamp() WHERE workspace_id=%s AND principal_id=%s AND id=%s RETURNING *",
                (*values, row["mac"], row["workspace_id"], row["principal_id"], row["id"]),
            ).fetchone()
        if saved is None:
            raise ScopeDeniedError("Qualification mutation was fenced")
        conn.execute(
            "INSERT INTO mcp_qualification_events(workspace_id,id,principal_id,subject_kind,subject_id,revision,action,binding_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["workspace_id"],
                uuid4(),
                row["principal_id"],
                kind,
                row["id"],
                row["revision"],
                action,
                content_hash(_body(kind, row)),
            ),
        )
        return dict(saved)

    @staticmethod
    def _expected(kind: str, row: dict[str, Any], revision: int, digest: str) -> None:
        if (
            type(revision) is not int
            or not 1 <= revision < 2**53 - 1
            or row["revision"] != revision
            or content_hash(_body(kind, row)) != _sha(digest)
        ):
            raise RevisionConflictError("Review the current qualification revision and digest")

    def _authority_reasons(
        self, conn: Any, context: WorkspaceContext, suite: dict[str, Any], token: str | None
    ) -> list[str]:
        reasons = []
        config = suite["config"]
        if suite["state"] != "enabled":
            reasons.append("suite_" + suite["state"])
        if not self._valid(conn, "suite", suite, token):
            reasons.append("admission_invalid")
        try:
            self.discovery.validate_reference(context, config["reference"], connection=conn)
        except DomainError:
            reasons.append("discovery_not_current")
        try:
            selected = config["taskBinding"]
            current = read_mcp_task_binding(
                self.database,
                context,
                episode_id=UUID(selected["episodeId"]),
                repository_state_id=UUID(selected["repositoryStateId"]),
                contract_revision_id=UUID(selected["contractRevisionId"]),
            )
            if current != selected:
                reasons.append("task_binding_changed")
        except DomainError:
            reasons.append("task_binding_changed")
        try:
            if qualification_consumer_sha256() != config["consumerSha256"]:
                reasons.append("consumer_changed")
        except (DomainError, OSError):
            reasons.append("consumer_changed")
        return reasons

    def _suite_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        config = row["config"]
        valid = self._valid(conn, "suite", row, token)
        reasons = self._authority_reasons(conn, context, row, token)
        actions = []
        if row["state"] != "revoked":
            if valid:
                actions.append("disable" if row["state"] == "enabled" else "enable")
                if not reasons and row["active_attempt_id"] is None:
                    actions.append("qualify")
            actions.append("revoke")
        public = (
            "name",
            "description",
            "agentId",
            "toolName",
            "transport",
            "suiteSha256",
            "suiteFileSha256",
            "configurationSha256",
            "installationSha256",
            "consumerSha256",
            "taskBinding",
            "cases",
        )
        return {
            "id": str(row["id"]),
            "bindingId": str(row["binding_id"]),
            **{name: config[name] for name in public},
            "revision": row["revision"],
            "state": row["state"],
            "bindingSha256": content_hash(_body("suite", row)),
            "approvalValid": valid,
            "activeAttemptId": str(row["active_attempt_id"]) if row["active_attempt_id"] else None,
            "lastAttemptId": str(row["last_attempt_id"]) if row["last_attempt_id"] else None,
            "lastReportId": str(row["last_report_id"]) if row["last_report_id"] else None,
            "authorityCurrent": not reasons,
            "reasonCodes": reasons,
            "actions": actions,
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _cases(suite: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
        values = []
        for spec in suite["config"]["cases"]:
            saved = row["payload"]["cases"].get(spec["id"])
            check = saved.get("resultCheck") if saved else None
            if saved and saved["phase"] == "finished":
                state = "passed" if check and check["passed"] else "failed"
                reason = saved["reasonCode"]
            elif saved:
                state = (
                    "not_run"
                    if spec["id"] in row["payload"].get("notRun", [])
                    else "prepared"
                    if row["state"] in _ACTIVE
                    else "indeterminate"
                )
                reason = (
                    None
                    if state == "prepared"
                    else "case_not_run"
                    if state == "not_run"
                    else "case_outcome_unknown"
                )
            else:
                state = "pending" if row["state"] in _ACTIVE else "not_run"
                reason = None if state == "pending" else "case_not_run"
            result = {
                "id": spec["id"],
                "title": spec["title"],
                "caseSha256": spec["caseSha256"],
                "status": state,
                "oracleSha256": spec["oracleSha256"],
                "checks": check["checks"] if check else None,
                "reasonCode": reason,
            }
            for field in (
                "actionId",
                "candidateId",
                "reservationId",
                "runId",
                "resultArtifactId",
                "resultSha256",
                "receiptSha256",
                "elapsedMs",
            ):
                result[field] = saved.get(field) if saved else None
            values.append(result)
        return values

    def _attempt_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        suite = self._row(conn, context, "suite", row["suite_id"])
        trusted = (
            self._valid(conn, "attempt", row, token)
            and self._valid(conn, "suite", suite, token)
            and not row["payload"].get("retiredWithoutAdmission", False)
            and not suite["config"].get("retiredWithoutAdmission", False)
        )
        if trusted:
            cases = self._cases(suite, row)
        else:
            # Preserve the exact review identity for terminal cancellation, but
            # never project an unsigned or retired journal as verified results.
            # Do not parse untrusted execution fields or erase the private data.
            cases = [
                {
                    "id": spec["id"],
                    "title": spec["title"],
                    "caseSha256": spec["caseSha256"],
                    "oracleSha256": spec["oracleSha256"],
                    "status": "indeterminate",
                    "reasonCode": "admission_invalid",
                    **{
                        field: None
                        for field in (
                            "actionId",
                            "candidateId",
                            "reservationId",
                            "runId",
                            "resultArtifactId",
                            "resultSha256",
                            "receiptSha256",
                            "checks",
                            "elapsedMs",
                        )
                    },
                }
                for spec in suite["config"]["cases"]
            ]
        return {
            "id": str(row["id"]),
            "suiteId": str(row["suite_id"]),
            "bindingId": str(suite["binding_id"]),
            "agentId": suite["config"]["agentId"],
            "revision": row["revision"],
            "bindingSha256": content_hash(_body("attempt", row)),
            "status": row["state"] if trusted or row["state"] in _ACTIVE else "indeterminate",
            "requestedSuiteSha256": row["payload"]["requestedSuiteSha256"],
            "suiteSha256": suite["config"]["suiteSha256"],
            "configurationSha256": suite["config"]["configurationSha256"],
            "caseCount": len(cases),
            "completedCases": sum(c["status"] in {"passed", "failed"} for c in cases),
            "passedCases": sum(c["status"] == "passed" for c in cases),
            "failedCases": sum(c["status"] == "failed" for c in cases),
            "cases": cases,
            "reportId": row["payload"]["reportId"] if trusted else None,
            "reportSha256": row["payload"]["reportSha256"] if trusted else None,
            "reasonCode": row["payload"]["reasonCode"] if trusted else "admission_invalid",
            "actions": ["cancel"] if row["state"] in _ACTIVE else [],
            "recoveryAvailable": bool(
                trusted
                and row["state"]
                in {"running", "recovering", "indeterminate", "failed", "cancelled"}
                and row["payload"]["cases"]
                and row["payload"]["reportId"] is None
            ),
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    def _report_view(
        self, conn: Any, context: WorkspaceContext, row: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        if content_hash(row["payload"]) != row["sha256"]:
            raise StateMismatchError("Retained qualification report integrity failed")
        suite = self._row(conn, context, "suite", row["suite_id"])
        valid = self._valid(conn, "report", row, token)
        reasons = self._authority_reasons(conn, context, suite, token)
        if not valid:
            reasons.append("report_admission_invalid")
        if suite["last_report_id"] != row["id"] or suite["last_attempt_id"] != row["attempt_id"]:
            reasons.append("report_superseded")
        if suite["active_attempt_id"] is not None:
            reasons.append("qualification_in_progress")
        return {
            "id": str(row["id"]),
            "suiteId": str(row["suite_id"]),
            "attemptId": str(row["attempt_id"]),
            "bindingId": str(suite["binding_id"]),
            "sha256": row["sha256"],
            "approvalValid": valid,
            **row["payload"],
            "authorityCurrent": not reasons,
            "reasonCodes": reasons,
            "recordedAt": row["recorded_at"].isoformat(),
        }

    def suite(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "suite": self._suite_view(
                    conn, context, self._row(conn, context, "suite", identity), self._token()
                ),
            }

    def attempt(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "attempt": self._attempt_view(
                    conn, context, self._row(conn, context, "attempt", identity), self._token()
                ),
            }

    def report(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return {
                **self._scope(context),
                "report": self._report_view(
                    conn, context, self._row(conn, context, "report", identity), self._token()
                ),
            }

    def _list(
        self,
        context: WorkspaceContext,
        kind: str,
        *,
        parent: UUID | None,
        limit: int,
        after: str | None,
    ) -> dict[str, Any]:
        cursor = _page(limit, after)
        if parent is not None:
            _uuid(parent)
        with self.database.transaction(context) as conn:
            field = "binding_id" if kind == "suite" else "suite_id"
            rows = conn.execute(
                f"SELECT * FROM {_TABLES[kind]} WHERE workspace_id=%s AND principal_id=%s"
                + (f" AND {field}=%s" if parent else "")
                + (" AND id>%s" if cursor else "")
                + " ORDER BY id LIMIT %s",
                (
                    context.workspace_id,
                    context.principal_id,
                    *((parent,) if parent else ()),
                    *((cursor,) if cursor else ()),
                    limit + 1,
                ),
            ).fetchall()
            view = self._suite_view if kind == "suite" else self._attempt_view
            items = [view(conn, context, dict(row), self._token()) for row in rows[:limit]]
            return {
                **self._scope(context),
                "items": items,
                "nextCursor": items[-1]["id"] if len(rows) > limit else None,
            }

    def suites(
        self,
        context: WorkspaceContext,
        *,
        binding_id: UUID | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        return self._list(context, "suite", parent=binding_id, limit=limit, after=after)

    def attempts(
        self,
        context: WorkspaceContext,
        *,
        suite_id: UUID | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        return self._list(context, "attempt", parent=suite_id, limit=limit, after=after)

    def admit(
        self,
        context: WorkspaceContext,
        profile: McpInstalledProfile,
        suite: McpQualificationSuite,
        episode_id: UUID,
        repository_state_id: UUID,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        if not isinstance(profile, McpInstalledProfile) or not isinstance(
            suite, McpQualificationSuite
        ):
            raise InvalidInputError(
                "Qualification requires exact private installed and suite profiles"
            )
        installed = profile.installed
        binding_id = installed.discovery_binding_id
        if (
            binding_id is None
            or installed.discovery_configuration_sha256 != profile.configuration_sha256
        ):
            raise ScopeDeniedError(
                "Qualification requires an explicitly linked installed MCP profile"
            )
        identity = installed_identity(installed, context)
        request = {
            "bindingId": str(binding_id),
            "installedIdentity": identity,
            "agentId": str(profile.agent_id),
            "configurationSha256": _sha(profile.configuration_sha256),
            "suiteSha256": _sha(suite.sha256),
            "suiteFileSha256": _sha(suite.file_sha256),
            "suiteSummary": suite.summary(),
            "episodeId": str(_uuid(episode_id)),
            "repositoryStateId": str(_uuid(repository_state_id)),
        }
        bounded_metadata(request, maximum=64 * 1024)
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                # Read the committed command before current task/catalogue
                # preflight, so an identical lost-response retry stays readable.
                readback = read_mcp_qualification_suite(suite.path)
                if (
                    readback.to_dict() != suite.to_dict()
                    or readback.file_sha256 != suite.file_sha256
                ):
                    raise ScopeDeniedError("Qualification suite differs from its private file")
                task = read_mcp_task_binding(
                    self.database,
                    context,
                    episode_id=_uuid(episode_id),
                    repository_state_id=_uuid(repository_state_id),
                )
                for case in suite.cases:
                    if case.tool_name != installed.tool.name:
                        raise ScopeDeniedError("Qualification cannot admit another discovered tool")
                    validate_arguments(installed.tool, case.arguments)
                reference = None
                after = None
                while True:
                    page = self.discovery.entries(
                        context,
                        binding_id,
                        kind="tool",
                        query=installed.tool.name,
                        limit=50,
                        after=after,
                    )
                    matched = [
                        entry
                        for entry in page["items"]
                        if entry["configuredTool"] and entry["identity"] == installed.tool.name
                    ]
                    if matched:
                        reference = self.discovery.reference(
                            context, binding_id, UUID(matched[0]["id"])
                        )["reference"]
                        break
                    after = page["nextCursor"]
                    if after is None:
                        raise ScopeDeniedError(
                            "The configured MCP tool has no current retained reference"
                        )
                summary = suite.summary()
                config = {
                    **identity,
                    "definitionName": installed.definition.name,
                    "agentId": str(profile.agent_id),
                    "name": summary["name"],
                    "description": summary["description"],
                    "cases": summary["cases"],
                    "suiteSha256": _sha(suite.sha256),
                    "suiteFileSha256": _sha(suite.file_sha256),
                    "configurationSha256": _sha(profile.configuration_sha256),
                    "consumerSha256": qualification_consumer_sha256(),
                    "taskBinding": task,
                    "reference": reference,
                }
                bounded_metadata(config, maximum=64 * 1024)
                self.discovery.validate_reference(
                    context, reference, installed=installed, connection=conn
                )
                binding = self.discovery._row(conn, context, "binding", binding_id)
                if binding["config"]["agentId"] != str(profile.agent_id):
                    raise ScopeDeniedError(
                        "Qualification Agent differs from installed discovery admission"
                    )
                contract = conn.execute(
                    "SELECT content FROM contract_revisions WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, UUID(task["contractRevisionId"])),
                ).fetchone()
                if contract is None:
                    raise ScopeDeniedError("Qualification task contract is unavailable")
                CapabilityContext((installed.definition.name,)).authorize(
                    installed.definition, contract["content"]
                )
                conn.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                    (f"potbot:mcp-qualification:{context.workspace_id}:{context.principal_id}",),
                )
                count = conn.execute(
                    "SELECT count(*) AS n FROM mcp_qualification_suites WHERE workspace_id=%s AND principal_id=%s AND state<>'revoked'",
                    (context.workspace_id, context.principal_id),
                ).fetchone()["n"]
                if count >= MAX_SUITES:
                    raise InvalidInputError(
                        "Revoke a retired qualification suite before admitting another"
                    )
                row = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": uuid4(),
                    "binding_id": binding_id,
                    "config": config,
                    "revision": 1,
                    "state": "enabled",
                    "active_attempt_id": None,
                    "last_attempt_id": None,
                    "last_report_id": None,
                }
                saved = self._save(conn, "suite", row, token, "admitted", insert=True)
                if self._authority_reasons(conn, context, saved, token):
                    raise ScopeDeniedError("Qualification authority changed during admission")
                return {
                    **self._scope(context),
                    "suite": self._suite_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "mcp.qualification.admit", _key(context, key), request, create
            )

    def _attempt_rows(
        self, conn: Any, context: WorkspaceContext, identity: UUID, *, lock: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        preliminary = self._row(conn, context, "attempt", identity)
        suite = self._row(conn, context, "suite", preliminary["suite_id"], lock=lock)
        return suite, self._row(conn, context, "attempt", identity, lock=lock)

    def _current(
        self,
        conn: Any,
        context: WorkspaceContext,
        suite: dict[str, Any],
        row: dict[str, Any],
        token: str | None,
        *,
        recovery: bool = False,
    ) -> None:
        if (
            not self._valid(conn, "suite", suite, token)
            or not self._valid(conn, "attempt", row, token)
            or suite["config"].get("retiredWithoutAdmission", False)
            or row["payload"].get("retiredWithoutAdmission", False)
            or suite["active_attempt_id"] != row["id"]
            or row["payload"]["admissionSha256"] != content_hash(_body("suite", suite))
        ):
            raise ScopeDeniedError("Qualification worker authority has been fenced")
        if not recovery and self._authority_reasons(conn, context, suite, token):
            raise ScopeDeniedError("Qualification no longer has current execution admission")

    def control(
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
        choices = {"enable": "enabled", "disable": "disabled", "revoke": "revoked"}
        if action not in choices:
            raise InvalidInputError("Unsupported qualification control")
        request = {
            "id": str(_uuid(identity)),
            "action": action,
            "revision": revision,
            "digest": _sha(digest),
        }
        with self.database.transaction(context) as conn:

            def change() -> dict[str, Any]:
                suite = self._row(conn, context, "suite", identity, lock=True)
                self._expected("suite", suite, revision, digest)
                if suite["state"] in {"revoked", choices[action]}:
                    raise RevisionConflictError("This qualification control is unavailable")
                valid = self._valid(conn, "suite", suite, token)
                if action != "revoke" and not valid:
                    raise ScopeDeniedError(
                        "Positive qualification controls require prior independent admission"
                    )
                if not valid:
                    suite["config"]["retiredWithoutAdmission"] = True
                if suite["active_attempt_id"]:
                    row = self._row(conn, context, "attempt", suite["active_attempt_id"], lock=True)
                    if row["state"] in _ACTIVE and self._valid(conn, "attempt", row, token):
                        row.update(
                            state="cancelled", worker_token=None, revision=row["revision"] + 1
                        )
                        row["payload"]["reasonCode"] = "authority_changed"
                        self._save(conn, "attempt", row, token, "authority_changed")
                suite.update(
                    state=choices[action], revision=suite["revision"] + 1, active_attempt_id=None
                )
                saved = self._save(conn, "suite", suite, token, action)
                return {
                    **self._scope(context),
                    "suite": self._suite_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "mcp.qualification.control", _key(context, key), request, change
            )

    def request(
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
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                suite = self._row(conn, context, "suite", identity, lock=True)
                self._expected("suite", suite, revision, digest)
                if suite["active_attempt_id"] is not None or self._authority_reasons(
                    conn, context, suite, token
                ):
                    raise ScopeDeniedError("Qualification requires one current admitted idle suite")
                attempt_id = uuid4()
                suite.update(
                    revision=suite["revision"] + 1,
                    active_attempt_id=attempt_id,
                    last_attempt_id=attempt_id,
                )
                row = {
                    "workspace_id": context.workspace_id,
                    "principal_id": context.principal_id,
                    "id": attempt_id,
                    "suite_id": identity,
                    "revision": 1,
                    "state": "requested",
                    "worker_token": None,
                    "payload": {
                        "requestedSuiteSha256": digest,
                        "admissionSha256": content_hash(_body("suite", suite)),
                        "cases": {},
                        "notRun": [],
                        "reportId": None,
                        "reportSha256": None,
                        "reasonCode": None,
                    },
                }
                saved = self._save(conn, "attempt", row, token, "requested", insert=True)
                self._save(conn, "suite", suite, token, "qualification_requested")
                return {
                    **self._scope(context),
                    "attempt": self._attempt_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "mcp.qualification.request", _key(context, key), request, create
            )

    def execution_replay(
        self,
        context: WorkspaceContext,
        identity: UUID,
        revision: int,
        digest: str,
        key: str,
        *,
        setup_token: str,
        recovery: bool = False,
    ) -> dict[str, Any] | None:
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        operation = "mcp.qualification.recover_claim" if recovery else "mcp.qualification.claim"
        with self.database.transaction(context) as conn:
            prior = conn.execute(
                "SELECT request_hash FROM command_results WHERE workspace_id=%s AND principal_id=%s AND operation=%s AND idempotency_key=%s",
                (context.workspace_id, context.principal_id, operation, _key(context, key)),
            ).fetchone()
            if prior is None:
                return None
            if prior["request_hash"] != content_hash(request):
                raise RevisionConflictError(
                    "This native command key belongs to another exact qualification request"
                )
            return {
                **self._scope(context),
                "attempt": self._attempt_view(
                    conn, context, self._row(conn, context, "attempt", identity), token
                ),
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
        recovery: bool = False,
        fresh_only: bool = False,
    ) -> dict[str, Any]:
        token = self._authorized(setup_token)
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        operation = "mcp.qualification.recover_claim" if recovery else "mcp.qualification.claim"
        with self.database.transaction(context) as conn:
            created = False

            def acquire() -> dict[str, Any]:
                nonlocal created
                suite, row = self._attempt_rows(conn, context, identity)
                self._expected("attempt", row, revision, digest)
                if recovery:
                    if (
                        not self._valid(conn, "suite", suite, token)
                        or not self._valid(conn, "attempt", row, token)
                        or not self._attempt_view(conn, context, row, token)["recoveryAvailable"]
                        or suite["active_attempt_id"] not in {None, identity}
                    ):
                        raise ScopeDeniedError(
                            "Qualification recovery requires its retained admitted attempt"
                        )
                    suite.update(
                        revision=suite["revision"] + 1,
                        active_attempt_id=identity,
                    )
                    self._save(conn, "suite", suite, token, "recovery_requested")
                    row["payload"]["admissionSha256"] = content_hash(_body("suite", suite))
                else:
                    self._current(conn, context, suite, row, token)
                    if row["state"] != "requested":
                        raise RevisionConflictError(
                            "This qualification already has a native execution claim"
                        )
                row.update(
                    state="recovering" if recovery else "running",
                    worker_token=uuid4(),
                    revision=row["revision"] + 1,
                )
                saved = self._save(
                    conn, "attempt", row, token, "recovery_claimed" if recovery else "claimed"
                )
                created = True
                return {
                    "id": str(row["id"]),
                    "suiteId": str(suite["id"]),
                    "workerToken": str(saved["worker_token"]),
                    "config": suite["config"],
                    "reference": suite["config"]["reference"],
                }

            result = idempotent_command(
                conn, context, operation, _key(context, key), request, acquire
            )
            if fresh_only and not created:
                return {
                    "replay": {
                        **self._scope(context),
                        "attempt": self._attempt_view(
                            conn, context, self._row(conn, context, "attempt", identity), token
                        ),
                    }
                }
            return result

    def _claim_rows(
        self,
        conn: Any,
        context: WorkspaceContext,
        identity: UUID,
        worker_token: UUID,
        *,
        lock: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        suite, row = self._attempt_rows(conn, context, identity, lock=lock)
        token = self._token()
        if row["state"] not in {"running", "recovering"} or row["worker_token"] != _uuid(
            worker_token
        ):
            raise ScopeDeniedError("The qualification native claim has been fenced")
        self._current(conn, context, suite, row, token, recovery=row["state"] == "recovering")
        assert token is not None
        return suite, row, token

    def validate_claim(
        self, context: WorkspaceContext, identity: UUID, worker_token: UUID
    ) -> dict[str, Any]:
        # This callback also runs while ToolGateway owns its episode lock on a
        # different connection. Never acquire row locks from this read path.
        with self.database.transaction(context) as conn:
            suite, row, _token = self._claim_rows(conn, context, identity, worker_token, lock=False)
            return {
                "config": suite["config"],
                "reference": suite["config"]["reference"],
                "state": row["state"],
            }

    def case(
        self, context: WorkspaceContext, identity: UUID, worker_token: UUID, case_id: str
    ) -> dict[str, Any] | None:
        with self.database.transaction(context) as conn:
            suite, row, _token = self._claim_rows(conn, context, identity, worker_token, lock=False)
            if case_id not in {case["id"] for case in suite["config"]["cases"]}:
                raise InvalidInputError("Unknown admitted qualification case")
            event = row["payload"]["cases"].get(case_id)
            return dict(event) if event is not None else None

    @staticmethod
    def _case_event(value: Any, spec: dict[str, Any]) -> dict[str, Any]:
        event = bounded_metadata(value, maximum=32 * 1024)
        if (
            not isinstance(event, dict)
            or event.get("schema") != "potbot-mcp-qualification-case/v1"
            or event.get("phase") not in {"prepared", "finished"}
            or set(event) != (_PREPARED if event["phase"] == "prepared" else _FINISHED)
            or event["caseId"] != spec["id"]
            or event["caseSha256"] != spec["caseSha256"]
        ):
            raise ScopeDeniedError(
                "Native qualification case differs from its exact admitted contract"
            )
        for name in event:
            if name.endswith("Sha256"):
                _sha(event[name])
        for name in ("candidateId", "actionId", "reservationId", "inputArtifactId"):
            _wire_uuid(event[name])
        pins = event["nativePins"]
        if (
            not isinstance(pins, dict)
            or set(pins) != _NATIVE_PINS
            or type(pins["agentHomeRevision"]) is not int
            or not 1 <= pins["agentHomeRevision"] < 2**53
        ):
            raise ScopeDeniedError("Qualification requires its original native execution pins")
        for name in _NATIVE_PINS - {"agentHomeRevision"}:
            _sha(pins[name])
        if (
            not isinstance(event["leaseOwner"], str)
            or not 1 <= len(event["leaseOwner"]) <= 200
            or any(ord(c) < 33 or ord(c) > 126 for c in event["leaseOwner"])
            or type(event["leaseGeneration"]) is not int
            or not 1 <= event["leaseGeneration"] < 2**53
        ):
            raise InvalidInputError(
                "Qualification case requires its exact existing candidate lease"
            )
        if event["phase"] == "prepared":
            return dict(event)
        for name in ("runId", "resultArtifactId"):
            _wire_uuid(event[name])
        if (
            event["processesStopped"] is not True
            or type(event["elapsedMs"]) is not int
            or not 0 <= event["elapsedMs"] <= 600_000
            or event["reasonCode"]
            not in {None, "assertion_mismatch", "invalid_result_evidence", "tool_failed"}
        ):
            raise ScopeDeniedError("Qualification case requires bounded stopped gateway evidence")
        check = event["resultCheck"]
        if check is None:
            if event["reasonCode"] not in {"invalid_result_evidence", "tool_failed"}:
                raise ScopeDeniedError("An unchecked qualification result cannot pass")
            return dict(event)
        if (
            not isinstance(check, dict)
            or set(check) != {"schema", "oracleSha256", "outputSha256", "passed", "checks"}
            or check["schema"] != "potbot-mcp-result-check/v1"
            or check["oracleSha256"] != spec["oracleSha256"]
            or type(check["passed"]) is not bool
            or not isinstance(check["checks"], list)
            or len(check["checks"]) != spec["assertionCount"]
        ):
            raise ScopeDeniedError(
                "Qualification checks differ from their independently admitted oracle"
            )
        _sha(check["outputSha256"])
        for index, item in enumerate(check["checks"]):
            if (
                not isinstance(item, dict)
                or set(item) != {"index", "passed", "reasonCode"}
                or type(item["index"]) is not int
                or item["index"] != index
                or type(item["passed"]) is not bool
                or item["reasonCode"] not in _CHECK_REASONS | {None}
                or item["passed"] != (item["reasonCode"] is None)
            ):
                raise InvalidInputError("Qualification assertion evidence is inconsistent")
        passed = all(item["passed"] for item in check["checks"])
        if check["passed"] != passed or event["reasonCode"] != (
            None if passed else "assertion_mismatch"
        ):
            raise ScopeDeniedError("Qualification success cannot override a failed assertion")
        return dict(event)

    @staticmethod
    def _existing(
        conn: Any, context: WorkspaceContext, table: str, identity: str
    ) -> dict[str, Any]:
        if table not in {
            "actions",
            "search_nodes",
            "knowledge_views",
            "artifacts",
            "candidate_workspaces",
            "budget_reservations",
            "action_runs",
        }:
            raise InvalidInputError("Unknown qualification evidence owner")
        row = conn.execute(
            f"SELECT * FROM {table} WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, _wire_uuid(identity)),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Qualification evidence is unavailable in this workspace")
        return dict(row)

    def _verify_case_owners(
        self, conn: Any, context: WorkspaceContext, config: dict[str, Any], event: dict[str, Any]
    ) -> None:
        task = config["taskBinding"]
        action = self._existing(conn, context, "actions", event["actionId"])
        node = self._existing(conn, context, "search_nodes", str(action["node_id"]))
        view = self._existing(conn, context, "knowledge_views", str(node["knowledge_view_id"]))
        candidate = self._existing(conn, context, "candidate_workspaces", event["candidateId"])
        reservation = self._existing(conn, context, "budget_reservations", event["reservationId"])
        artifact = self._existing(conn, context, "artifacts", event["inputArtifactId"])
        if (
            str(action["episode_id"]) != task["episodeId"]
            or str(node["episode_id"]) != task["episodeId"]
            or str(view["episode_id"]) != task["episodeId"]
            or str(view["contract_revision_id"]) != task["contractRevisionId"]
            or str(view["repository_state_id"]) != task["repositoryStateId"]
            or str(view["semantic_release_id"]) != task["semanticReleaseId"]
            or action["kind"] != config["definitionName"]
            or action["tool_definition_digest"] != config["definitionSha256"]
            or action["proposal"] != ActionProposal(config["definitionName"]).to_dict()
            or str(action["stdin_artifact_id"]) != event["inputArtifactId"]
            or artifact["digest"] != event["inputSha256"]
            or str(reservation["episode_id"]) != task["episodeId"]
        ):
            raise ScopeDeniedError("Qualification case substitutes an existing execution owner")
        if event["phase"] == "prepared":
            lease = conn.execute(
                "SELECT * FROM workspace_leases WHERE workspace_id=%s AND candidate_id=%s",
                (context.workspace_id, candidate["id"]),
            ).fetchone()
            prior = conn.execute(
                "SELECT id FROM action_runs WHERE workspace_id=%s AND action_id=%s",
                (context.workspace_id, action["id"]),
            ).fetchone()
            if (
                prior is not None
                or str(candidate["state_id"]) != task["repositoryStateId"]
                or reservation["status"] != "reserved"
                or not lease
                or lease["owner"] != event["leaseOwner"]
                or lease["generation"] != event["leaseGeneration"]
                or lease["status"] != "active"
            ):
                raise ScopeDeniedError(
                    "Qualification preparation must precede one admitted gateway dispatch"
                )
            return
        run = self._existing(conn, context, "action_runs", event["runId"])
        completion = conn.execute(
            "SELECT * FROM action_completions WHERE workspace_id=%s AND run_id=%s",
            (context.workspace_id, run["id"]),
        ).fetchone()
        result = self._existing(conn, context, "artifacts", event["resultArtifactId"])
        if (
            str(run["action_id"]) != event["actionId"]
            or str(run["candidate_id"]) != event["candidateId"]
            or str(run["reservation_id"]) != event["reservationId"]
            or str(run["episode_id"]) != task["episodeId"]
            or str(run["contract_revision_id"]) != task["contractRevisionId"]
            or str(run["input_state_id"]) != task["repositoryStateId"]
            or run["input_fingerprint"] != task["repositorySha256"]
            or run["lease_owner"] != event["leaseOwner"]
            or run["lease_generation"] != event["leaseGeneration"]
            or completion is None
            or reservation["status"] != "settled"
            or str(completion["stdout_artifact_id"]) != event["resultArtifactId"]
            or result["digest"] != event["resultSha256"]
            or completion["receipt_digest"] != event["receiptSha256"]
            or completion["elapsed_ms"] != event["elapsedMs"]
            or completion["result_fingerprint"] != task["repositorySha256"]
            or run["status"] != completion["outcome"]
            or completion["details"].get("mcp_intent_hash") != event["invocationSha256"]
            or completion["details"].get("mcp_discovery_reference_sha256")
            != content_hash(config["reference"])
            or content_hash(completion["details"].get("isolation_identity"))
            != event["isolationSha256"]
            or completion["details"].get("dispatched") is not True
            or completion["details"].get("stdout_truncated") is not False
        ):
            raise ScopeDeniedError(
                "Qualification result lacks matching existing gateway completion evidence"
            )
        if event["resultCheck"] is not None and completion["outcome"] != "completed":
            raise ScopeDeniedError("A failed gateway invocation cannot pass an output oracle")

    def record_case(
        self, context: WorkspaceContext, identity: UUID, worker_token: UUID, value: dict[str, Any]
    ) -> None:
        with self.database.transaction(context) as conn:
            suite, row, token = self._claim_rows(conn, context, identity, worker_token, lock=True)
            specs = {case["id"]: case for case in suite["config"]["cases"]}
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("caseId"), str)
                or value["caseId"] not in specs
            ):
                raise InvalidInputError("Unknown admitted qualification case")
            event = self._case_event(value, specs[value["caseId"]])
            prior = row["payload"]["cases"].get(event["caseId"])
            if prior == event:
                if event["phase"] == "finished":
                    self._verify_case_owners(conn, context, suite["config"], event)
                return
            if row["state"] == "recovering" and event["phase"] == "prepared":
                raise ScopeDeniedError(
                    "Recovery cannot prepare or dispatch another qualification action"
                )
            if (prior is not None and prior["phase"] == "finished") or (
                event["phase"] == "finished" and prior is None
            ):
                raise ScopeDeniedError(
                    "Qualification evidence cannot replace or skip its retained preparation"
                )
            if prior is not None and any(event[key] != prior[key] for key in _PREPARED - {"phase"}):
                raise ScopeDeniedError(
                    "Qualification completion differs from its recorded preparation"
                )
            if event["phase"] == "prepared":
                preceding = list(specs).index(event["caseId"])
                for case_id in list(specs)[:preceding]:
                    if row["payload"]["cases"].get(case_id, {}).get("phase") != "finished":
                        raise ScopeDeniedError(
                            "Qualification cases must dispatch in their admitted order"
                        )
            others = list(row["payload"]["cases"].values())
            if any(
                item["storageSha256"] != event["storageSha256"]
                or item["nativePins"] != event["nativePins"]
                for item in others
            ):
                raise ScopeDeniedError("Qualification private storage or native identity changed")
            if any(
                event["nativePins"][key] != suite["config"][key]
                for key in ("consumerSha256", "configurationSha256")
            ) or event["nativePins"]["referenceSha256"] != content_hash(
                suite["config"]["reference"]
            ):
                raise ScopeDeniedError("Qualification native preparation differs from admission")
            if any(
                item["caseId"] != event["caseId"] and item["actionId"] == event["actionId"]
                for item in others
            ):
                raise ScopeDeniedError("An existing gateway action cannot qualify a different case")
            self._verify_case_owners(conn, context, suite["config"], event)
            row["payload"]["cases"][event["caseId"]] = event
            row["revision"] += 1
            self._save(conn, "attempt", row, token, "case_" + event["phase"])

    def _native_report(
        self,
        context: WorkspaceContext,
        identity: UUID,
        config: dict[str, Any],
        records: list[dict[str, Any]],
        value: Any,
    ) -> dict[str, Any]:
        fields = {
            "schema",
            "workspaceId",
            "principalId",
            "attemptId",
            "agentId",
            "suiteSha256",
            "suiteFileSha256",
            "consumerSha256",
            "configurationSha256",
            "installationSha256",
            "definitionSha256",
            "runtimeSha256",
            "imageSha256",
            "qualificationSha256",
            "agentHomeRevision",
            "storageSha256",
            "referenceSha256",
            "taskBinding",
            "observedAt",
            "completedAt",
            "casesSha256",
        }
        report = bounded_metadata(value, maximum=64 * 1024)
        if (
            not isinstance(report, dict)
            or set(report) != fields
            or report["schema"] != "potbot-mcp-qualification-report/v1"
            or report["workspaceId"] != str(context.workspace_id)
            or report["principalId"] != str(context.principal_id)
            or report["attemptId"] != str(identity)
            or type(report["agentHomeRevision"]) is not int
            or not 1 <= report["agentHomeRevision"] < 2**53
        ):
            raise ScopeDeniedError("Qualification requires exact native lifetime evidence")
        for key in fields:
            if key.endswith("Sha256"):
                _sha(report[key])
        for key in (
            "agentId",
            "suiteSha256",
            "suiteFileSha256",
            "consumerSha256",
            "configurationSha256",
            "installationSha256",
            "definitionSha256",
            "taskBinding",
        ):
            if report[key] != config[key]:
                raise ScopeDeniedError(
                    "Native qualification differs from the admitted installed or task owner"
                )
        if (
            report["referenceSha256"] != content_hash(config["reference"])
            or report["casesSha256"] != content_hash(records)
            or any(record["storageSha256"] != report["storageSha256"] for record in records)
            or any(
                record["nativePins"] != {key: report[key] for key in _NATIVE_PINS}
                for record in records
            )
        ):
            raise ScopeDeniedError("Native qualification differs from its retained case evidence")
        try:
            observed, completed = (
                datetime.fromisoformat(report["observedAt"]),
                datetime.fromisoformat(report["completedAt"]),
            )
            now = self.clock()
            if (
                observed.utcoffset() != timedelta(0)
                or completed.utcoffset() != timedelta(0)
                or not observed <= completed <= now
                or completed - observed > timedelta(seconds=600)
                or now - completed > timedelta(seconds=30)
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise RevisionConflictError(
                "Native qualification evidence is stale or inconsistent"
            ) from None
        return dict(report)

    def complete(
        self,
        context: WorkspaceContext,
        identity: UUID,
        worker_token: UUID,
        native_report: dict[str, Any],
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            suite, row, token = self._claim_rows(conn, context, identity, worker_token, lock=True)
            records = [
                row["payload"]["cases"][case["id"]]
                for case in suite["config"]["cases"]
                if case["id"] in row["payload"]["cases"]
            ]
            report = self._native_report(context, identity, suite["config"], records, native_report)
            not_run = []
            for event in records:
                if event["phase"] == "finished":
                    self._verify_case_owners(conn, context, suite["config"], event)
                elif row["state"] == "recovering":
                    run = conn.execute(
                        "SELECT id FROM action_runs WHERE workspace_id=%s AND action_id=%s",
                        (context.workspace_id, UUID(event["actionId"])),
                    ).fetchone()
                    reservation = self._existing(
                        conn, context, "budget_reservations", event["reservationId"]
                    )
                    if run is not None or reservation["status"] != "released":
                        raise ScopeDeniedError(
                            "Unknown gateway work cannot be published as recovered qualification"
                        )
                    not_run.append(event["caseId"])
                else:
                    raise ScopeDeniedError(
                        "A fresh qualification cannot publish incomplete gateway evidence"
                    )
            if row["state"] != "recovering" and len(records) != len(suite["config"]["cases"]):
                raise ScopeDeniedError("Fresh qualification requires every admitted case")
            row["payload"]["notRun"] = not_run
            row["state"] = (
                "failed"  # Make unstarted cases explicit in the immutable report projection.
            )
            cases = self._cases(suite, row)
            complete = all(case["status"] in {"passed", "failed"} for case in cases)
            passed = complete and all(case["status"] == "passed" for case in cases)
            payload = {"complete": complete, "passed": passed, "evidence": report, "cases": cases}
            retained = {
                "workspace_id": context.workspace_id,
                "principal_id": context.principal_id,
                "id": uuid4(),
                "suite_id": suite["id"],
                "attempt_id": identity,
                "payload": payload,
                "sha256": content_hash(payload),
            }
            retained["mac"] = _mac(token, "report", _body("report", retained))
            conn.execute(
                "INSERT INTO mcp_qualification_reports(workspace_id,id,principal_id,suite_id,attempt_id,payload,sha256,mac) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    retained["id"],
                    context.principal_id,
                    suite["id"],
                    identity,
                    Jsonb(payload),
                    retained["sha256"],
                    retained["mac"],
                ),
            )
            failure = next(
                (
                    reason
                    for reason in ("invalid_result_evidence", "tool_failed", "assertion_mismatch")
                    if any(case["reasonCode"] == reason for case in cases)
                ),
                "qualification_failed",
            )
            row["payload"].update(
                reportId=str(retained["id"]),
                reportSha256=retained["sha256"],
                reasonCode=None if passed else failure if complete else "report_incomplete",
            )
            row.update(
                state="succeeded" if passed else "failed",
                worker_token=None,
                revision=row["revision"] + 1,
            )
            saved = self._save(conn, "attempt", row, token, "report_retained")
            suite.update(
                revision=suite["revision"] + 1,
                active_attempt_id=None,
            )
            if suite["last_attempt_id"] == identity:
                # Explicit historical recovery retains its own report without
                # replacing evidence from a newer requested attempt.
                suite["last_report_id"] = retained["id"]
            self._save(conn, "suite", suite, token, "qualification_report_retained")
            return {
                **self._scope(context),
                "attempt": self._attempt_view(conn, context, saved, token),
            }

    def fail(
        self, context: WorkspaceContext, identity: UUID, worker_token: UUID, reason: str
    ) -> dict[str, Any]:
        if reason not in _FAILURES:
            raise InvalidInputError("Unknown safe qualification failure reason")
        with self.database.transaction(context) as conn:
            suite, row = self._attempt_rows(conn, context, identity)
            token = self._token()
            if (
                row["state"] not in {"running", "recovering"}
                or row["worker_token"] != _uuid(worker_token)
                or not self._valid(conn, "attempt", row, token)
            ):
                raise ScopeDeniedError("Qualification failure belongs to a fenced worker")
            assert token is not None
            pending = any(
                event["phase"] == "prepared" for event in row["payload"]["cases"].values()
            )
            if reason == "qualification_failed" and pending:
                reason = "qualification_indeterminate"
            state = (
                "indeterminate"
                if reason == "qualification_indeterminate"
                else "cancelled"
                if reason == "qualification_cancelled"
                else "failed"
            )
            row["payload"]["reasonCode"] = reason
            row.update(state=state, worker_token=None, revision=row["revision"] + 1)
            saved = self._save(conn, "attempt", row, token, "native_failed")
            if self._valid(conn, "suite", suite, token) and suite["active_attempt_id"] == identity:
                suite.update(active_attempt_id=None, revision=suite["revision"] + 1)
                self._save(conn, "suite", suite, token, "qualification_failed")
            return {
                **self._scope(context),
                "attempt": self._attempt_view(conn, context, saved, token),
            }

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
        request = {"id": str(_uuid(identity)), "revision": revision, "digest": _sha(digest)}
        with self.database.transaction(context) as conn:

            def stop() -> dict[str, Any]:
                suite, row = self._attempt_rows(conn, context, identity)
                self._expected("attempt", row, revision, digest)
                if row["state"] not in _ACTIVE:
                    raise RevisionConflictError(
                        "Only an active qualification attempt can be cancelled"
                    )
                if not self._valid(conn, "attempt", row, token):
                    # A terminal decision may stop invalid work; it must never
                    # re-admit its untrusted journal for positive recovery.
                    row["payload"]["retiredWithoutAdmission"] = True
                row["payload"]["reasonCode"] = "qualification_cancelled"
                row.update(state="cancelled", worker_token=None, revision=row["revision"] + 1)
                saved = self._save(conn, "attempt", row, token, "cancelled")
                if (
                    self._valid(conn, "suite", suite, token)
                    and suite["active_attempt_id"] == identity
                ):
                    suite.update(active_attempt_id=None, revision=suite["revision"] + 1)
                    self._save(conn, "suite", suite, token, "qualification_cancelled")
                return {
                    **self._scope(context),
                    "attempt": self._attempt_view(conn, context, saved, token),
                }

            return idempotent_command(
                conn, context, "mcp.qualification.cancel", _key(context, key), request, stop
            )
