"""Bounded read-only joins of existing capability owners for task discovery.

No source is installed, refreshed, scheduled or invoked here. Registry approval,
operation state, configured MCP references and qualification reports remain
owned by their existing services. Descriptions are summaries; exact owner hashes
bind the full records. Missing typed metadata remains missing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from potbot.context.service import native_skill_setup_token
from potbot.context.skill_acquisition import SkillAcquisition
from potbot.controller.capability_discovery import MAX_SOURCES, DiscoveryService
from potbot.controller.discovery_installed import installed_skill_preference
from potbot.controller.mcp_discovery import MAX_BINDINGS, McpDiscoveryService
from potbot.controller.mcp_qualification import MAX_SUITES, McpQualificationService
from potbot.controller.operation_catalogue import OperationCatalogue
from potbot.controller.operation_discovery_metadata import OperationDiscoveryMetadata
from potbot.controller.task_discovery_ranking import (
    MAX_TASK_CANDIDATES,
    validate_task_candidates,
)
from potbot.domain.auth import WorkspaceContext
from potbot.domain.discovery import RegistryCandidate, bounded_json, digest_value
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database
from potbot.semantics.catalog import catalog_fingerprint

MAX_INSPECTION_BYTES = 128 * 1024
_TASK_FIELDS = frozenset(
    {
        "episodeId",
        "contractRevisionId",
        "contractSha256",
        "repositoryStateId",
        "repositorySha256",
        "semanticReleaseId",
        "semanticReleaseSha256",
    }
)
_UNKNOWN_CAPABILITIES: dict[str, list[str]] = {
    "classes": [],
    "inputTypes": [],
    "outputTypes": [],
    "effects": [],
    "scopes": [],
    "platforms": [],
}
_UNKNOWN_REASONS = [
    "canonical_ports_unknown",
    "canonical_scopes_unknown",
    "effects_unknown",
    "task_capability_unknown",
    "task_evidence_unavailable",
]
_SOURCE_FIELDS = (
    "id",
    "revision",
    "state",
    "configSha256",
    "bindingSha256",
    "approvalValid",
    "snapshotId",
    "snapshotSha256",
    "lastErrorCode",
)
_PUBLISHER_FIELDS = (
    "id",
    "sourceId",
    "revision",
    "state",
    "bindingSha256",
    "approvalValid",
)
_REGISTRY_FIELDS = (
    "id",
    "sourceId",
    "snapshotId",
    "candidateSha256",
    "sourceState",
    "publisherId",
    "publisherState",
    "sourceApprovalValid",
    "publisherApprovalValid",
    "stale",
)
_BINDING_FIELDS = (
    "id",
    "agentId",
    "revision",
    "state",
    "bindingSha256",
    "approvalValid",
    "installationSha256",
    "configurationSha256",
    "definitionSha256",
    "toolName",
    "toolFingerprint",
    "transport",
    "snapshotId",
    "snapshotSha256",
    "stale",
    "lastErrorCode",
    "activeAttemptId",
)
_ENTRY_FIELDS = (
    "id",
    "bindingId",
    "snapshotId",
    "snapshotSha256",
    "kind",
    "identity",
    "definitionSha256",
    "configuredTool",
    "referenceAvailable",
)
_OPERATION_FIELDS = (
    "id",
    "kind",
    "reference",
    "name",
    "state",
    "contextObjectId",
    "version",
    "fingerprintSha256",
    "updateAvailable",
    "sources",
    "release",
)
_SKILL_FIELDS = (
    "id",
    "origin",
    "ownerPrincipalId",
    "owned",
    "contextObjectId",
    "revision",
    "state",
    "enabled",
    "approvalValid",
    "releaseSha256",
    "contentSha256",
    "packageId",
    "sourceSha256",
)
_ACCOUNTING_REASONS = frozenset({"accounting_scope_limited", "invocation_indeterminate"})


def _pick(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in fields}


def _task_binding(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _TASK_FIELDS:
        raise InvalidInputError("Task discovery requires the exact seven-field task binding")
    for key, selected in value.items():
        if key.endswith("Sha256"):
            digest_value(selected, "Task binding digest")
        else:
            try:
                if not isinstance(selected, str) or str(UUID(selected)) != selected:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise InvalidInputError("Task binding identities require canonical UUIDs") from None
    return dict(value)


def _identity(context: WorkspaceContext, kind: str, owner: str, binding: str | None) -> str:
    return content_hash(
        {
            "schema": "potbot-task-discovery-candidate/v1",
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "kind": kind,
            "ownerId": owner,
            "bindingId": binding,
        }
    )


def _summary(value: str, maximum: int) -> str:
    # Wire descriptions may contain newlines; the shortlist is bounded plain text.
    return " ".join(value.split())[:maximum]


def _no_evidence() -> dict[str, Any]:
    return {
        "status": "none",
        "suiteId": None,
        "reportId": None,
        "reportSha256": None,
        "caseCount": 0,
        "passedCases": 0,
    }


def _active_ids(
    database: Database,
    context: WorkspaceContext,
    table: str,
    maximum: int,
) -> list[UUID]:
    if table not in {"discovery_sources", "mcp_discovery_bindings", "mcp_qualification_suites"}:
        raise InvalidInputError("Unsupported task discovery owner")
    with database.transaction(context) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE workspace_id=%s AND principal_id=%s "
            "AND state<>'revoked' ORDER BY id LIMIT %s",
            (context.workspace_id, context.principal_id, maximum + 1),
        ).fetchall()
    if len(rows) > maximum:
        raise ScopeDeniedError("Task discovery owner inventory exceeds its admitted allowance")
    return [UUID(str(row["id"])) for row in rows]


def _pages(read: Callable[[str | None], dict[str, Any]], *, maximum: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    identities: set[str] = set()
    cursors: set[str] = set()
    cursor = None
    while True:
        result = read(cursor)
        page = result.get("items")
        if not isinstance(page, list):
            raise ScopeDeniedError("Task discovery owner returned an incomplete page")
        for item in page:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ScopeDeniedError("Task discovery owner returned an invalid observation")
            if item["id"] in identities:
                raise RevisionConflictError("Task discovery source changed during its bounded scan")
            identities.add(item["id"])
            items.append(item)
            if len(items) > maximum:
                raise ScopeDeniedError("Task discovery source exceeds its complete-scan allowance")
        cursor = result.get("nextCursor")
        if cursor is None:
            return items
        if not page or not isinstance(cursor, str) or cursor in cursors:
            raise ScopeDeniedError("Task discovery source pagination did not make progress")
        cursors.add(cursor)


def _publisher_heads(
    database: Database,
    context: WorkspaceContext,
    service: DiscoveryService,
    identities: list[UUID],
) -> dict[str, dict[str, Any]]:
    if not identities:
        return {}
    with database.transaction(context) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            "SELECT * FROM discovery_publishers WHERE workspace_id=%s AND principal_id=%s "
            "AND id=ANY(%s::uuid[]) ORDER BY id",
            (context.workspace_id, context.principal_id, identities),
        ).fetchall()
        token = service._token()
        heads = {
            str(row["id"]): _pick(
                service._publisher_view(conn, dict(row), token), _PUBLISHER_FIELDS
            )
            for row in rows
        }
    if set(heads) != {str(identity) for identity in identities}:
        raise ScopeDeniedError("A current registry publisher owner is unavailable")
    return heads


def _installed(
    database: Database,
    context: WorkspaceContext,
    service: DiscoveryService,
    views: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    with database.transaction(context) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        _, bindings = installed_skill_preference(
            conn,
            context,
            service._token(),
            [RegistryCandidate.from_dict(view["observation"]) for view in views],
        )
    if any(binding.get("truncated") for binding in bindings):
        raise ScopeDeniedError("Installed skill preference cannot be scanned completely")
    return {
        view["candidateSha256"]: sorted(
            [
                {key: value for key, value in binding.items() if key != "candidateSha256"}
                for binding in bindings
                if view["candidateSha256"] in binding["candidateSha256"]
            ],
            key=lambda binding: binding["skillId"],
        )
        for view in views
    }


def _registry_candidate(
    context: WorkspaceContext,
    view: dict[str, Any],
    source: dict[str, Any],
    publisher: dict[str, Any],
    installed: list[dict[str, Any]],
) -> dict[str, Any]:
    observation = RegistryCandidate.from_dict(view["observation"])
    if (
        source["id"] != view["sourceId"]
        or source["snapshotId"] != view["snapshotId"]
        or publisher["sourceId"] != view["sourceId"]
        or publisher["id"] != view["publisherId"]
        or source["state"] != view["sourceState"]
        or publisher["state"] != view["publisherState"]
        or source["approvalValid"] != view["sourceApprovalValid"]
        or publisher["approvalValid"] != view["publisherApprovalValid"]
        or observation.sha256 != view["candidateSha256"]
    ):
        raise RevisionConflictError("Registry candidate no longer matches its current owner heads")
    current = bool(
        view["sourceApprovalValid"]
        and view["sourceState"] == "active"
        and view["publisherApprovalValid"]
        and view["publisherState"] == "approved"
        and not view["stale"]
        and observation.status in {"active", "unresolved"}
    )
    return {
        "id": _identity(context, "registry", view["id"], None),
        "kind": "registry",
        "name": _summary(observation.name, 256),
        "description": _summary(observation.description, 4096),
        "ownerId": view["id"],
        "bindingId": None,
        "ownerSha256": content_hash(
            {
                "source": source,
                "publisher": publisher,
                "candidate": _pick(view, _REGISTRY_FIELDS),
                "installed": installed,
            }
        ),
        "capabilities": observation.capabilities,
        "requiredTaskCapability": None,
        "installed": bool(installed) and current,
        "authorityCurrent": current,
        "reasonCodes": sorted(
            set([*view["reasonCodes"], "task_capability_unknown", "task_evidence_unavailable"])
        ),
        "taskEvidence": _no_evidence(),
    }


def _registry_candidates(
    database: Database,
    context: WorkspaceContext,
    service: DiscoveryService,
) -> list[dict[str, Any]]:
    identities = _active_ids(database, context, "discovery_sources", MAX_SOURCES)
    sources = {
        str(identity): _pick(service.source(context, identity)["source"], _SOURCE_FIELDS)
        for identity in identities
    }
    if any(source["snapshotId"] and not source["snapshotSha256"] for source in sources.values()):
        raise ScopeDeniedError(
            "A retained registry snapshot failed its existing owner integrity check"
        )
    views = _pages(
        lambda cursor: service.candidates(context, limit=50, after=cursor),
        maximum=MAX_TASK_CANDIDATES,
    )
    if any(view["sourceId"] not in sources for view in views):
        raise RevisionConflictError("Registry sources changed during task discovery")
    publishers = _publisher_heads(
        database, context, service, sorted({UUID(view["publisherId"]) for view in views})
    )
    installed = _installed(database, context, service, views)
    result = [
        _registry_candidate(
            context,
            view,
            sources[view["sourceId"]],
            publishers[view["publisherId"]],
            installed[view["candidateSha256"]],
        )
        for view in views
    ]
    # The public cursor seals each paged source scan. Check the heads also when
    # the whole inventory fitted in one page or was empty.
    if _active_ids(database, context, "discovery_sources", MAX_SOURCES) != identities or any(
        _pick(service.source(context, identity)["source"], _SOURCE_FIELDS) != sources[str(identity)]
        for identity in identities
    ):
        raise RevisionConflictError("Registry sources changed during task discovery")
    return result


def _operation_skill(
    context: WorkspaceContext,
    item: dict[str, Any],
    service: SkillAcquisition,
) -> dict[str, Any] | None:
    """Read only the existing acquired package owner's safe description/control.

    Authored SKILL.md and mutable context documents are deliberately not loaded
    for search. The existing owner verifies the package manifest digest and its
    current control; this projection never parses or executes package resources.
    """
    if item["kind"] != "skill" or "acquired_skill_record_only" not in item["reasons"]:
        return None
    try:
        reference = item["reference"]
        if not reference.startswith("skill:"):
            raise ValueError
        identity = UUID(reference.removeprefix("skill:"))
    except (ValueError, TypeError, AttributeError):
        raise RevisionConflictError(
            "The acquired skill operation has an invalid owner reference"
        ) from None
    try:
        skill = service.inspect(context, identity)["skill"]
    except ScopeDeniedError:
        # The catalogue can retain an operation observation after its private
        # owner becomes unavailable. Keep that explicit exclusion, with no text
        # or positive installation observation borrowed from a historical row.
        return {"unavailable": True}
    if (
        skill["id"] != str(identity)
        or skill["origin"] != "acquired"
        or skill["releaseSha256"] != item["fingerprintSha256"]
        or skill["contextObjectId"] != item["contextObjectId"]
        or skill["owned"] != (skill["ownerPrincipalId"] == str(context.principal_id))
    ):
        raise RevisionConflictError("Acquired skill changed from its operation catalogue head")
    manifest = skill["manifest"]
    if not isinstance(manifest, dict) or not isinstance(manifest.get("description"), str):
        raise ScopeDeniedError("The existing acquired skill owner has no valid description")
    return {**_pick(skill, _SKILL_FIELDS), "description": _summary(manifest["description"], 4096)}


def _operation_candidate(
    context: WorkspaceContext,
    item: dict[str, Any],
    skill: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    head = _pick(item, _OPERATION_FIELDS)
    reasons = sorted(set(item["reasons"]) - _ACCOUNTING_REASONS)
    head["reasons"] = reasons
    current = item["state"] not in {"policy_disabled", "degraded", "indeterminate"} and (
        "unsupported_reference" not in reasons
    )
    description = ""
    capabilities: dict[str, list[str]] = {key: [] for key in _UNKNOWN_CAPABILITIES}
    required_capability = None
    unknown = set(_UNKNOWN_REASONS)
    # The general read model has no canonical port/effect declarations. The
    # acquired-package owner can separately supply a current verified control
    # and a digest-pinned description, without loading SKILL.md instructions.
    installed = bool(
        current
        and item["kind"] != "skill"
        and item["state"] in {"installed_unqualified", "qualified"}
    )
    if skill is not None:
        head["acquiredSkill"] = skill
        reasons = [
            reason for reason in reasons if reason != "skill_activation_signature_not_evaluated"
        ]
        available = not skill.get("unavailable", False)
        current = bool(
            current
            and available
            and skill["state"] == "active"
            and skill["enabled"] is True
            and skill["approvalValid"] is True
        )
        installed = bool(current and skill["owned"] is True)
        if available:
            description = skill["description"]
        reasons.append(
            "acquired_skill_control_current" if current else "acquired_skill_control_unavailable"
        )
        head["reasons"] = reasons
    if metadata is not None:
        # The projection is derived from existing sealed source/handler owners.
        # It does not modify installation, qualification or execution authority.
        head["operationMetadata"] = metadata
        description = metadata["description"]
        capabilities = metadata["capabilities"]
        required_capability = metadata["requiredTaskCapability"]
        reasons.extend(metadata["reasonCodes"])
        if capabilities["inputTypes"] and capabilities["outputTypes"]:
            unknown.discard("canonical_ports_unknown")
        if capabilities["effects"]:
            unknown.discard("effects_unknown")
        if capabilities["scopes"]:
            unknown.discard("canonical_scopes_unknown")
        if required_capability is not None:
            unknown.discard("task_capability_unknown")
        head["reasons"] = sorted(set(reasons))
    return {
        "id": _identity(context, "operation", item["id"], None),
        "kind": "operation",
        "name": _summary(item["name"], 256),
        "description": description,
        "ownerId": item["id"],
        "bindingId": None,
        "ownerSha256": content_hash(head),
        "capabilities": capabilities,
        "requiredTaskCapability": required_capability,
        "installed": installed,
        "authorityCurrent": current,
        "reasonCodes": sorted(set(reasons) | unknown),
        "taskEvidence": _no_evidence(),
    }


def _mcp_current(
    context: WorkspaceContext,
    service: McpDiscoveryService,
    binding: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    if not (
        binding["state"] == "enabled"
        and binding["approvalValid"]
        and not binding["stale"]
        and entry["configuredTool"]
        and entry["referenceAvailable"]
    ):
        return None
    reference: dict[str, Any] = service.reference(context, UUID(binding["id"]), UUID(entry["id"]))[
        "reference"
    ]
    current = service.validate_reference(context, reference)
    if _pick(current, _ENTRY_FIELDS) != _pick(entry, _ENTRY_FIELDS):
        raise RevisionConflictError("Configured MCP entry changed during task discovery")
    return reference


def _mcp_entries(
    context: WorkspaceContext,
    service: McpDiscoveryService,
    identity: UUID,
) -> list[dict[str, Any]]:
    return _pages(
        lambda cursor: service.entries(context, identity, kind="tool", limit=50, after=cursor),
        maximum=256,
    )


def _mcp_candidate(
    context: WorkspaceContext,
    binding: dict[str, Any],
    entry: dict[str, Any],
    reference: dict[str, Any] | None,
) -> dict[str, Any]:
    if (
        entry["bindingId"] != binding["id"]
        or entry["snapshotId"] != binding["snapshotId"]
        or entry["snapshotSha256"] != binding["snapshotSha256"]
        or not entry["configuredTool"]
    ):
        raise RevisionConflictError("MCP entry no longer matches its configured binding")
    current = reference is not None
    return {
        "id": _identity(context, "mcp", entry["id"], binding["id"]),
        "kind": "mcp",
        "name": _summary(entry["name"], 256),
        "description": _summary(entry["description"], 4096),
        "ownerId": entry["id"],
        "bindingId": binding["id"],
        "ownerSha256": content_hash(
            {
                "binding": _pick(binding, _BINDING_FIELDS),
                "entry": _pick(entry, _ENTRY_FIELDS),
                "referenceSha256": content_hash(reference) if reference else None,
            }
        ),
        "capabilities": {key: [] for key in _UNKNOWN_CAPABILITIES},
        "requiredTaskCapability": None,
        "installed": current,
        "authorityCurrent": current,
        "reasonCodes": sorted(
            set(
                [
                    *entry["reasonCodes"],
                    *_UNKNOWN_REASONS,
                    *([] if current else ["mcp_reference_not_current"]),
                ]
            )
        ),
        "taskEvidence": _no_evidence(),
    }


def _evidence(
    context: WorkspaceContext,
    service: McpQualificationService,
    task: dict[str, Any],
    binding: dict[str, Any],
    reference: dict[str, Any] | None,
    suites: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    choices = []
    invalid = False
    for suite in suites:
        if suite["bindingId"] != binding["id"] or suite["lastReportId"] is None:
            continue
        try:
            report = service.report(context, UUID(suite["lastReportId"]))["report"]
            if not report["approvalValid"] or not suite["approvalValid"]:
                invalid = True
                continue
            cases = report["cases"]
            if (
                not isinstance(cases, list)
                or not 1 <= len(cases) <= 8
                or any(
                    not isinstance(case, dict)
                    or case.get("status")
                    not in {"passed", "failed", "not_run", "indeterminate", "pending", "prepared"}
                    for case in cases
                )
            ):
                invalid = True
                continue
            native = report["evidence"]
            exact = bool(
                reference is not None
                and suite["authorityCurrent"]
                and report["authorityCurrent"]
                and suite["activeAttemptId"] is None
                and suite["taskBinding"] == task
                and suite["lastReportId"] == report["id"]
                and suite["id"] == report["suiteId"]
                and suite["lastAttemptId"] == report["attemptId"]
                and report["bindingId"] == binding["id"]
                and native["taskBinding"] == task
                and native["workspaceId"] == str(context.workspace_id)
                and native["principalId"] == str(context.principal_id)
                and native["attemptId"] == report["attemptId"]
                and native["referenceSha256"] == content_hash(reference)
                and all(
                    suite[key] == native[key] == binding[key]
                    for key in ("agentId", "installationSha256", "configurationSha256")
                )
                and native["definitionSha256"] == binding["definitionSha256"]
                and all(
                    suite[key] == native[key]
                    for key in ("suiteSha256", "suiteFileSha256", "consumerSha256")
                )
            )
            passed = sum(case["status"] == "passed" for case in cases)
            matching_pass = (
                report["complete"] is True and report["passed"] is True and passed == len(cases)
            )
            status = (
                ("matching_pass" if matching_pass else "matching_failure")
                if exact
                else "historical"
            )
            choices.append(
                {
                    "status": status,
                    "suiteId": suite["id"],
                    "reportId": report["id"],
                    "reportSha256": report["sha256"],
                    "caseCount": len(cases),
                    "passedCases": passed,
                }
            )
        except (DomainError, KeyError, TypeError, ValueError):
            invalid = True
    if not choices:
        return _no_evidence(), [
            "task_evidence_unavailable",
            *(["task_evidence_invalid"] if invalid else []),
        ]
    # Separate active suites are not a time series. An exact failure wins over
    # an exact pass, and unrelated/historical reports supply no quality boost.
    priority = {"matching_failure": 0, "matching_pass": 1, "historical": 2}
    choices.sort(key=lambda item: (priority[item["status"]], item["suiteId"], item["reportId"]))
    selected = choices[0]
    reason = {
        "matching_pass": "exact_task_report_current",
        "matching_failure": "exact_task_report_failed",
        "historical": "task_evidence_historical",
    }[selected["status"]]
    return selected, [reason, *(["task_evidence_invalid"] if invalid else [])]


def collect_task_candidates(
    database: Database,
    context: WorkspaceContext,
    task_binding: dict[str, Any],
    *,
    setup_token: Callable[[], str | None] | None = None,
    clock: Callable[[], datetime] | None = None,
    artifacts: ArtifactStore | None = None,
) -> dict[str, Any]:
    """Return a complete scoped catalogue, or reject overflow/incomplete scans.

    ``setup_token`` is the same resolver accepted by the existing signed owners.
    The task binding is Q's exact immutable seven-field binding. This function
    does not read arguments, result oracles, private native paths or provider
    content, and it performs no installation, refresh, reservation or execution.
    """
    if not isinstance(context, WorkspaceContext):
        raise ScopeDeniedError("Task discovery requires an authenticated workspace principal")
    task = _task_binding(task_binding)
    resolver = setup_token or native_skill_setup_token
    registry = DiscoveryService(database, setup_token=resolver, clock=clock)
    discovery = McpDiscoveryService(database, setup_token=resolver, clock=clock)
    qualification = McpQualificationService(database, setup_token=resolver, clock=clock)
    skills = SkillAcquisition(database, setup_token_resolver=resolver)
    registry_items = _registry_candidates(database, context, registry)
    # One bounded existing-owner snapshot avoids pagination over display-only
    # recent invocation/accounting changes. Its own 2048-row gate still applies.
    operation_snapshot = OperationCatalogue(database)._snapshot(context)
    if len(registry_items) + len(operation_snapshot) > MAX_TASK_CANDIDATES:
        raise ScopeDeniedError("Task discovery exceeds its complete 256-candidate allowance")
    operation_metadata = OperationDiscoveryMetadata(database, context, artifacts=artifacts)
    operation_items = [
        _operation_candidate(
            context,
            item,
            _operation_skill(context, item, skills),
            operation_metadata.for_operation(item),
        )
        for item in operation_snapshot
    ]
    binding_ids = _active_ids(database, context, "mcp_discovery_bindings", MAX_BINDINGS)
    suite_ids = _active_ids(database, context, "mcp_qualification_suites", MAX_SUITES)
    suites = [qualification.suite(context, identity)["suite"] for identity in suite_ids]
    mcp_items = []
    for identity in binding_ids:
        binding = discovery.binding(context, identity)["binding"]
        entries = _mcp_entries(context, discovery, identity)
        configured = [entry for entry in entries if entry["configuredTool"]]
        if len(configured) > 1:
            raise ScopeDeniedError("An MCP binding must identify exactly one configured tool")
        for entry in configured:
            reference = _mcp_current(context, discovery, binding, entry)
            candidate = _mcp_candidate(context, binding, entry, reference)
            evidence, reasons = _evidence(context, qualification, task, binding, reference, suites)
            candidate["taskEvidence"] = evidence
            candidate["reasonCodes"] = sorted(
                (set(candidate["reasonCodes"]) - {"task_evidence_unavailable"}) | set(reasons)
            )
            mcp_items.append(candidate)
        if _pick(discovery.binding(context, identity)["binding"], _BINDING_FIELDS) != _pick(
            binding, _BINDING_FIELDS
        ):
            raise RevisionConflictError("MCP binding changed during task discovery")
    if _active_ids(database, context, "mcp_discovery_bindings", MAX_BINDINGS) != binding_ids:
        raise RevisionConflictError("MCP bindings changed during task discovery")
    items = validate_task_candidates([*registry_items, *operation_items, *mcp_items])
    types_sha = catalog_fingerprint()
    return {
        "items": items,
        "catalogueSha256": content_hash(
            {
                "schema": "potbot-task-discovery-catalogue/v1",
                "workspaceId": str(context.workspace_id),
                "principalId": str(context.principal_id),
                "taskBinding": task,
                "typesSha256": types_sha,
                "items": items,
            }
        ),
        "counts": {
            "registry": len(registry_items),
            "operations": len(operation_items),
            "mcp": len(mcp_items),
        },
        "typesSha256": types_sha,
    }


def inspect_task_candidate(
    database: Database,
    context: WorkspaceContext,
    candidate: dict[str, Any],
    *,
    setup_token: Callable[[], str | None] | None = None,
    clock: Callable[[], datetime] | None = None,
    artifacts: ArtifactStore | None = None,
) -> dict[str, Any]:
    """Read one existing-owner detail after the service revalidates its snapshot.

    The exact owner digest and scoped candidate identity are independently
    checked. The returned envelope is at most 128 KiB; no native work occurs.
    Evidence freshness across the task is checked by the parent catalogue read.
    """
    if not isinstance(context, WorkspaceContext):
        raise ScopeDeniedError("Task discovery requires an authenticated workspace principal")
    candidate = validate_task_candidates([candidate])[0]
    if candidate["id"] != _identity(
        context, candidate["kind"], candidate["ownerId"], candidate["bindingId"]
    ):
        raise ScopeDeniedError("Candidate identity belongs to a different owner scope")
    resolver = setup_token or native_skill_setup_token
    if candidate["kind"] == "registry":
        registry = DiscoveryService(database, setup_token=resolver, clock=clock)
        view = registry.candidate(context, UUID(candidate["ownerId"]))["candidate"]
        source = _pick(registry.source(context, UUID(view["sourceId"]))["source"], _SOURCE_FIELDS)
        publishers = _publisher_heads(database, context, registry, [UUID(view["publisherId"])])
        installed = _installed(database, context, registry, [view])
        current = _registry_candidate(
            context,
            view,
            source,
            publishers[view["publisherId"]],
            installed[view["candidateSha256"]],
        )
        detail = {"candidate": view}
    elif candidate["kind"] == "operation":
        item = OperationCatalogue(database).inspect(context, candidate["ownerId"])["item"]
        skill = _operation_skill(
            context, item, SkillAcquisition(database, setup_token_resolver=resolver)
        )
        metadata = OperationDiscoveryMetadata(database, context, artifacts=artifacts).for_operation(
            item
        )
        current = _operation_candidate(context, item, skill, metadata)
        detail = {"item": item}
        if skill is not None:
            detail["acquiredSkill"] = skill
        if metadata is not None:
            detail["operationMetadata"] = metadata
    else:
        discovery = McpDiscoveryService(database, setup_token=resolver, clock=clock)
        binding = discovery.binding(context, UUID(candidate["bindingId"]))["binding"]
        entries = _mcp_entries(context, discovery, UUID(candidate["bindingId"]))
        matches = [entry for entry in entries if entry["id"] == candidate["ownerId"]]
        if len(matches) != 1:
            raise ScopeDeniedError("The configured MCP candidate is no longer available")
        entry = matches[0]
        reference = _mcp_current(context, discovery, binding, entry)
        current = _mcp_candidate(context, binding, entry, reference)
        detail = {"entry": entry, "reference": reference}
    if current["ownerSha256"] != candidate["ownerSha256"]:
        raise RevisionConflictError(
            "Candidate owner changed; create a fresh task discovery snapshot"
        )
    result: dict[str, Any] = bounded_json(
        {
            "id": current["id"],
            "kind": current["kind"],
            "ownerId": current["ownerId"],
            "ownerSha256": current["ownerSha256"],
            "detail": detail,
        },
        maximum=MAX_INSPECTION_BYTES,
    )
    return result
