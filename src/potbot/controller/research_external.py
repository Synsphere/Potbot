"""External admission, dispatch receipts and verified completion for ResearchRunStore.

This module extends the existing research owner. It introduces no scheduler,
credential store, artifact authority or budget ledger. Every external send has a
durable, exact, current authorization and a separately retained response.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.jobs import _page_cursor, _parse_cursor
from potbot.controller.research_worker import ResearchLease, _action, _current, _run
from potbot.controller.tasks import lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution_events import ExecutionEventInput
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import idempotent_command
from potbot.persistence.evidence import append_event
from potbot.research.contracts import ResearchAction, ResearchCategory, ResearchOperation
from potbot.research.external_binding import (
    AUTHORIZATION_SCHEMA,
    BATCH_SCHEMA,
    RECEIPT_SCHEMA,
    RUN_SCHEMA,
    authorization_mac,
    bounded_key,
    require_setup,
    research_consumer_sha256,
)
from potbot.research.external_cog import EXTERNAL_RESEARCH_COG, external_research_outcome
from potbot.research.external_contracts import ExternalRequest, ExternalResponse, utc_text
from potbot.research.source_policy import ExternalResearchPolicy

if TYPE_CHECKING:
    from potbot.controller.research_worker import ResearchRunStore

_TERMINAL = frozenset({"succeeded", "blocked", "indeterminate"})
_ERRORS = frozenset(
    {
        "research_cancelled",
        "stale_scope",
        "policy_changed",
        "consumer_changed",
        "research_failed",
        "lease_expired",
        "usage_indeterminate",
        "request_failed",
        "budget_unavailable",
        "authority_changed",
        "runtime_exhausted",
        "provider_credential_unavailable",
    }
)


class ResearchRateWait(StateMismatchError):
    """A pre-dispatch wait; no request has been admitted or sent yet."""

    def __init__(self, milliseconds: int) -> None:
        self.milliseconds = milliseconds
        super().__init__("The configured research source rate interval has not elapsed")


def _limits(policy: ExternalResearchPolicy) -> dict[str, int]:
    interval = max(
        [rule.minimum_interval_ms for rule in policy.providers]
        + [rule.minimum_interval_ms for rule in policy.fetch_hosts]
        + [0]
    )
    return {
        "calls": policy.max_requests,
        "tool_ms": policy.max_requests * (policy.timeout_seconds * 1000 + interval)
        + policy.max_documents * 5000,
        "bytes_read": policy.max_requests * policy.max_source_bytes,
        "money_microunits": policy.max_requests
        * max([rule.cost_microunits_per_call for rule in policy.providers] + [0]),
        "concurrency": 1,
    }


def _external(
    conn: Any, context: WorkspaceContext, run_id: UUID, *, lock: bool = False
) -> dict[str, Any]:
    if not isinstance(run_id, UUID):
        raise InvalidInputError("Research execution requires a UUID")
    row = _run(conn, context, run_id, lock=lock)
    if row["execution_kind"] != "external":
        raise ScopeDeniedError("The run is not an external research attempt")
    return row


def _token(store: ResearchRunStore) -> str:
    token = store.setup_token()
    if not isinstance(token, str) or not token:
        raise ScopeDeniedError("Native research setup authority is unavailable")
    return token


def _pdf_extractor(store: ResearchRunStore, context: WorkspaceContext) -> Any:
    from potbot.research.pdf_extraction import configured_pdf_extractor

    extractor = configured_pdf_extractor(store.database, context)
    if extractor is not None:
        extractor.profile.verify_executable()
    return extractor


def _signed(value: dict[str, Any], token: str) -> dict[str, Any]:
    return {**value, "mac": authorization_mac(value, token)}


def _verify_signed(value: Any, token: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("mac"), str):
        raise StateMismatchError("Research receipt lacks its native authenticity binding")
    body = {key: item for key, item in value.items() if key != "mac"}
    if not hmac.compare_digest(value["mac"], authorization_mac(body, token)):
        raise StateMismatchError("Research receipt authenticity binding changed")
    return body


def _authorization(
    store: ResearchRunStore,
    conn: Any,
    context: WorkspaceContext,
    row: dict[str, Any],
    *,
    policy: ExternalResearchPolicy | None = None,
    current: bool = False,
) -> tuple[ResearchAction, ExternalResearchPolicy, dict[str, Any]]:
    value = row["external_authorization"]
    token = _token(store)
    if not isinstance(value, dict) or not hmac.compare_digest(
        row["external_mac"], authorization_mac(value, token)
    ):
        raise ScopeDeniedError("External research authorization is unavailable or changed")
    body = {key: item for key, item in value.items() if key != "sha256"}
    action, snapshot = _action(conn, context, row["action_id"])
    if (
        value.get("schema") != AUTHORIZATION_SCHEMA
        or value.get("sha256") != content_hash(body)
        or value.get("workspaceId") != str(context.workspace_id)
        or value.get("runId") != str(row["id"])
        or value.get("actionId") != str(action.action_id)
        or value.get("actionIdentity") != action.identity
        or value.get("actorPrincipalId") != str(row["external_actor_id"])
        or value.get("reservationId") != str(row["external_reservation_id"])
        or value.get("disclosureDecisionId") != str(row["external_decision_id"])
        or value.get("sourceArtifactId") != str(row["source_artifact_id"])
        or value.get("sourceRevision") != row["source_revision"]
        or row["action_identity"] != action.identity
        or row["source_artifact_id"] != snapshot["snapshot_artifact_id"]
        or row["source_revision"] != snapshot["content_digest"]
    ):
        raise StateMismatchError("Research authorization differs from immutable action scope")
    retained_policy = ExternalResearchPolicy.from_dict(value["policy"])
    if retained_policy.fingerprint != value["policySha256"] or value["limits"] != _limits(
        retained_policy
    ):
        raise StateMismatchError("Research policy or allowance binding changed")
    if policy is not None and policy.fingerprint != retained_policy.fingerprint:
        raise RevisionConflictError("Research deployment policy changed")
    if value["consumerSha256"] != research_consumer_sha256():
        raise RevisionConflictError("The external research consumer changed")
    extractor = _pdf_extractor(store, context)
    if value["pdfProfileSha256"] != (extractor.fingerprint if extractor else None):
        raise RevisionConflictError("The external research PDF extraction profile changed")
    decision = conn.execute(
        "SELECT * FROM evidence_events WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, row["external_decision_id"]),
    ).fetchone()
    expected = _disclosure(action, retained_policy, row["external_actor_id"], row["id"])
    if (
        decision is None
        or decision["event_type"] != "controller_decision"
        or decision["producer_kind"] != "controller"
        or decision["producer_id"] != "potbot.external_research"
        or decision["episode_id"] != action.scope.task_id
        or decision["details"] != expected
        or decision["content_digest"] != value["disclosureSha256"]
    ):
        raise ScopeDeniedError("Exact query disclosure decision is unavailable")
    reservation = conn.execute(
        "SELECT * FROM budget_reservations WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, row["external_reservation_id"]),
    ).fetchone()
    if (
        reservation is None
        or reservation["episode_id"] != action.scope.task_id
        or reservation["amounts"] != value["limits"]
        or (current and reservation["status"] != "reserved")
    ):
        raise ScopeDeniedError("External research budget admission is unavailable")
    if current:
        cancelled = (
            row["cancel_requested"]
            or conn.execute(
                "SELECT 1 FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
                "AND telemetry->>'phase'='cancellation_requested' LIMIT 1",
                (context.workspace_id, row["id"]),
            ).fetchone()
            is not None
        )
        if cancelled:
            raise OperationCancelledError("External research cancellation was requested")
        _current(conn, context, action, snapshot)
    return action, retained_policy, value


def _disclosure(
    action: ResearchAction, policy: ExternalResearchPolicy, actor: UUID, run_id: UUID
) -> dict[str, Any]:
    return {
        "schema": "potbot-research-disclosure-decision/v1",
        "decision": "allow",
        "runId": str(run_id),
        "actionId": str(action.action_id),
        "actionIdentity": action.identity,
        "actorPrincipalId": str(actor),
        "querySha256": hashlib.sha256(action.query.search_text.encode()).hexdigest(),
        "policySha256": policy.fingerprint,
        "providers": [rule.provider for rule in policy.providers],
        "fetchHosts": [rule.host for rule in policy.fetch_hosts],
        "visibility": "workspace",
        "disclosureConfirmed": True,
    }


def queue_external(
    store: ResearchRunStore,
    context: WorkspaceContext,
    action_id: UUID,
    policy: ExternalResearchPolicy,
    key: str,
    *,
    setup_token: str,
    disclosure_confirmed: bool,
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    bounded_key(key)
    if not isinstance(action_id, UUID) or not isinstance(policy, ExternalResearchPolicy):
        raise InvalidInputError("External research requires an admitted action and trusted policy")
    if (
        disclosure_confirmed is not True
        or not policy.allow_query_disclosure
        or not policy.providers
    ):
        raise ScopeDeniedError("External research requires explicit query disclosure approval")
    if policy.max_requests < len(policy.providers):
        raise InvalidInputError("Research request allowance cannot cover the configured providers")
    request = {
        "actionId": str(action_id),
        "policySha256": policy.fingerprint,
        "disclosureConfirmed": True,
    }
    with store.database.transaction(context) as conn:

        def create() -> dict[str, Any]:
            token = require_setup(setup_token, store.setup_token())
            conn.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                (f"{context.workspace_id}:research-queue:{action_id}",),
            )
            action, snapshot = _action(conn, context, action_id)
            lock_episode(conn, context, action.scope.task_id)
            if (
                action.query.category != ResearchCategory.DOCUMENTATION
                or action.query.operation != ResearchOperation.QUERY_SOURCE
                or action.query.locator != "external-research"
                or action.query.target_version != policy.fingerprint
                or action.query.max_results != policy.max_results
                or action.query.max_source_bytes != policy.max_source_bytes
                or not 1 <= len(action.query.search_text) <= 500
            ):
                raise InvalidInputError(
                    "External research action differs from its exact policy selector"
                )
            _current(conn, context, action, snapshot)
            prior = conn.execute(
                "SELECT * FROM research_runs WHERE workspace_id=%s AND action_id=%s",
                (context.workspace_id, action_id),
            ).fetchone()
            if prior is not None:
                if prior["execution_kind"] != "external":
                    raise StateMismatchError(
                        "The action already belongs to another research execution kind"
                    )
                _authorization(store, conn, context, dict(prior), policy=policy)
                return _view(store, conn, context, dict(prior))
            run_id = uuid4()
            reservation = BudgetLedger(store.database).reserve_in_transaction(
                conn, context, action.scope.task_id, _limits(policy), f"research.external:{run_id}"
            )
            event = append_event(
                conn,
                context,
                "controller_decision",
                episode_id=action.scope.task_id,
                producer_id="potbot.external_research",
                correlation_id=run_id,
                details=_disclosure(action, policy, context.principal_id, run_id),
            )
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            extractor = _pdf_extractor(store, context)
            value = {
                "schema": AUTHORIZATION_SCHEMA,
                "workspaceId": str(context.workspace_id),
                "runId": str(run_id),
                "actionId": str(action_id),
                "actionIdentity": action.identity,
                "actorPrincipalId": str(context.principal_id),
                "policy": policy.to_dict(),
                "policySha256": policy.fingerprint,
                "consumerSha256": research_consumer_sha256(),
                "pdfProfileSha256": extractor.fingerprint if extractor else None,
                "disclosureDecisionId": str(event["id"]),
                "disclosureSha256": event["content_digest"],
                "reservationId": reservation["id"],
                "limits": _limits(policy),
                "sourceArtifactId": str(snapshot["snapshot_artifact_id"]),
                "sourceRevision": snapshot["content_digest"],
                "issuedAt": utc_text(now),
                "visibility": "workspace",
            }
            value["sha256"] = content_hash(value)
            row = conn.execute(
                "INSERT INTO research_runs(workspace_id,id,episode_id,action_id,source_artifact_id,"
                "action_identity,source_revision,execution_kind,external_actor_id,external_authorization,"
                "external_mac,external_reservation_id,external_decision_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'external',%s,%s,%s,%s,%s) RETURNING *",
                (
                    context.workspace_id,
                    run_id,
                    action.scope.task_id,
                    action_id,
                    snapshot["snapshot_artifact_id"],
                    action.identity,
                    snapshot["content_digest"],
                    context.principal_id,
                    Jsonb(value),
                    authorization_mac(value, token),
                    UUID(reservation["id"]),
                    event["id"],
                ),
            ).fetchone()
            return _view(store, conn, context, dict(row))

        return idempotent_command(
            conn,
            context,
            "research.external.queue",
            content_hash([str(context.principal_id), key]),
            request,
            create,
        )


def _monitor(
    store: ResearchRunStore,
    conn: Any,
    context: WorkspaceContext,
    row: dict[str, Any],
    stage: Any,
    phase: str,
    *,
    child_id: UUID | None = None,
    parent_id: UUID | None = None,
    parent_run: UUID | None = None,
    output: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = row["id"] if child_id is None else child_id
    if parent_id is None and child_id is None:
        previous = conn.execute(
            "SELECT id FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
            "ORDER BY sequence DESC LIMIT 1",
            (context.workspace_id, row["id"]),
        ).fetchone()
        if previous is not None:
            parent_id, parent_run = previous["id"], row["id"]
    values = {
        "phase": phase,
        "researchRunId": str(row["id"]),
        "actionId": str(row["action_id"]),
        "qualification": "not_qualified",
        "metrics": {"memoryBytes": None, "cpuMs": None},
        **(telemetry or {}),
    }
    event = ExecutionEventInput(
        event_type_ref="synny://type/potbot-research-external-event",
        event_type_version="1",
        subject_type_ref=EXTERNAL_RESEARCH_COG.cog_type_ref,
        subject_version="1",
        run_id=identity,
        trigger_kind="parent_event" if parent_id else "api_request",
        trigger_ref=f"syn://event/{parent_id}"
        if parent_id
        else f"research://action/{row['action_id']}",
        trigger_input_sha256=row["action_identity"],
        emitter_run_id=identity,
        stage=stage,
        parent_event_id=parent_id,
        parent_run_id=parent_run,
        output_artifact_ref=f"artifact://{output['id']}" if output else None,
        output_sha256=output["digest"] if output else None,
        elapsed_ms=elapsed_ms,
        telemetry_json=canonical_json(values),
    )
    return ExecutionEventService(store.database)._append(conn, context, event)


def claim_external(
    store: ResearchRunStore,
    context: WorkspaceContext,
    worker_id: str,
    policy: ExternalResearchPolicy,
    *,
    lease_seconds: int = 90,
) -> ResearchLease | None:
    if not isinstance(worker_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", worker_id
    ):
        raise InvalidInputError("Research worker identity must be bounded and literal")
    if type(lease_seconds) is not int or not 5 <= lease_seconds <= 900:
        raise InvalidInputError("External research lease must be 5..900 seconds")
    with store.database.transaction(context) as conn:
        found = conn.execute(
            "SELECT * FROM research_runs WHERE workspace_id=%s AND execution_kind='external' "
            "AND state='queued' ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1",
            (context.workspace_id,),
        ).fetchone()
        if found is None:
            return None
        row = dict(found)
        lock_episode(conn, context, row["episode_id"])
        try:
            _authorization(store, conn, context, row, policy=policy, current=True)
        except (
            RevisionConflictError,
            ScopeDeniedError,
            StateMismatchError,
            OperationCancelledError,
        ):
            _finish_negative(store, conn, context, row, "authority_changed")
            return None
        claimed = conn.execute(
            "UPDATE research_runs SET state='running',generation=generation+1,revision=revision+1,"
            "lease_owner=%s,lease_until=clock_timestamp()+make_interval(secs=>%s),"
            "started_at=clock_timestamp(),updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
            (worker_id, lease_seconds, context.workspace_id, row["id"]),
        ).fetchone()
        row = dict(claimed)
        _monitor(store, conn, context, row, "started", "research_started")
        return ResearchLease(
            context.workspace_id,
            row["id"],
            row["action_id"],
            row["source_artifact_id"],
            row["source_revision"],
            row["action_identity"],
            worker_id,
            row["generation"],
            row["lease_until"],
        )


def _lease(
    store: ResearchRunStore, conn: Any, context: WorkspaceContext, lease: ResearchLease
) -> dict[str, Any]:
    if not isinstance(lease, ResearchLease) or lease.workspace_id != context.workspace_id:
        raise ScopeDeniedError("External research lease belongs to another workspace")
    row = _external(conn, context, lease.run_id, lock=True)
    if (
        row["state"] != "running"
        or row["lease_owner"] != lease.worker_id
        or row["generation"] != lease.generation
        or row["action_id"] != lease.action_id
        or row["source_artifact_id"] != lease.source_artifact_id
        or row["source_revision"] != lease.source_revision
        or row["action_identity"] != lease.action_identity
        or not conn.execute(
            "SELECT %s>clock_timestamp() AS valid", (row["lease_until"],)
        ).fetchone()["valid"]
    ):
        raise RevisionConflictError("External research lease expired or was superseded")
    return row


def _receipts(
    store: ResearchRunStore, conn: Any, context: WorkspaceContext, row: dict[str, Any]
) -> list[dict[str, Any]]:
    token = _token(store)
    found = conn.execute(
        "SELECT q.*,s.id AS response_id,s.content AS response_content,s.response_sha256,"
        "s.raw_artifact_id FROM research_external_requests q LEFT JOIN research_external_responses s "
        "ON s.workspace_id=q.workspace_id AND s.request_id=q.id "
        "WHERE q.workspace_id=%s AND q.run_id=%s ORDER BY q.ordinal",
        (context.workspace_id, row["id"]),
    ).fetchall()
    result = []
    for ordinal, item in enumerate(found, 1):
        request = _verify_signed(item["content"], token)
        if (
            item["ordinal"] != ordinal
            or request.get("requestSha256") != item["request_sha256"]
            or request.get("requestId") != str(item["id"])
            or request.get("runId") != str(row["id"])
            or request.get("authorizationSha256") != row["external_authorization"]["sha256"]
            or content_hash(request.get("request")) != item["request_sha256"]
            or request.get("eventId") != str(item["event_id"])
        ):
            raise StateMismatchError("External request receipt identity changed")
        dispatched = conn.execute(
            "SELECT * FROM syn_execution_events WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, item["event_id"]),
        ).fetchone()
        if (
            dispatched is None
            or dispatched["run_id"] != item["id"]
            or dispatched["parent_run_id"] != row["id"]
            or dispatched["stage"] != "started"
            or dispatched["subject_type_ref"] != EXTERNAL_RESEARCH_COG.cog_type_ref
            or dispatched["trigger_input_sha256"] != row["action_identity"]
            or dispatched["telemetry"].get("phase") != "request_dispatched"
            or dispatched["telemetry"].get("requestSha256") != item["request_sha256"]
            or dispatched["telemetry"].get("researchRunId") != str(row["id"])
            or dispatched["telemetry"].get("ordinal") != ordinal
        ):
            raise StateMismatchError(
                "Research dispatch monitoring does not match its signed request"
            )
        response = None
        if item["response_id"] is not None:
            response = _verify_signed(item["response_content"], token)
            if (
                content_hash(item["response_content"]) != item["response_sha256"]
                or response.get("requestId") != str(item["id"])
                or response.get("authorizationSha256") != row["external_authorization"]["sha256"]
                or response.get("response", {}).get("requestSha256") != item["request_sha256"]
                or response.get("rawArtifactId")
                != (str(item["raw_artifact_id"]) if item["raw_artifact_id"] else None)
            ):
                raise StateMismatchError("External response receipt identity changed")
            observed = conn.execute(
                "SELECT * FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
                "AND telemetry->>'phase'='request_observed'",
                (context.workspace_id, item["id"]),
            ).fetchall()
            output = response["response"]
            expected_stage = (
                "completed"
                if output["status"] == "fetched"
                else (
                    "indeterminate"
                    if output["status"] in {"timed_out", "indeterminate"}
                    else "failed"
                )
            )
            if (
                len(observed) != 1
                or observed[0]["parent_event_id"] != item["event_id"]
                or observed[0]["parent_run_id"] != item["id"]
                or observed[0]["stage"] != expected_stage
                or observed[0]["subject_type_ref"] != EXTERNAL_RESEARCH_COG.cog_type_ref
                or observed[0]["trigger_input_sha256"] != row["action_identity"]
                or observed[0]["elapsed_ms"] != output["elapsedMs"]
                or observed[0]["output_sha256"] != output["sourceSha256"]
                or observed[0]["output_artifact_ref"]
                != (
                    f"artifact://{response['rawArtifactId']}" if response["rawArtifactId"] else None
                )
                or observed[0]["telemetry"].get("status") != output["status"]
                or observed[0]["telemetry"].get("requestSha256") != item["request_sha256"]
                or observed[0]["telemetry"].get("receivedBytes") != output["receivedBytes"]
                or observed[0]["telemetry"].get("usageOrigin") != response["usageOrigin"]
            ):
                raise StateMismatchError(
                    "Research response monitoring does not match retained bytes and usage"
                )
        result.append({**dict(item), "request": request, "response": response})
    return result


def _raw(
    store: ResearchRunStore, conn: Any, context: WorkspaceContext, receipt: dict[str, Any]
) -> bytes:
    body = receipt["response"]
    if body is None or body["rawArtifactId"] is None or body["response"]["status"] != "fetched":
        raise StateMismatchError("Research source has no complete retained response")
    response = body["response"]
    metadata = store.artifacts._metadata(conn, context, UUID(body["rawArtifactId"]))
    if metadata["byte_length"] > 262144 or metadata["byte_length"] != response["receivedBytes"]:
        raise StateMismatchError("Retained research response length changed")
    raw = store.artifacts.read_in_transaction(conn, context, UUID(body["rawArtifactId"]))
    if hashlib.sha256(raw).hexdigest() != response["sourceSha256"]:
        raise StateMismatchError("Retained research response digest changed")
    return raw


def _search_results(
    store: ResearchRunStore,
    conn: Any,
    context: WorkspaceContext,
    receipts: list[dict[str, Any]],
    policy: ExternalResearchPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from potbot.research.providers import merge_search_results, normalize_search_results

    batches = []
    for item in receipts:
        descriptor, response = item["request"]["request"], item["response"]
        if (
            descriptor["kind"] != "search"
            or response is None
            or response["response"]["status"] != "fetched"
        ):
            continue
        raw = _raw(store, conn, context, item)
        normalized = normalize_search_results(
            descriptor["provider"],
            raw,
            retrieved_at=datetime.fromisoformat(
                response["response"]["retrievedAt"].replace("Z", "+00:00")
            ),
            max_results=policy.max_results,
        )
        batches.append(normalized)
    return batches, merge_search_results(batches)


def _prepared_request(
    store: ResearchRunStore,
    conn: Any,
    context: WorkspaceContext,
    row: dict[str, Any],
    action: ResearchAction,
    policy: ExternalResearchPolicy,
    request: ExternalRequest,
    receipts: list[dict[str, Any]],
) -> tuple[str, int]:
    from potbot.research.providers import (
        fetch_request,
        robots_interval_ms,
        robots_permits,
        robots_request,
        search_request,
    )

    if not isinstance(request, ExternalRequest):
        raise InvalidInputError("Research transport requires a typed exact request")
    if any(item["response"] is None for item in receipts):
        raise StateMismatchError("An earlier external send has an unresolved outcome")
    if any(item["request_sha256"] == request.fingerprint for item in receipts):
        raise StateMismatchError("External research never retries an admitted send")
    searches = [item for item in receipts if item["request"]["request"]["kind"] == "search"]
    providers = [rule.provider for rule in policy.providers]
    for index, item in enumerate(searches):
        if index >= len(providers) or item["request"]["request"]["provider"] != providers[index]:
            raise StateMismatchError("Research provider order changed")
        response = item["response"]["response"]
        if response["status"] != "fetched" and response["status"] not in policy.fallback_statuses:
            raise ScopeDeniedError(
                "The research policy does not permit continuation after this provider outcome"
            )
    if request.kind == "search":
        if (
            len(searches) != len(receipts)
            or len(searches) >= len(providers)
            or request.provider != providers[len(searches)]
        ):
            raise ScopeDeniedError("Research search is not the next separately disclosed provider")
        prepared = search_request(policy, request.provider, action.query.search_text)
        rule = policy.require_provider(request.provider)
        interval = rule.minimum_interval_ms
        rate_key = f"provider:{request.provider}"
    else:
        if len(searches) != len(providers):
            raise ScopeDeniedError(
                "Research document retrieval requires completed provider comparison"
            )
        _, merged = _search_results(store, conn, context, receipts, policy)
        targets = [item["canonicalUrl"] for item in merged[: policy.max_documents]]
        if request.kind == "robots":
            matching = [
                url for url in targets if urlsplit(url).hostname == urlsplit(request.url).hostname
            ]
            if not matching:
                raise ScopeDeniedError("Robots request does not belong to a retained result host")
            target = matching[0]
            prepared = robots_request(policy, target)
        else:
            if request.url not in targets:
                raise ScopeDeniedError(
                    "Fetch URL is not one of the bounded retained search results"
                )
            target = request.url
            prepared = fetch_request(policy, target)
        host_rule = policy.require_fetch_url(target)
        interval = host_rule.minimum_interval_ms
        rate_key = f"host:{host_rule.host}"
        if request.kind == "fetch":
            robot_sha = robots_request(policy, target).fingerprint
            robots = next((item for item in receipts if item["request_sha256"] == robot_sha), None)
            if (
                robots is None
                or robots["response"] is None
                or robots["response"]["response"]["status"] != "fetched"
            ):
                raise ScopeDeniedError("Research fetch requires complete retained robots evidence")
            raw = _raw(store, conn, context, robots)
            if not robots_permits(raw, target):
                raise ScopeDeniedError("The retained robots policy disallows this fetch")
            interval = max(interval, robots_interval_ms(raw, target))
    if prepared.fingerprint != request.fingerprint:
        raise ScopeDeniedError("Prepared external request differs from exact approved operation")
    return rate_key, interval


def before_send_external(
    store: ResearchRunStore,
    context: WorkspaceContext,
    lease: ResearchLease,
    policy: ExternalResearchPolicy,
    request: ExternalRequest,
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    with store.database.transaction(context) as conn:
        row = _lease(store, conn, context, lease)
        action, _, authorization = _authorization(
            store, conn, context, row, policy=policy, current=True
        )
        receipts = _receipts(store, conn, context, row)
        if (
            conn.execute(
                "SELECT 1 FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
                "AND telemetry->>'phase'='processing_started' LIMIT 1",
                (context.workspace_id, row["id"]),
            ).fetchone()
            is not None
        ):
            raise ScopeDeniedError("External sends cannot follow source processing")
        if len(receipts) >= policy.max_requests:
            raise ScopeDeniedError("External research request allowance is exhausted")
        rate_key, interval = _prepared_request(
            store, conn, context, row, action, policy, request, receipts
        )
        conn.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
            (f"{context.workspace_id}:research-rate:{rate_key}",),
        )
        last = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (clock_timestamp()-max(created_at)))*1000 AS elapsed "
            "FROM research_external_requests WHERE workspace_id=%s AND rate_key=%s",
            (context.workspace_id, rate_key),
        ).fetchone()["elapsed"]
        if last is not None and last < interval:
            raise ResearchRateWait(max(1, interval - int(last)))
        allowed = conn.execute(
            "SELECT clock_timestamp() < %s+make_interval(secs=>%s) AS valid",
            (row["started_at"], authorization["limits"]["tool_ms"] / 1000),
        ).fetchone()["valid"]
        if not allowed:
            raise StateMismatchError("Research total runtime allowance is exhausted")
        # Repeat all live task/need/reservation and cancellation checks at the
        # actual durable dispatch boundary, after source/rate locks were taken.
        _authorization(store, conn, context, row, policy=policy, current=True)
        request_id = uuid4()
        parent = conn.execute(
            "SELECT id FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
            "ORDER BY sequence DESC LIMIT 1",
            (context.workspace_id, row["id"]),
        ).fetchone()
        if parent is None:
            raise StateMismatchError("Research start monitoring receipt is unavailable")
        event = _monitor(
            store,
            conn,
            context,
            row,
            "started",
            "request_dispatched",
            child_id=request_id,
            parent_id=parent["id"],
            parent_run=row["id"],
            telemetry={
                "requestSha256": request.fingerprint,
                "kind": request.kind,
                "provider": request.provider,
                "ordinal": len(receipts) + 1,
            },
        )
        body = {
            "schema": "potbot-research-dispatch-receipt/v1",
            "runId": str(row["id"]),
            "requestId": str(request_id),
            "requestSha256": request.fingerprint,
            "authorizationSha256": authorization["sha256"],
            "request": request.to_dict(),
            "ordinal": len(receipts) + 1,
            "eventId": event["id"],
        }
        conn.execute(
            "INSERT INTO research_external_requests(workspace_id,id,episode_id,run_id,ordinal,"
            "request_sha256,rate_key,event_id,content) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                context.workspace_id,
                request_id,
                row["episode_id"],
                row["id"],
                len(receipts) + 1,
                request.fingerprint,
                rate_key,
                UUID(event["id"]),
                Jsonb(_signed(body, _token(store))),
            ),
        )
        updated = conn.execute(
            "UPDATE research_runs SET revision=revision+1,updated_at=clock_timestamp(),"
            "lease_until=clock_timestamp()+interval '120 seconds' WHERE workspace_id=%s AND id=%s "
            "AND state='running' AND lease_owner=%s AND generation=%s AND lease_until>clock_timestamp() RETURNING id",
            (context.workspace_id, row["id"], lease.worker_id, lease.generation),
        ).fetchone()
        if updated is None:
            raise RevisionConflictError("Research lease expired at dispatch")
        return body


def refresh_external_wait(
    store: ResearchRunStore,
    context: WorkspaceContext,
    lease: ResearchLease,
    policy: ExternalResearchPolicy,
) -> None:
    """Keep one current worker alive during an explicitly configured rate wait."""
    with store.database.transaction(context) as conn:
        row = _lease(store, conn, context, lease)
        _, _, authorization = _authorization(store, conn, context, row, policy=policy, current=True)
        valid = conn.execute(
            "SELECT clock_timestamp() < %s+make_interval(secs=>%s) AS valid",
            (row["started_at"], authorization["limits"]["tool_ms"] / 1000),
        ).fetchone()["valid"]
        if not valid:
            raise StateMismatchError("Research total runtime allowance is exhausted")
        conn.execute(
            "UPDATE research_runs SET lease_until=clock_timestamp()+interval '120 seconds',"
            "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, row["id"]),
        )


def _usage(
    receipts: list[dict[str, Any]], policy: ExternalResearchPolicy
) -> tuple[dict[str, int], str, bool]:
    totals = {"calls": 0, "tool_ms": 0, "bytes_read": 0, "money_microunits": 0}
    origins = set()
    uncertain = False
    for item in receipts:
        retained = item["response"]
        if retained is None:
            uncertain = True
            continue
        response = retained["response"]
        origins.add(retained["usageOrigin"])
        sent = response["requestSent"]
        uncertain |= sent and response["status"] in {"timed_out", "indeterminate"}
        totals["calls"] += int(sent)
        totals["tool_ms"] += response["elapsedMs"]
        totals["bytes_read"] += response["receivedBytes"]
        provider = item["request"]["request"]["provider"]
        if sent and provider is not None:
            totals["money_microunits"] += policy.require_provider(provider).cost_microunits_per_call
    if len(origins) > 1:
        raise StateMismatchError(
            "Synthetic and measured research transport cannot share one attempt"
        )
    origin = (
        "synthetic"
        if origins == {"synthetic"}
        else ("conservative_estimate" if totals["money_microunits"] else "measured")
    )
    return totals, origin, uncertain


def _build_batch(
    store: ResearchRunStore,
    conn: Any,
    context: WorkspaceContext,
    row: dict[str, Any],
    action: ResearchAction,
    policy: ExternalResearchPolicy,
    receipts: list[dict[str, Any]],
    *,
    retained: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from potbot.research.extraction import extract_document, verify_extraction, verify_retained_pdf
    from potbot.research.providers import fetch_request, robots_permits, robots_request

    usage, origin, uncertain = _usage(receipts, policy)
    if uncertain:
        raise StateMismatchError("Uncertain research sends cannot publish a verified result")
    batches, merged = _search_results(store, conn, context, receipts, policy)
    provider_receipts = [
        item for item in receipts if item["request"]["request"]["kind"] == "search"
    ]
    if len(provider_receipts) != len(policy.providers):
        raise StateMismatchError(
            "Research completion requires every separately scheduled provider outcome"
        )
    reasons = []
    for rule, item in zip(policy.providers, provider_receipts, strict=True):
        status = item["response"]["response"]["status"]
        if item["request"]["request"]["provider"] != rule.provider:
            raise StateMismatchError("Research completion provider order changed")
        if status != "fetched":
            if status not in policy.fallback_statuses:
                raise StateMismatchError(
                    "Research completion follows a prohibited fallback outcome"
                )
            reasons.append(f"provider_{status}")
    documents = []
    complete = True
    extractor = _pdf_extractor(store, context)
    for result in merged[: policy.max_documents]:
        url = result["canonicalUrl"]
        source: dict[str, Any] = {
            "runId": str(row["id"]),
            "requestedUrl": url,
            "resolvedUrl": url,
            "retrievedAt": None,
            "rawArtifactId": None,
            "rawSha256": None,
            "extractionArtifactId": None,
            "extractionSha256": None,
            "status": "not_fetched",
            "reasonCodes": [],
        }
        fetched = None
        try:
            policy.require_fetch_url(url)
            robots_sha = robots_request(policy, url).fingerprint
            robots = next((item for item in receipts if item["request_sha256"] == robots_sha), None)
            if robots is None:
                source["reasonCodes"].append("request_limit_reached")
                complete = False
            elif robots["response"]["response"]["status"] != "fetched":
                source["reasonCodes"].append("robots_unavailable")
            elif not robots_permits(_raw(store, conn, context, robots), url):
                source["reasonCodes"].append("robots_disallowed")
            else:
                fetch_sha = fetch_request(policy, url).fingerprint
                fetched = next(
                    (item for item in receipts if item["request_sha256"] == fetch_sha), None
                )
                if fetched is None:
                    source["reasonCodes"].append("request_limit_reached")
                    complete = False
                elif fetched["response"]["response"]["status"] != "fetched":
                    source["reasonCodes"].append(
                        "fetch_" + fetched["response"]["response"]["status"]
                    )
                    fetched = None
        except (ScopeDeniedError, InvalidInputError):
            source["reasonCodes"].append("fetch_policy_denied")
        if fetched is not None:
            raw = _raw(store, conn, context, fetched)
            response = fetched["response"]["response"]
            if retained is None:
                extraction = extract_document(
                    raw,
                    response["mediaType"] or "application/octet-stream",
                    pdf_extractor=extractor,
                )
                data = canonical_json(extraction).encode("utf-8")
                artifact = store.artifacts.put_in_transaction(
                    conn, context, data, "application/json"
                )
            else:
                matching = [item for item in retained["documents"] if item["requestedUrl"] == url]
                if len(matching) != 1 or matching[0]["extractionArtifactId"] is None:
                    raise StateMismatchError("Retained source extraction is absent or duplicated")
                artifact = store.artifacts._metadata(
                    conn, context, UUID(matching[0]["extractionArtifactId"])
                )
                if (
                    artifact["byte_length"] > 4 * 1024 * 1024
                    or artifact["digest"] != matching[0]["extractionSha256"]
                ):
                    raise StateMismatchError("Retained source extraction metadata changed")
                data = store.artifacts.read_in_transaction(conn, context, artifact["id"])
                extraction = json.loads(data)
                if extraction["mediaType"].split(";", 1)[0].strip().lower() == "application/pdf":
                    verify_retained_pdf(raw, extraction, pdf_extractor=extractor)
                else:
                    verify_extraction(raw, extraction)
                if canonical_json(extraction).encode() != data:
                    raise StateMismatchError("Retained extraction bytes are not canonical")
            extraction_sha = hashlib.sha256(data).hexdigest()
            execution = extraction.get("execution")
            if execution is not None:
                usage["tool_ms"] += execution["elapsedMs"]
            source.update(
                retrievedAt=response["retrievedAt"],
                rawArtifactId=fetched["response"]["rawArtifactId"],
                rawSha256=response["sourceSha256"],
                extractionArtifactId=str(artifact["id"]),
                extractionSha256=extraction_sha,
                status=extraction["status"],
                reasonCodes=extraction["reasonCodes"],
            )
        source["sourceId"] = content_hash(
            {
                "canonicalUrl": url,
                "rawSha256": source["rawSha256"],
                "extractionSha256": source["extractionSha256"],
            }
        )
        source["id"] = source["sourceId"]
        documents.append(source)
        reasons.extend(source["reasonCodes"])
    if not batches:
        reasons.append("no_successful_provider_results")
    return {
        "schema": BATCH_SCHEMA,
        "runId": str(row["id"]),
        "actionId": str(action.action_id),
        "actionIdentity": action.identity,
        "authorizationSha256": row["external_authorization"]["sha256"],
        "policySha256": policy.fingerprint,
        "consumerSha256": row["external_authorization"]["consumerSha256"],
        "requests": [
            {
                "id": str(item["id"]),
                "ordinal": item["ordinal"],
                "requestSha256": item["request_sha256"],
                "request": item["request"]["request"],
                "responseSha256": item["response_sha256"],
                "response": item["response"]["response"],
                "rawArtifactId": item["response"]["rawArtifactId"],
                "usageOrigin": item["response"]["usageOrigin"],
            }
            for item in receipts
        ],
        "searchResults": batches,
        "mergedResults": merged,
        "documents": documents,
        "usage": {**usage, "origin": origin},
        "complete": complete,
        "reasonCodes": sorted(set(reasons)),
        "visibility": "workspace",
        "typedTransformation": {
            "signature": EXTERNAL_RESEARCH_COG.to_dict(),
            "implementationSha256": row["external_authorization"]["consumerSha256"],
            "synActivation": "not_qualified",
        },
    }


def complete_external(
    store: ResearchRunStore,
    context: WorkspaceContext,
    lease: ResearchLease,
    policy: ExternalResearchPolicy,
    batch: dict[str, Any] | None = None,
    *,
    revalidate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    # Processing can invoke the explicitly configured supervised PDF tool. Its
    # start survives a publication rollback or worker loss; such work is never
    # silently replayed or settled as if only the network calls happened.
    with store.database.transaction(context) as conn:
        row = _lease(store, conn, context, lease)
        _authorization(store, conn, context, row, policy=policy, current=True)
        if revalidate is not None:
            revalidate()
        previous = conn.execute(
            "SELECT 1 FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
            "AND telemetry->>'phase'='processing_started' LIMIT 1",
            (context.workspace_id, row["id"]),
        ).fetchone()
        if previous is not None:
            raise StateMismatchError("An external research processing attempt cannot be replayed")
        _monitor(store, conn, context, row, "updated", "processing_started")
    with store.database.transaction(context) as conn:
        row = _lease(store, conn, context, lease)
        lock_episode(conn, context, row["episode_id"])
        action, _, authorization = _authorization(
            store, conn, context, row, policy=policy, current=True
        )
        receipts = _receipts(store, conn, context, row)
        result = _build_batch(store, conn, context, row, action, policy, receipts)
        if revalidate is not None:
            revalidate()
        if batch is not None and batch != result:
            raise StateMismatchError(
                "Supplied research result differs from retained source evidence"
            )
        outcome = external_research_outcome(
            action, result, run_id=row["id"], authorization_sha256=authorization["sha256"]
        )
        output = store.artifacts.put_in_transaction(
            conn, context, canonical_json(outcome.value.value).encode(), "application/json"
        )
        usage = {
            key: result["usage"][key]
            for key in ("calls", "tool_ms", "bytes_read", "money_microunits")
        }
        origin = result["usage"]["origin"]
        _, _, uncertain = _usage(receipts, policy)
        assert not uncertain
        BudgetLedger(store.database).settle(
            context, row["external_reservation_id"], usage, origin, connection=conn
        )
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "runId": str(row["id"]),
            "actionId": str(action.action_id),
            "actionIdentity": action.identity,
            "authorizationSha256": authorization["sha256"],
            "policySha256": policy.fingerprint,
            "consumerSha256": authorization["consumerSha256"],
            "outputArtifactId": str(output["id"]),
            "outputSha256": output["digest"],
            "requestIds": [str(item["id"]) for item in receipts],
            "requestSha256s": [item["request_sha256"] for item in receipts],
            "responseSha256s": [item["response_sha256"] for item in receipts],
            "usage": usage,
            "usageOrigin": origin,
            "typedTransformation": {
                **result["typedTransformation"],
                "receipt": outcome.receipt.to_dict(),
                "receiptSha256": outcome.receipt.fingerprint,
            },
            "needResolved": False,
            "grantsTaskCompletion": False,
        }
        signed = _signed(receipt, _token(store))
        digest = content_hash(signed)
        conn.execute(
            "INSERT INTO research_run_receipts(workspace_id,id,episode_id,run_id,result_artifact_id,receipt_digest,content) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                context.workspace_id,
                uuid4(),
                row["episode_id"],
                row["id"],
                output["id"],
                digest,
                Jsonb(signed),
            ),
        )
        append_event(
            conn,
            context,
            "external_research_observed",
            payload_id=output["id"],
            episode_id=row["episode_id"],
            producer_kind="tool",
            producer_id="potbot.external_research",
            causation_id=row["external_decision_id"],
            correlation_id=row["id"],
            details={
                "researchRunId": str(row["id"]),
                "receiptSha256": digest,
                "outputSha256": output["digest"],
                "authorizationSha256": authorization["sha256"],
            },
        )
        _monitor(
            store,
            conn,
            context,
            row,
            "completed",
            "research_completed",
            output=output,
            elapsed_ms=usage["tool_ms"],
            telemetry={"receiptSha256": digest, "usage": usage, "usageOrigin": origin},
        )
        updated = conn.execute(
            "UPDATE research_runs SET state='succeeded',revision=revision+1,lease_owner=NULL,lease_until=NULL,"
            "result_artifact_id=%s,receipt_digest=%s,finished_at=clock_timestamp(),updated_at=clock_timestamp() "
            "WHERE workspace_id=%s AND id=%s AND state='running' AND lease_owner=%s AND generation=%s "
            "AND lease_until>clock_timestamp() RETURNING *",
            (
                output["id"],
                digest,
                context.workspace_id,
                row["id"],
                lease.worker_id,
                lease.generation,
            ),
        ).fetchone()
        if updated is None:
            raise RevisionConflictError("External research lease expired before final publication")
        return _view(store, conn, context, dict(updated))


def _finish_negative(
    store: ResearchRunStore,
    conn: Any,
    context: WorkspaceContext,
    row: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    if row["state"] in _TERMINAL:
        return row
    count = conn.execute(
        "SELECT count(*) AS n FROM research_external_requests WHERE workspace_id=%s AND run_id=%s",
        (context.workspace_id, row["id"]),
    ).fetchone()["n"]
    ledger = BudgetLedger(store.database)
    status = conn.execute(
        "SELECT status FROM budget_reservations WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, row["external_reservation_id"]),
    ).fetchone()
    uncertain = bool(count)
    processing = (
        conn.execute(
            "SELECT 1 FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
            "AND telemetry->>'phase'='processing_started' LIMIT 1",
            (context.workspace_id, row["id"]),
        ).fetchone()
        is not None
    )
    if status is not None and status["status"] in {"reserved", "indeterminate"}:
        if not count and status["status"] == "reserved":
            ledger.release(
                context,
                row["external_reservation_id"],
                reason,
                confirmed_not_started=True,
                connection=conn,
            )
            uncertain = False
        else:
            try:
                policy = ExternalResearchPolicy.from_dict(row["external_authorization"]["policy"])
                if not hmac.compare_digest(
                    row["external_mac"],
                    authorization_mac(row["external_authorization"], _token(store)),
                ):
                    raise StateMismatchError("Research authorization changed")
                receipts = _receipts(store, conn, context, row)
                actual, origin, uncertain = _usage(receipts, policy)
                uncertain |= processing
                if not uncertain:
                    ledger.settle(
                        context, row["external_reservation_id"], actual, origin, connection=conn
                    )
            except (InvalidInputError, StateMismatchError, ScopeDeniedError, KeyError, TypeError):
                uncertain = True
            if uncertain:
                ledger.mark_indeterminate(
                    context,
                    row["external_reservation_id"],
                    "research_usage_unresolved",
                    connection=conn,
                )
    target = "indeterminate" if uncertain or reason == "lease_expired" else "blocked"
    safe_reason = "usage_indeterminate" if uncertain else reason
    _monitor(
        store,
        conn,
        context,
        row,
        "indeterminate"
        if target == "indeterminate"
        else ("cancelled" if reason == "research_cancelled" else "failed"),
        "research_stopped",
        telemetry={"reasonCode": safe_reason},
    )
    result = conn.execute(
        "UPDATE research_runs SET state=%s,error_code=%s,revision=revision+1,"
        "lease_owner=NULL,lease_until=NULL,finished_at=clock_timestamp(),updated_at=clock_timestamp() "
        "WHERE workspace_id=%s AND id=%s RETURNING *",
        (target, safe_reason, context.workspace_id, row["id"]),
    ).fetchone()
    return dict(result)


def fail_external(
    store: ResearchRunStore, context: WorkspaceContext, lease: ResearchLease, reason: str
) -> dict[str, Any]:
    if reason not in _ERRORS:
        raise InvalidInputError("External research failure requires a fixed safe reason")
    with store.database.transaction(context) as conn:
        row = _lease(store, conn, context, lease)
        return _view(store, conn, context, _finish_negative(store, conn, context, row, reason))


def cancel_external(
    store: ResearchRunStore, context: WorkspaceContext, run_id: UUID, key: str
) -> dict[str, Any]:
    bounded_key(key)
    with store.database.transaction(context) as conn:

        def cancel() -> dict[str, Any]:
            row = _external(conn, context, run_id, lock=True)
            if row["state"] == "queued":
                row = _finish_negative(store, conn, context, row, "research_cancelled")
            elif row["state"] == "running" and not row["cancel_requested"]:
                updated = conn.execute(
                    "UPDATE research_runs SET cancel_requested=true,revision=revision+1,updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s RETURNING *",
                    (context.workspace_id, run_id),
                ).fetchone()
                row = dict(updated)
                _monitor(store, conn, context, row, "updated", "cancellation_requested")
            return _view(store, conn, context, row)

        return idempotent_command(
            conn,
            context,
            "research.external.cancel",
            content_hash([str(context.principal_id), key]),
            {"runId": str(run_id)},
            cancel,
        )


def quarantine_external(store: ResearchRunStore, context: WorkspaceContext) -> int:
    with store.database.transaction(context) as conn:
        rows = conn.execute(
            "SELECT * FROM research_runs WHERE workspace_id=%s AND execution_kind='external' "
            "AND state='running' AND lease_until<=clock_timestamp() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 25",
            (context.workspace_id,),
        ).fetchall()
        for row in rows:
            _finish_negative(store, conn, context, dict(row), "lease_expired")
        return len(rows)


_AUTHORIZATION_FIELDS = (
    "schema",
    "workspaceId",
    "runId",
    "actionId",
    "actionIdentity",
    "actorPrincipalId",
    "policy",
    "policySha256",
    "consumerSha256",
    "pdfProfileSha256",
    "disclosureDecisionId",
    "disclosureSha256",
    "reservationId",
    "limits",
    "sourceArtifactId",
    "sourceRevision",
    "issuedAt",
    "visibility",
    "sha256",
)
_RECEIPT_FIELDS = (
    "schema",
    "runId",
    "actionId",
    "actionIdentity",
    "authorizationSha256",
    "policySha256",
    "consumerSha256",
    "outputArtifactId",
    "outputSha256",
    "requestIds",
    "requestSha256s",
    "responseSha256s",
    "usage",
    "usageOrigin",
    "typedTransformation",
    "needResolved",
    "grantsTaskCompletion",
)


def _view(
    store: ResearchRunStore, conn: Any, context: WorkspaceContext, row: dict[str, Any]
) -> dict[str, Any]:
    # Read/cancel remain available after token rotation, task staleness or damaged
    # positive evidence. A public history view is never a new send authorization.
    integrity = True
    try:
        action, _ = _action(conn, context, row["action_id"])
        action_value: dict[str, Any] | None = action.to_dict()
    except (InvalidInputError, StateMismatchError, ScopeDeniedError):
        action_value = None
        integrity = False
    value = row["external_authorization"]
    authorization = None
    try:
        if not isinstance(value, dict) or set(value) != set(_AUTHORIZATION_FIELDS):
            raise InvalidInputError("Research authorization projection has an invalid shape")
        if value["sha256"] != content_hash(
            {k: v for k, v in value.items() if k != "sha256"}
        ) or not hmac.compare_digest(row["external_mac"], authorization_mac(value, _token(store))):
            raise StateMismatchError("Research authorization signature is unavailable")
        ExternalResearchPolicy.from_dict(value["policy"])
        authorization = {name: value[name] for name in _AUTHORIZATION_FIELDS}
    except (InvalidInputError, StateMismatchError, ScopeDeniedError, KeyError, TypeError):
        integrity = False
    receipt = None
    output_sha = None
    if row["state"] == "succeeded":
        found = conn.execute(
            "SELECT content,receipt_digest FROM research_run_receipts WHERE workspace_id=%s AND run_id=%s",
            (context.workspace_id, row["id"]),
        ).fetchone()
        try:
            if (
                found is None
                or found["receipt_digest"] != row["receipt_digest"]
                or content_hash(found["content"]) != row["receipt_digest"]
                or set(found["content"]) != set(_RECEIPT_FIELDS) | {"mac"}
            ):
                raise StateMismatchError("Research completion projection is unavailable")
            body = _verify_signed(found["content"], _token(store))
            receipt = {name: body[name] for name in _RECEIPT_FIELDS}
            output_sha = receipt["outputSha256"]
        except (InvalidInputError, StateMismatchError, ScopeDeniedError, KeyError, TypeError):
            integrity = False
    reservation = conn.execute(
        "SELECT origin FROM budget_reservations WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, row["external_reservation_id"]),
    ).fetchone()
    count = conn.execute(
        "SELECT count(*) AS n FROM research_external_requests WHERE workspace_id=%s AND run_id=%s",
        (context.workspace_id, row["id"]),
    ).fetchone()["n"]
    run = {
        "id": str(row["id"]),
        "actionId": str(row["action_id"]),
        "episodeId": str(row["episode_id"]),
        "needId": action_value["scope"]["need_id"] if action_value else None,
        "needRevisionId": action_value["scope"]["need_revision_id"] if action_value else None,
        "state": row["state"],
        "revision": row["revision"],
        "createdAt": utc_text(row["created_at"]),
        "startedAt": utc_text(row["started_at"]) if row["started_at"] else None,
        "finishedAt": utc_text(row["finished_at"]) if row["finished_at"] else None,
        "errorCode": row["error_code"],
        "cancelRequested": row["cancel_requested"],
        "authorizationSha256": authorization["sha256"] if authorization else None,
        "policySha256": authorization["policySha256"] if authorization else None,
        "consumerSha256": authorization["consumerSha256"] if authorization else None,
        "disclosureDecisionId": str(row["external_decision_id"]),
        "reservationId": str(row["external_reservation_id"]),
        "resultArtifactId": str(row["result_artifact_id"]) if row["result_artifact_id"] else None,
        "resultSha256": output_sha,
        "receiptSha256": row["receipt_digest"],
        "requestCount": count,
        "usageOrigin": reservation["origin"] if reservation else None,
        "visibility": "workspace",
        "recordIntegrityValid": integrity,
    }
    return {
        "schema": RUN_SCHEMA,
        "scope": {
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        },
        "run": run,
        "action": action_value,
        "authorization": authorization,
        "receipt": receipt,
    }


def inspect_external(
    store: ResearchRunStore, context: WorkspaceContext, run_id: UUID
) -> dict[str, Any]:
    with store.database.transaction(context) as conn:
        return _view(store, conn, context, _external(conn, context, run_id))


def list_external(
    store: ResearchRunStore,
    context: WorkspaceContext,
    episode_id: UUID | None = None,
    *,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    if (
        type(limit) is not int
        or not 1 <= limit <= 25
        or (episode_id is not None and not isinstance(episode_id, UUID))
    ):
        raise InvalidInputError("Research history requires a task UUID and page limit 1..25")
    cursor = _parse_cursor(after)
    with store.database.transaction(context) as conn:
        clauses = ["workspace_id=%s", "execution_kind='external'"]
        parameters: list[Any] = [context.workspace_id]
        if episode_id is not None:
            clauses.append("episode_id=%s")
            parameters.append(episode_id)
        if cursor is not None:
            clauses.append("(created_at,id)<(%s,%s)")
            parameters.extend(cursor)
        parameters.append(limit + 1)
        rows = conn.execute(
            "SELECT * FROM research_runs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC,id DESC LIMIT %s",
            tuple(parameters),
        ).fetchall()
        items: list[dict[str, Any]] = []
        size = 0
        for row in rows[:limit]:
            item = _view(store, conn, context, dict(row))
            length = len(canonical_json(item).encode())
            if items and size + length > 1_000_000:
                break
            items.append(item)
            size += length
        more = len(rows) > len(items)
        return {
            "items": items,
            "nextCursor": _page_cursor(dict(rows[len(items) - 1])) if more and items else None,
        }


def events_external(
    store: ResearchRunStore,
    context: WorkspaceContext,
    run_id: UUID,
    *,
    limit: int = 25,
    after: int = 0,
) -> dict[str, Any]:
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(after) is not int
        or not 0 <= after <= 2**53 - 1
    ):
        raise InvalidInputError("Research events require a bounded integer page and cursor")
    with store.database.transaction(context) as conn:
        _external(conn, context, run_id)
        rows = conn.execute(
            "SELECT e.* FROM syn_execution_events e WHERE e.workspace_id=%s AND e.sequence>%s "
            "AND (e.run_id=%s OR EXISTS(SELECT 1 FROM research_external_requests q "
            "WHERE q.workspace_id=e.workspace_id AND q.run_id=%s AND q.id=e.run_id)) "
            "ORDER BY e.sequence LIMIT %s",
            (context.workspace_id, after, run_id, run_id, limit + 1),
        ).fetchall()
        items = [ExecutionEventService._public(dict(item)) for item in rows[:limit]]
        return {"items": items, "nextCursor": items[-1]["sequence"] if len(rows) > limit else None}


def audit_external(
    store: ResearchRunStore, context: WorkspaceContext, run_id: UUID
) -> dict[str, Any]:
    with store.database.transaction(context) as conn:
        row = _external(conn, context, run_id)
        if row["state"] != "succeeded" or row["result_artifact_id"] is None:
            raise StateMismatchError("External research has no completed immutable result")
        action, policy, authorization = _authorization(store, conn, context, row)
        retained = conn.execute(
            "SELECT * FROM research_run_receipts WHERE workspace_id=%s AND run_id=%s",
            (context.workspace_id, run_id),
        ).fetchone()
        if (
            retained is None
            or content_hash(retained["content"]) != row["receipt_digest"]
            or retained["receipt_digest"] != row["receipt_digest"]
        ):
            raise StateMismatchError("Research completion receipt changed")
        receipt = _verify_signed(retained["content"], _token(store))
        if (
            receipt["runId"] != str(run_id)
            or receipt["authorizationSha256"] != authorization["sha256"]
            or receipt["outputArtifactId"] != str(row["result_artifact_id"])
            or retained["result_artifact_id"] != row["result_artifact_id"]
        ):
            raise StateMismatchError("Research result receipt does not join its admitted run")
        metadata = store.artifacts._metadata(conn, context, row["result_artifact_id"])
        if (
            metadata["byte_length"] > 4 * 1024 * 1024
            or metadata["digest"] != receipt["outputSha256"]
        ):
            raise StateMismatchError("Research result artifact metadata changed")
        data = store.artifacts.read_in_transaction(conn, context, row["result_artifact_id"])
        try:
            batch = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise StateMismatchError("Research output is not a complete JSON batch") from error
        receipts = _receipts(store, conn, context, row)
        rebuilt = _build_batch(store, conn, context, row, action, policy, receipts, retained=batch)
        if rebuilt != batch or canonical_json(batch).encode() != data:
            raise StateMismatchError(
                "Research output differs from independently reconstructed source evidence"
            )
        outcome = external_research_outcome(
            action, batch, run_id=run_id, authorization_sha256=authorization["sha256"]
        )
        if (
            receipt["typedTransformation"]["receipt"] != outcome.receipt.to_dict()
            or receipt["typedTransformation"]["receiptSha256"] != outcome.receipt.fingerprint
        ):
            raise StateMismatchError("Research typed transformation receipt changed")
        _, _, uncertain = _usage(receipts, policy)
        usage = {
            key: rebuilt["usage"][key]
            for key in ("calls", "tool_ms", "bytes_read", "money_microunits")
        }
        origin = rebuilt["usage"]["origin"]
        settlement = conn.execute(
            "SELECT status,actual,origin FROM budget_reservations WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, row["external_reservation_id"]),
        ).fetchone()
        measured = conn.execute(
            "SELECT amounts,origin FROM budget_usage WHERE workspace_id=%s AND reservation_id=%s",
            (context.workspace_id, row["external_reservation_id"]),
        ).fetchall()
        if (
            uncertain
            or receipt["usage"] != usage
            or receipt["usageOrigin"] != origin
            or settlement is None
            or settlement["status"] != "settled"
            or settlement["actual"] != usage
            or settlement["origin"] != origin
            or len(measured) != 1
            or measured[0]["amounts"] != usage
            or measured[0]["origin"] != origin
        ):
            raise StateMismatchError("Research output lacks matching settled immutable usage")
        observed = conn.execute(
            "SELECT details FROM evidence_events WHERE workspace_id=%s AND episode_id=%s "
            "AND event_type='external_research_observed' AND payload_id=%s AND causation_id=%s "
            "AND producer_kind='tool' AND producer_id='potbot.external_research' AND correlation_id=%s",
            (
                context.workspace_id,
                row["episode_id"],
                row["result_artifact_id"],
                row["external_decision_id"],
                run_id,
            ),
        ).fetchall()
        expected = {
            "researchRunId": str(run_id),
            "receiptSha256": row["receipt_digest"],
            "outputSha256": receipt["outputSha256"],
            "authorizationSha256": authorization["sha256"],
        }
        if len(observed) != 1 or observed[0]["details"] != expected:
            raise StateMismatchError("Research source observation evidence is unavailable")
        monitoring = conn.execute(
            "SELECT * FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
            "AND telemetry->>'phase' IN ('research_started','processing_started','research_completed') "
            "ORDER BY sequence",
            (context.workspace_id, run_id),
        ).fetchall()
        if (
            len(monitoring) != 3
            or [item["telemetry"]["phase"] for item in monitoring]
            != ["research_started", "processing_started", "research_completed"]
            or [item["stage"] for item in monitoring] != ["started", "updated", "completed"]
            or any(
                item["subject_type_ref"] != EXTERNAL_RESEARCH_COG.cog_type_ref
                or item["trigger_input_sha256"] != row["action_identity"]
                for item in monitoring
            )
            or monitoring[1]["parent_event_id"] != monitoring[0]["id"]
            or monitoring[2]["parent_event_id"] != monitoring[1]["id"]
            or monitoring[2]["output_artifact_ref"] != f"artifact://{row['result_artifact_id']}"
            or monitoring[2]["output_sha256"] != receipt["outputSha256"]
            or monitoring[2]["elapsed_ms"] != usage["tool_ms"]
            or monitoring[2]["telemetry"].get("receiptSha256") != row["receipt_digest"]
            or monitoring[2]["telemetry"].get("usage") != usage
            or monitoring[2]["telemetry"].get("usageOrigin") != origin
        ):
            raise StateMismatchError(
                "Research lifecycle monitoring does not match its completion receipt"
            )
        return {
            **batch,
            "resultArtifactId": str(row["result_artifact_id"]),
            "resultSha256": receipt["outputSha256"],
            "receiptSha256": row["receipt_digest"],
            "receipt": receipt,
        }


def record_external_response(
    store: ResearchRunStore,
    context: WorkspaceContext,
    lease: ResearchLease,
    request: ExternalRequest,
    response: ExternalResponse,
    *,
    usage_origin: str = "measured",
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    if not isinstance(response, ExternalResponse) or response.request_sha256 != request.fingerprint:
        raise InvalidInputError("Research transport response differs from its prepared request")
    if usage_origin not in {"measured", "synthetic"}:
        raise InvalidInputError("Research transport usage must explicitly be measured or synthetic")
    if response.data is not None and len(response.data) > request.max_bytes:
        raise StateMismatchError("Research source exceeds its admitted byte ceiling")
    with store.database.transaction(context) as conn:
        row = _lease(store, conn, context, lease)
        receipts = _receipts(store, conn, context, row)
        found = next(
            (item for item in receipts if item["request_sha256"] == request.fingerprint), None
        )
        if found is None or found["response"] is not None:
            raise StateMismatchError("Research response requires exactly one unfinished dispatch")
        # Retain confirmed transport evidence even if task/cancellation authority
        # changed during the call. That authority still blocks every next send.
        raw = (
            store.artifacts.put_in_transaction(
                conn, context, response.data, response.media_type or "application/octet-stream"
            )
            if response.data is not None
            else None
        )
        body = {
            "schema": "potbot-research-response-receipt/v1",
            "requestId": str(found["id"]),
            "authorizationSha256": row["external_authorization"]["sha256"],
            "response": response.to_dict(),
            "rawArtifactId": str(raw["id"]) if raw else None,
            "usageOrigin": usage_origin,
        }
        signed = _signed(body, _token(store))
        conn.execute(
            "INSERT INTO research_external_responses(workspace_id,id,episode_id,run_id,request_id,"
            "raw_artifact_id,response_sha256,content) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                context.workspace_id,
                uuid4(),
                row["episode_id"],
                row["id"],
                found["id"],
                raw["id"] if raw else None,
                content_hash(signed),
                Jsonb(signed),
            ),
        )
        stage = (
            "completed"
            if response.status == "fetched"
            else (
                "indeterminate" if response.status in {"timed_out", "indeterminate"} else "failed"
            )
        )
        _monitor(
            store,
            conn,
            context,
            row,
            stage,
            "request_observed",
            child_id=found["id"],
            parent_id=found["event_id"],
            parent_run=found["id"],
            output=raw,
            elapsed_ms=response.elapsed_ms,
            telemetry={
                "requestSha256": request.fingerprint,
                "status": response.status,
                "receivedBytes": response.received_bytes,
                "usageOrigin": usage_origin,
            },
        )
        conn.execute(
            "UPDATE research_runs SET revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, row["id"]),
        )
        return body
