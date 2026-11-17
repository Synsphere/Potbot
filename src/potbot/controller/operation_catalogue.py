"""Read-only operation observations joined from existing authorities.

This catalogue neither installs nor authorizes anything. In particular, an
imported Cog, a context observation, a grant, an S12 release row or recorded
model qualification is not current runtime admission. No host, network, vault,
lease-expiry or discovery action is performed while producing this projection.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash

if TYPE_CHECKING:
    from potbot.persistence.database import Database

SCHEMA = "potbot-operation-catalogue/v1"
KINDS = ("tool", "cog", "workflow", "connector", "model", "skill", "gateway")
STATES = (
    "source_only",
    "installed_unqualified",
    "policy_disabled",
    "qualified",
    "degraded",
    "indeterminate",
)
SOURCE_FAMILIES = (
    "runtime_context",
    "tool_history",
    "shared_tool_upgrade",
    "model_catalog",
    "model_deployment",
    "published_skill",
    "syn_snapshot",
)
MAX_SOURCE_ROWS = 2048
MAX_RECENT = 5
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_LABEL = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,249}\Z")
_SYN = re.compile(r"syn(?:ny)?://cog/[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
_THING = re.compile(r"syn(?:ny)?://thing/[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
_SHARED = re.compile(r"shared-tool:[a-z][a-z0-9-]{1,62}\Z")
_UNIT = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}\Z")
_ORIGINS = {"measured", "synthetic", "provider_reported", "conservative_estimate"}
_RELEASE_STATES = {"none", "repinned", "testing", "failed", "indeterminate", "released"}
_UPGRADE_STATES = {
    "awaiting_approval",
    "approved",
    "rejected",
    "executing",
    "awaiting_repin",
    "active",
    "indeterminate",
    "rolled_back",
}

# One SELECT observes all participating owners at one PostgreSQL statement
# snapshot. LIMIT+1 makes overflow explicit rather than silently returning a
# complete-looking partial inventory. No payloads, source text, prompts, argv,
# credentials, configuration paths or deployment configuration are selected.
_READ = """WITH
runtime_heads AS (
 SELECT DISTINCT ON (r.object_id) r.object_id AS id,r.kind,r.reference,r.fingerprint,
        r.revision,r.state
 FROM workspace_context_runtime_sources r
 JOIN workspace_context_objects o ON o.workspace_id=r.workspace_id AND o.id=r.object_id
 WHERE r.workspace_id=%(workspace)s AND r.owner_principal_id=%(principal)s
   AND o.visibility='private' AND o.owner_principal_id=%(principal)s
   AND r.kind IN ('tool','workflow','connector','model','gateway')
 ORDER BY r.object_id,r.revision DESC
), runtime AS (
 SELECT * FROM runtime_heads WHERE state='active' ORDER BY id LIMIT %(cap)s
), tool_history AS (
 SELECT s.object_id AS id,s.source_key AS reference
 FROM workspace_context_sources s JOIN workspace_context_objects o
 ON o.workspace_id=s.workspace_id AND o.id=s.object_id
 WHERE s.workspace_id=%(workspace)s AND s.source_family='tool' AND s.state='active'
 AND (o.visibility='shared' OR o.owner_principal_id=%(principal)s)
 ORDER BY s.object_id LIMIT %(cap)s
), upgrade_heads AS (
 SELECT id,slug,agent_id,old_commit,new_commit,revision,state,release_phase,
        release_generation,evidence_sha256,suite_sha256,release_binding,release_evidence,
        created_at,updated_at,row_number() OVER (PARTITION BY slug,
        CASE WHEN state IN ('executing','awaiting_repin','active','indeterminate','rolled_back')
                    OR release_phase!='none' THEN 'activation'
             WHEN state IN ('awaiting_approval','approved') THEN 'proposal'
             ELSE 'rejected' END ORDER BY created_at DESC,id DESC) AS rank
 FROM ui_tool_upgrade_approvals
 WHERE workspace_id=%(workspace)s AND principal_id=%(principal)s
), upgrades AS (
 SELECT * FROM upgrade_heads WHERE rank=1
 ORDER BY created_at DESC,id LIMIT %(cap)s
), models AS (
 SELECT id,canonical_model,role FROM model_catalog
 WHERE workspace_id=%(workspace)s ORDER BY id LIMIT %(cap)s
), deployments AS (
 SELECT DISTINCT ON (d.canonical_model) d.id,d.canonical_model,d.content_hash,
        h.health,q.qualified,q.qualification_event_id
 FROM model_deployments d
 LEFT JOIN deployment_health h ON h.workspace_id=d.workspace_id AND h.deployment_id=d.id
 LEFT JOIN deployment_qualifications q ON q.workspace_id=d.workspace_id AND q.deployment_id=d.id
 WHERE d.workspace_id=%(workspace)s ORDER BY d.canonical_model,d.created_at DESC,d.id
 LIMIT %(cap)s
), skills AS (
 SELECT k.id,k.slug,k.owner_agent_id,k.context_object_id,r.source_revision,k.latest_sha256
 FROM workspace_context_skills k JOIN workspace_context_objects o
 ON o.workspace_id=k.workspace_id AND o.id=k.context_object_id
 JOIN workspace_context_skill_releases r ON r.workspace_id=k.workspace_id
 AND r.skill_id=k.id AND r.sha256=k.latest_sha256
 WHERE k.workspace_id=%(workspace)s AND k.published AND k.state='active'
 AND (o.visibility='shared' OR o.owner_principal_id=%(principal)s)
 ORDER BY k.id LIMIT %(cap)s
), syn AS (
 SELECT DISTINCT ON (s.namespace_id,o.ref) o.ref,s.id,s.source_hash,s.namespace_id
 FROM syn_objects o JOIN syn_snapshots s
 ON s.workspace_id=o.workspace_id AND s.id=o.snapshot_id
 JOIN syn_snapshot_seals z ON z.workspace_id=s.workspace_id AND z.snapshot_id=s.id
 WHERE o.workspace_id=%(workspace)s AND o.kind='cog'
 ORDER BY s.namespace_id,o.ref,s.created_at DESC,s.id LIMIT %(cap)s
), owned_jobs AS (
 SELECT j.* FROM jobs j WHERE j.workspace_id=%(workspace)s AND EXISTS (
 SELECT 1 FROM command_results c WHERE c.workspace_id=j.workspace_id
 AND c.principal_id=%(principal)s AND c.operation='submit_api_job'
 AND c.result->>'id'=j.id::text)
), cog_events AS (
 SELECT DISTINCT ON (e.run_id) e.run_id AS id,
 COALESCE(e.telemetry->'implementation'->>'operation_ref',e.telemetry->>'cog_ref') AS reference,
 e.stage AS state,e.recorded_at,NULL::uuid AS reservation_id,'cog'::text AS source,
 CASE WHEN e.telemetry->'implementation'->>'kind' IN ('workflow','mcp_workflow') THEN 'workflow'
      WHEN e.telemetry->'implementation'->>'kind' IN ('api','mcp_server','mcp_session') THEN 'connector'
      WHEN e.telemetry->'implementation'->>'kind'='model' THEN 'model'
      WHEN e.telemetry->'implementation'->>'kind' IS NULL THEN 'cog' ELSE 'tool' END AS kind
 FROM syn_execution_events e WHERE e.workspace_id=%(workspace)s
 AND e.emitter_principal_id=%(principal)s ORDER BY e.run_id,e.sequence DESC
), history AS (
 SELECT id,reference,state,recorded_at,reservation_id,source,kind FROM cog_events
 UNION ALL
 SELECT j.id,j.input->>'operation_ref',j.state,j.updated_at,NULL::uuid,'cog','tool'
 FROM owned_jobs j WHERE j.job_type='cog.execute'
 UNION ALL
 SELECT i.id,r.request->'generation'->'selection'->>'model',i.status,i.updated_at,i.reservation_id,'hosted','model'
 FROM hosted_invocations i JOIN hosted_requests r ON r.workspace_id=i.workspace_id AND r.id=i.id
 JOIN owned_jobs j ON j.workspace_id=r.workspace_id AND j.id=r.job_id
 WHERE i.workspace_id=%(workspace)s
 UNION ALL
 SELECT i.id,i.model,i.status,i.updated_at,i.reservation_id,'reasoning','model'
 FROM reasoning_budget_invocations i JOIN reasoning_runtime_runs r
 ON r.workspace_id=i.workspace_id AND r.id=i.run_id
 WHERE i.workspace_id=%(workspace)s AND r.principal_id=%(principal)s
), unique_history AS (
 SELECT DISTINCT ON (source,id) * FROM history
 WHERE reference IS NOT NULL ORDER BY source,id,recorded_at DESC
), ranked_history AS (
 SELECT *,row_number() OVER (PARTITION BY kind,reference
 ORDER BY recorded_at DESC,id) AS rank FROM unique_history
), recent AS (
 SELECT id,reference,state,recorded_at,reservation_id,source,kind FROM ranked_history
 WHERE rank<=5 ORDER BY recorded_at DESC,id LIMIT %(cap)s
), accounting AS (
 SELECT r.id,r.status,u.amounts,u.origin FROM budget_reservations r
 LEFT JOIN budget_usage u ON u.workspace_id=r.workspace_id AND u.reservation_id=r.id
 WHERE r.workspace_id=%(workspace)s AND r.id IN (
 SELECT reservation_id FROM recent WHERE reservation_id IS NOT NULL)
 ORDER BY r.id LIMIT %(cap)s
)
SELECT jsonb_build_object(
 'runtime',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM runtime t),'[]'::jsonb),
 'tool_history',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM tool_history t),'[]'::jsonb),
 'upgrades',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM upgrades t),'[]'::jsonb),
 'models',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM models t),'[]'::jsonb),
 'deployments',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM deployments t),'[]'::jsonb),
 'skills',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM skills t),'[]'::jsonb),
 'syn',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM syn t),'[]'::jsonb),
 'recent',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM recent t),'[]'::jsonb),
 'accounting',COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM accounting t),'[]'::jsonb)
) AS observations
"""


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise StateMismatchError("Catalogue owner returned an invalid record identity") from None


def _safe_integer(value: Any, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise StateMismatchError("Catalogue revision is outside the public integer bound")
    if nullable and value == 0:
        return None
    return value


def _reference(value: Any, kind: str, record_id: str) -> tuple[str, str, bool]:
    """Only established identifier syntaxes may become searchable public text."""
    if (
        kind == "model"
        and isinstance(value, str)
        and _MODEL.fullmatch(value)
        and (
            "://" not in value and ".." not in value and not value.startswith(("secret:", "file:"))
        )
    ):
        return "model:" + value, value, True
    if (
        isinstance(value, str)
        and len(value) <= 512
        and (
            _SYN.fullmatch(value)
            or _THING.fullmatch(value)
            or _SHARED.fullmatch(value)
            or _LABEL.fullmatch(value)
            or re.fullmatch(r"potbot\.[a-z0-9_.-]+/v[0-9]+", value)
        )
    ):
        return value.replace("synny://", "syn://"), value.rsplit("/", 1)[-1], True
    return "opaque:" + content_hash([kind, record_id]), "Unrecognized operation", False


def _empty_accounting() -> dict[str, Any]:
    return {
        "scope": "linked_recent_invocations",
        "complete": False,
        "reservationIds": [],
        "settledCount": 0,
        "indeterminateCount": 0,
        "totals": [],
    }


def _accounting(
    invocations: list[dict[str, Any]], rows: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    result = _empty_accounting()
    identities = sorted({row["reservationId"] for row in invocations if row["reservationId"]})
    result["reservationIds"] = identities
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for identity in identities:
        row = rows.get(identity)
        if row is None:
            continue  # Missing observation is unavailable, never a zero charge.
        result["settledCount"] += row["status"] == "settled"
        result["indeterminateCount"] += row["status"] == "indeterminate"
        amounts, origin = row["amounts"], row["origin"]
        if amounts is None:
            continue
        if not isinstance(amounts, dict) or len(amounts) > 32 or origin not in _ORIGINS:
            raise StateMismatchError("Catalogue usage observation is malformed")
        for unit, value in amounts.items():
            if (
                not isinstance(unit, str)
                or not _UNIT.fullmatch(unit)
                or type(value) is not int
                or not 0 <= value <= 2**53 - 1
            ):
                raise StateMismatchError("Catalogue usage has an invalid dimension")
            if unit != "concurrency":
                totals[(unit, origin)] += value
    if len(totals) > 32:
        raise CapabilityUnavailableError("Catalogue linked usage exceeds its dimension bound")
    result["totals"] = [
        {"unit": unit, "value": str(value), "provenance": origin}
        for (unit, origin), value in sorted(totals.items())
    ]
    return result


def project_observations(
    context: WorkspaceContext, observations: dict[str, Any]
) -> list[dict[str, Any]]:
    """Pure bounded projection, independently testable without runtime authority."""
    families = {
        "runtime",
        "tool_history",
        "upgrades",
        "models",
        "deployments",
        "skills",
        "syn",
        "recent",
        "accounting",
    }
    if (
        not isinstance(observations, dict)
        or set(observations) != families
        or any(
            not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            for rows in observations.values()
        )
    ):
        raise StateMismatchError("Catalogue observations are incomplete")
    if (
        any(len(rows) > MAX_SOURCE_ROWS for rows in observations.values())
        or sum(len(observations[key]) for key in families - {"recent", "accounting"})
        > MAX_SOURCE_ROWS
    ):
        raise CapabilityUnavailableError(
            "Catalogue exceeds its bounded source scan; no partial result"
        )
    items: dict[tuple[str, str], dict[str, Any]] = {}

    def item(kind: str, reference: str, name: str, *, valid: bool = True) -> dict[str, Any]:
        if _SYN.fullmatch(reference):
            kind = "cog"
        if kind not in KINDS:
            kind = "tool"
        key = kind, reference
        if key not in items:
            items[key] = {
                "id": content_hash(
                    {"workspace": str(context.workspace_id), "kind": kind, "reference": reference}
                ),
                "kind": kind,
                "reference": reference,
                "name": name[:256],
                "state": "source_only",
                "reasons": ["source_metadata_only"],
                "contextObjectId": None,
                "version": None,
                "fingerprintSha256": None,
                "updateAvailable": None,
                "sources": [],
                "release": None,
                "recentInvocations": [],
                "accounting": _empty_accounting(),
            }
        selected = items[key]
        if not valid and "unsupported_reference" not in selected["reasons"]:
            selected["reasons"].append("unsupported_reference")
        return selected

    def source(
        target: dict[str, Any], family: str, record_id: Any, revision: Any = None, sha: Any = None
    ) -> None:
        record = {
            "family": family,
            "recordId": _uuid(record_id),
            "revision": _safe_integer(revision) if revision is not None else None,
            "sha256": _digest(sha),
        }
        if record not in target["sources"]:
            target["sources"].append(record)

    for row in observations["tool_history"]:
        ref, name, valid = _reference(row["reference"], "tool", str(row["id"]))
        selected = item("tool", ref, name, valid=valid)
        selected["contextObjectId"] = _uuid(row["id"])
        source(selected, "tool_history", row["id"])
    for row in observations["syn"]:
        ref, name, valid = _reference(row["ref"], "cog", str(row["id"]))
        source(
            item("cog", ref, name, valid=valid), "syn_snapshot", row["id"], sha=row["source_hash"]
        )
    for row in observations["models"]:
        ref, name, valid = _reference(row["canonical_model"], "model", str(row["id"]))
        source(item("model", ref, name, valid=valid), "model_catalog", row["id"])
    for row in observations["deployments"]:
        ref, name, valid = _reference(row["canonical_model"], "model", str(row["id"]))
        selected = item("model", ref, name, valid=valid)
        selected["fingerprintSha256"] = _digest(row["content_hash"])
        selected["version"] = selected["fingerprintSha256"]
        selected["reasons"].extend(
            ["model_deployment_record_only", "qualification_not_established"]
        )
        health = row.get("health") or "unknown"
        if health in {"degraded", "unavailable"}:
            selected["state"] = "degraded"
        if health in {"unknown", "degraded", "unavailable"}:
            selected["reasons"].append("model_health_" + health)
        if row.get("qualified"):
            selected["reasons"].append("model_qualification_record_only")
        source(selected, "model_deployment", row["id"], sha=row["content_hash"])
    for row in observations["skills"]:
        identity = _uuid(row["id"])
        ref = "skill:" + identity
        name = (
            row["slug"]
            if isinstance(row["slug"], str) and _LABEL.fullmatch(row["slug"])
            else "Published skill"
        )
        selected = item("skill", ref, name)
        selected["contextObjectId"] = _uuid(row["context_object_id"])
        selected["fingerprintSha256"] = _digest(row["latest_sha256"])
        selected["version"] = selected["fingerprintSha256"]
        selected["reasons"].append("skill_is_untrusted_data")
        source(selected, "published_skill", identity, row["source_revision"], row["latest_sha256"])
    for row in observations["runtime"]:
        ref, name, valid = _reference(row["reference"], row["kind"], str(row["id"]))
        selected = item(row["kind"], ref, name, valid=valid)
        if selected["state"] == "source_only":
            selected["state"] = "installed_unqualified"
        selected["reasons"] = [
            reason for reason in selected["reasons"] if reason != "source_metadata_only"
        ] + [
            "runtime_observation_not_live",
            "qualification_not_established",
            "execution_policy_not_evaluated",
        ]
        if not valid:
            selected["reasons"].append("unsupported_reference")
        selected["contextObjectId"] = _uuid(row["id"])
        selected["fingerprintSha256"] = _digest(row["fingerprint"])
        selected["version"] = str(_safe_integer(row["revision"]))
        source(selected, "runtime_context", row["id"], row["revision"], row["fingerprint"])

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations["upgrades"]:
        if not isinstance(row["slug"], str) or not _SHARED.fullmatch("shared-tool:" + row["slug"]):
            raise StateMismatchError("Catalogue upgrade has an invalid tool slug")
        grouped[row["slug"]].append(row)
    for slug, rows in grouped.items():
        selected = item("tool", "shared-tool:" + slug, slug)
        rows.sort(key=lambda row: (row["created_at"], str(row["id"])), reverse=True)
        activated = [
            row
            for row in rows
            if row["state"]
            in {"executing", "awaiting_repin", "active", "indeterminate", "rolled_back"}
            or row["release_phase"] != "none"
        ]
        current = activated[0] if activated else rows[0]
        state, phase = current["state"], current["release_phase"]
        if state not in _UPGRADE_STATES or phase not in _RELEASE_STATES:
            raise StateMismatchError("Catalogue upgrade state is not recognized")
        binding, evidence = current.get("release_binding"), current.get("release_evidence")
        generation = _safe_integer(current["release_generation"], nullable=True)
        if phase != "none" and generation is None:
            raise StateMismatchError("Catalogue release generation is missing")
        selected["release"] = {
            "operationId": _uuid(current["id"]),
            "state": state,
            "releaseState": phase,
            "generation": generation,
            "bindingSha256": content_hash(binding) if isinstance(binding, dict) else None,
            "evidenceSha256": content_hash(evidence) if isinstance(evidence, dict) else None,
        }
        selected["reasons"].append("release_record_not_current_admission")
        for row in rows:
            source(
                selected, "shared_tool_upgrade", row["id"], row["revision"], row["evidence_sha256"]
            )
        selected["updateAvailable"] = (
            True
            if any(
                row["state"] in {"awaiting_approval", "approved"}
                and row["release_phase"] == "none"
                and (not activated or row["created_at"] > current["created_at"])
                for row in rows
            )
            else None
        )
        reason = {
            "awaiting_approval": "approval_required",
            "approved": "upgrade_approved_not_activated",
            "rejected": "upgrade_rejected",
            "executing": "activation_in_progress",
            "awaiting_repin": "runtime_repin_required",
            "indeterminate": "release_indeterminate",
        }.get(state)
        if reason:
            selected["reasons"].append(reason)
        if phase == "indeterminate" or state == "indeterminate":
            selected["state"] = "indeterminate"
        elif phase == "failed":
            selected["state"] = "degraded"
        elif state in {"executing", "awaiting_repin"} or phase in {"repinned", "testing"}:
            selected["state"] = "policy_disabled"
        if phase in {"repinned", "testing", "failed"}:
            selected["reasons"].append(
                {
                    "repinned": "postflight_required",
                    "testing": "postflight_running",
                    "failed": "postflight_failed",
                }[phase]
            )

    recent: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations["recent"]:
        kind = row.get("kind", "cog" if row["source"] == "cog" else "model")
        ref, _, valid = _reference(row["reference"], kind, str(row["id"]))
        if _SYN.fullmatch(ref):
            kind = "cog"
        if not valid or row["source"] not in {"cog", "hosted", "reasoning"}:
            continue
        state = row["state"]
        recorded = row["recorded_at"]
        if (
            not isinstance(state, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", state)
            or not isinstance(recorded, str)
            or len(recorded) > 64
        ):
            raise StateMismatchError("Catalogue invocation observation is malformed")
        try:
            timestamp = datetime.fromisoformat(recorded)
            if timestamp.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise StateMismatchError("Catalogue invocation timestamp is invalid") from None
        recent[(kind, ref)].append(
            {
                "id": _uuid(row["id"]),
                "source": row["source"],
                "state": state,
                "recordedAt": recorded,
                "reservationId": _uuid(row["reservation_id"]) if row["reservation_id"] else None,
            }
        )
    accounting = {_uuid(row["id"]): row for row in observations["accounting"]}
    for selected in items.values():
        history = sorted(
            recent.get((selected["kind"], selected["reference"]), []),
            key=lambda row: (row["recordedAt"], row["id"]),
            reverse=True,
        )
        seen: set[tuple[str, str]] = set()
        for row in history:
            invocation_key = row["source"], row["id"]
            if invocation_key not in seen:
                selected["recentInvocations"].append(row)
                seen.add(invocation_key)
            if len(selected["recentInvocations"]) >= MAX_RECENT:
                break
        selected["accounting"] = _accounting(selected["recentInvocations"], accounting)
        selected["reasons"].append("accounting_scope_limited")
        if any(row["state"] == "indeterminate" for row in selected["recentInvocations"]):
            selected["reasons"].append("invocation_indeterminate")
        selected["reasons"] = sorted(set(selected["reasons"]))
        selected["sources"].sort(
            key=lambda row: (row["family"], row["recordId"], row["revision"] or 0)
        )
        if len(selected["sources"]) > 32 or len(selected["reasons"]) > 32:
            raise CapabilityUnavailableError("Catalogue operation exceeds its provenance bound")
    return sorted(items.values(), key=lambda row: (row["kind"], row["name"].casefold(), row["id"]))


class OperationCatalogue:
    """A bounded read model; existing stores remain the only durable owners."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def _snapshot(self, context: WorkspaceContext) -> list[dict[str, Any]]:
        if not isinstance(context, WorkspaceContext):
            raise ScopeDeniedError("Catalogue requires an authenticated workspace principal")
        with self.database.transaction(context) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                _READ,
                {
                    "workspace": context.workspace_id,
                    "principal": context.principal_id,
                    "cap": MAX_SOURCE_ROWS + 1,
                },
            ).fetchone()
        if row is None:
            raise StateMismatchError("Catalogue source observation is unavailable")
        return project_observations(context, row["observations"])

    @staticmethod
    def _envelope(context: WorkspaceContext, items: list[dict[str, Any]]) -> dict[str, Any]:
        scope = {
            "schema": SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
        }
        return {**scope, "snapshotSha256": content_hash({**scope, "items": items})}

    def list(
        self,
        context: WorkspaceContext,
        *,
        query: str = "",
        kind: str | None = None,
        state: str | None = None,
        after: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str)
            or len(query) > 160
            or any(ord(c) < 32 for c in query)
            or (kind is not None and kind not in KINDS)
            or (state is not None and state not in STATES)
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise InvalidInputError("Invalid operation catalogue filters")
        query = query.strip().casefold()
        snapshot = self._snapshot(context)
        envelope = self._envelope(context, snapshot)
        items = [
            row
            for row in snapshot
            if (kind is None or row["kind"] == kind)
            and (state is None or row["state"] == state)
            and (not query or query in (row["name"] + " " + row["reference"]).casefold())
        ]
        filters = {"query": query, "kind": kind, "state": state}
        start = 0
        if after is not None:
            if not isinstance(after, str) or not 1 <= len(after) <= 4096:
                raise InvalidInputError("Invalid operation catalogue cursor")
            try:
                raw = base64.b64decode(
                    after + "=" * (-len(after) % 4), altchars=b"-_", validate=True
                )
                cursor = json.loads(raw)
                if (
                    not isinstance(cursor, dict)
                    or set(cursor) != {"version", "scope", "filters", "snapshot", "after"}
                    or type(cursor["version"]) is not int
                    or cursor["version"] != 1
                ):
                    raise ValueError
                if canonical_json(cursor).encode() != raw:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
                raise InvalidInputError("Invalid operation catalogue cursor") from None
            scope = content_hash([str(context.workspace_id), str(context.principal_id)])
            if cursor["scope"] != scope or cursor["filters"] != filters:
                raise RevisionConflictError(
                    "Catalogue cursor belongs to a different scope or filter"
                )
            if cursor["snapshot"] != envelope["snapshotSha256"]:
                raise RevisionConflictError("Operation catalogue changed; refresh the listing")
            positions = [i for i, row in enumerate(items) if row["id"] == cursor["after"]]
            if len(positions) != 1:
                raise InvalidInputError("Catalogue cursor position is unavailable")
            start = positions[0] + 1
        page = items[start : start + limit]
        next_cursor = None
        if start + limit < len(items) and page:
            cursor = {
                "version": 1,
                "scope": content_hash([str(context.workspace_id), str(context.principal_id)]),
                "filters": filters,
                "snapshot": envelope["snapshotSha256"],
                "after": page[-1]["id"],
            }
            next_cursor = (
                base64.urlsafe_b64encode(canonical_json(cursor).encode()).rstrip(b"=").decode()
            )
        return {**envelope, "items": page, "nextCursor": next_cursor}

    def inspect(self, context: WorkspaceContext, operation_id: str) -> dict[str, Any]:
        if not isinstance(operation_id, str) or not _SHA.fullmatch(operation_id):
            raise InvalidInputError("Operation catalogue identity must be a SHA-256 identifier")
        items = self._snapshot(context)
        selected = next((row for row in items if row["id"] == operation_id), None)
        if selected is None:
            raise ScopeDeniedError("Operation is unavailable to this workspace principal")
        return {**self._envelope(context, items), "item": selected}
