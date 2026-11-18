"""Independent source citations and immutable comparison evidence.

ResearchRunStore authenticates execution, disclosure and accounting. This owner
then reads the actual retained artifacts and derives each exact quote. Reports
use the existing artifact/evidence ledger; they do not create a claim registry.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.research_evidence import (
    COMPARISON_SCHEMA,
    build_citation,
    citation_spec,
    claim_specs,
    compare_claims,
    digest,
    identity,
    timestamp,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import idempotent_command
from potbot.persistence.evidence import EvidenceStore, append_event
from potbot.research.contracts import ResearchAction

if TYPE_CHECKING:
    from potbot.controller.research_workbench import ResearchWorkbench

_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_REPORT_BYTES = 1_400_000
_RECORD_SCHEMA = "potbot-research-comparison-record/v1"


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
        if not isinstance(value, dict) or canonical_json(value).encode("utf-8") != raw:
            raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        raise StateMismatchError("Research evidence is not retained canonical JSON") from None
    return value


def _event_digest(event: dict[str, Any]) -> str:
    body = {
        "schema_version": 1,
        **{
            key: str(event[key]) if event[key] is not None else None
            for key in (
                "workspace_id",
                "id",
                "episode_id",
                "deployment_id",
                "causation_id",
                "correlation_id",
                "payload_id",
            )
        },
        **{key: event[key] for key in ("event_type", "producer_kind", "producer_id", "details")},
        **{key: event[key].isoformat() for key in ("occurred_at", "recorded_at")},
    }
    if event.get("run_id") is not None or event.get("node_id") is not None:
        body.update(
            {
                key: str(event[key]) if event[key] is not None else None
                for key in ("run_id", "node_id")
            }
        )
    return content_hash(body)


class ResearchEvidence:
    def __init__(self, workbench: ResearchWorkbench) -> None:
        self.workbench = workbench

    def _source(
        self,
        actor: WorkspaceContext,
        run_id: UUID,
        source_id: str,
        cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        digest(source_id)
        cached = cache if cache is not None else {}
        if str(run_id) not in cached:
            runs = self.workbench.runs(actor)
            # Audit is the authority; ordinary run success is insufficient.
            batch = runs.audit_external(actor, run_id)
            execution = runs.inspect_external(actor, run_id)
            if (
                batch.get("runId") != str(run_id)
                or batch.get("actionId") != execution["run"]["actionId"]
                or batch.get("actionIdentity") != execution["action"]["identity"]
            ):
                raise StateMismatchError("Research source audit and run identity differ")
            cached[str(run_id)] = (execution, batch)
        execution, batch = cached[str(run_id)]
        matches = [item for item in batch["documents"] if item.get("sourceId") == source_id]
        if len(matches) != 1:
            raise ScopeDeniedError("Source is not a fetched document in this research run")
        source = matches[0]
        if source.get("rawArtifactId") is None or source.get("extractionArtifactId") is None:
            raise StateMismatchError("Research document has no retained source extraction")
        store = self.workbench.artifact_store(actor)
        with self.workbench.database.transaction(actor) as conn:
            values = []
            for id_key, sha_key in (
                ("rawArtifactId", "rawSha256"),
                ("extractionArtifactId", "extractionSha256"),
            ):
                artifact_id = identity(source[id_key])
                record = store._metadata(conn, actor, artifact_id)
                if record["byte_length"] > _MAX_SOURCE_BYTES or record["digest"] != digest(
                    source[sha_key]
                ):
                    raise StateMismatchError("Research source artifact exceeds its verified bound")
                raw = store.read_in_transaction(conn, actor, artifact_id)
                if hashlib.sha256(raw).hexdigest() != source[sha_key]:
                    raise StateMismatchError("Research source artifact identity changed")
                values.append(raw)
        extraction = _json(values[1])
        if extraction.get("sourceSha256") != hashlib.sha256(values[0]).hexdigest():
            raise StateMismatchError("Research extraction does not reference its exact raw source")
        return execution, source, extraction

    def source(self, actor: WorkspaceContext, run_id: UUID, source_id: str) -> dict[str, Any]:
        execution, source, extraction = self._source(actor, run_id, source_id)
        return self.workbench.envelope(
            actor, execution=execution, source=source, extraction=extraction
        )

    def citation(
        self,
        actor: WorkspaceContext,
        value: object,
        *,
        cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        spec = citation_spec(value)
        execution, source, extraction = self._source(
            actor, identity(spec["runId"]), spec["sourceId"], cache
        )
        action = ResearchAction.from_dict(execution["action"])
        return build_citation(
            spec,
            source,
            extraction,
            research_scope=action.scope,
            receipt_sha256=digest(execution["run"]["receiptSha256"]),
            freshness_seconds=action.query.freshness_seconds,
        )

    def _claims(
        self,
        actor: WorkspaceContext,
        claims: object,
        task: dict[str, Any],
        need_id: UUID,
        revision_id: str,
        evaluated_at: datetime,
    ) -> list[dict[str, Any]]:
        specs = claim_specs(claims)
        cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        verified: dict[str, dict[str, Any]] = {}
        unavailable: dict[str, str] = {}
        for item in specs:
            for spec in item["citations"]:
                key = content_hash(spec)
                if key in verified or key in unavailable:
                    continue
                try:
                    citation = self.citation(actor, spec, cache=cache)
                    scope = citation["researchScope"]
                    if (
                        scope["workspace_id"] != str(actor.workspace_id)
                        or scope["task_id"] != task["episodeId"]
                        or scope["contract_revision_id"] != task["contractRevisionId"]
                        or scope["repository_state_id"] != task["repositoryStateId"]
                        or scope["semantic_release_digest"] != task["semanticReleaseSha256"]
                        or scope["need_id"] != str(need_id)
                        or scope["need_revision_id"] != revision_id
                    ):
                        unavailable[key] = "citation_scope_mismatch"
                    else:
                        verified[key] = citation
                except DomainError:
                    unavailable[key] = "citation_unavailable"
        return compare_claims(specs, verified, unavailable, evaluated_at=evaluated_at)

    @staticmethod
    def _report(
        actor: WorkspaceContext,
        episode_id: UUID,
        need_id: UUID,
        revision_id: str,
        context_sha256: str,
        evaluated_at: datetime,
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        states = {item["status"] for item in claims}
        report = {
            "schema": COMPARISON_SCHEMA,
            "workspaceId": str(actor.workspace_id),
            "episodeId": str(episode_id),
            "needId": str(need_id),
            "needRevisionId": revision_id,
            "contextSha256": context_sha256,
            "evaluatedAt": evaluated_at.isoformat(),
            "claims": claims,
            "status": "supported"
            if states == {"supported"}
            else "unsupported"
            if states == {"unsupported"}
            else "unresolved",
            "qualification": "verified_cited_source_agreement_only",
            "needResolved": False,
        }
        report["sha256"] = content_hash(report)
        if len(canonical_json(report).encode("utf-8")) > _MAX_REPORT_BYTES:
            raise InvalidInputError("Research comparison exceeds its report allowance")
        return report

    def compare(
        self,
        actor: WorkspaceContext,
        episode_id: UUID,
        need_id: UUID,
        context_sha256: str,
        claims: object,
        key: str,
    ) -> dict[str, Any]:
        from potbot.controller.research_workbench import command_key

        specs = claim_specs(claims)
        request = {
            "episodeId": str(episode_id),
            "needId": str(need_id),
            "contextSha256": digest(context_sha256),
            "claims": specs,
        }
        with self.workbench.database.transaction(actor) as conn:

            def write() -> dict[str, Any]:
                # The first retained run selects the already reviewed immutable
                # repository state. Cross-task/need references never infer it.
                first = specs[0]["citations"][0]
                execution = self.workbench.runs(actor).inspect_external(
                    actor, identity(first["runId"])
                )
                action = ResearchAction.from_dict(execution["action"])
                if action.scope.task_id != episode_id or action.scope.need_id != need_id:
                    raise ScopeDeniedError("Comparison sources must belong to the selected need")
                current = self.workbench._context(
                    conn, actor, episode_id, action.scope.repository_state_id, need_id, lock=True
                )
                if current["contextSha256"] != context_sha256 or not current["current"]:
                    raise RevisionConflictError(
                        "Research comparison context changed before publication"
                    )
                stamp = conn.execute("SELECT clock_timestamp() AS stamp").fetchone()["stamp"]
                revision = current["need"]["revisionId"]
                checked = self._claims(actor, specs, current["task"], need_id, revision, stamp)
                report = self._report(
                    actor, episode_id, need_id, revision, context_sha256, stamp, checked
                )
                record = {
                    "schema": _RECORD_SCHEMA,
                    "request": request,
                    "task": current["task"],
                    "report": report,
                }
                raw = canonical_json(record).encode("utf-8")
                if len(raw) > _MAX_REPORT_BYTES:
                    raise InvalidInputError("Research comparison record exceeds its allowance")
                artifact = self.workbench.artifact_store(actor).put_in_transaction(
                    conn, actor, raw, "application/vnd.potbot.research-comparison+json"
                )
                event = append_event(
                    conn,
                    actor,
                    "controller_decision",
                    payload_id=artifact["id"],
                    episode_id=episode_id,
                    producer_id="potbot.research_evidence",
                    details={
                        "kind": "research_cited_source_comparison",
                        "actorPrincipalId": str(actor.principal_id),
                        "reportSha256": report["sha256"],
                        "recordSha256": artifact["digest"],
                        "contextSha256": context_sha256,
                        "needId": str(need_id),
                        "needRevisionId": revision,
                    },
                )
                return {"evidenceId": str(event["id"]), "report": report}

            result = idempotent_command(
                conn,
                actor,
                "research_workbench_comparison",
                command_key(actor, key),
                request,
                write,
            )
        return self.workbench.envelope(actor, **result)

    def read(self, actor: WorkspaceContext, evidence_id: UUID) -> dict[str, Any]:
        event = EvidenceStore(self.workbench.database).read(actor, evidence_id)
        details = event["details"]
        if (
            event["event_type"] != "controller_decision"
            or event["producer_kind"] != "controller"
            or event["producer_id"] != "potbot.research_evidence"
            or not isinstance(details, dict)
            or details.get("kind") != "research_cited_source_comparison"
            or event["payload_id"] is None
            or _event_digest(event) != event["content_digest"]
        ):
            raise StateMismatchError("Research comparison evidence identity is unavailable")
        store = self.workbench.artifact_store(actor)
        metadata = store.metadata(actor, event["payload_id"])
        if (
            metadata["byte_length"] > _MAX_REPORT_BYTES
            or metadata["digest"] != details["recordSha256"]
        ):
            raise StateMismatchError("Research comparison artifact identity changed")
        record = _json(store.read(actor, event["payload_id"]))
        if (
            set(record) != {"schema", "request", "task", "report"}
            or record["schema"] != _RECORD_SCHEMA
        ):
            raise StateMismatchError("Research comparison record has an invalid envelope")
        report, request, task = record["report"], record["request"], record["task"]
        if (
            report.get("sha256")
            != content_hash({key: value for key, value in report.items() if key != "sha256"})
            or report["sha256"] != details["reportSha256"]
            or report["workspaceId"] != str(actor.workspace_id)
            or report["episodeId"] != str(event["episode_id"])
            or report["needId"] != details["needId"]
            or report["needRevisionId"] != details["needRevisionId"]
            or report["contextSha256"] != details["contextSha256"]
            or request["episodeId"] != report["episodeId"]
            or request["needId"] != report["needId"]
            or request["contextSha256"] != report["contextSha256"]
            or task["episodeId"] != report["episodeId"]
        ):
            raise StateMismatchError("Research comparison report differs from its evidence event")
        stamp = timestamp(report["evaluatedAt"])
        claims = self._claims(
            actor,
            request["claims"],
            task,
            identity(report["needId"]),
            report["needRevisionId"],
            stamp,
        )
        replay = self._report(
            actor,
            identity(report["episodeId"]),
            identity(report["needId"]),
            report["needRevisionId"],
            report["contextSha256"],
            stamp,
            claims,
        )
        if replay != report:
            raise StateMismatchError(
                "Research comparison no longer matches its verified source chain"
            )
        return self.workbench.envelope(actor, evidenceId=str(evidence_id), report=report)
