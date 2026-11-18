from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from potbot.controller import task_discovery_sources as sources
from potbot.controller.registry_sources import parse_curated_manifest
from potbot.controller.task_discovery_ranking import rank_task_candidates
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash

ROOT = Path(__file__).resolve().parents[2]


def identity(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, "task-discovery-test:" + label))


def digest(label: str) -> str:
    return content_hash(label)


class Connection:
    def __init__(self, world: World) -> None:
        self.world = world
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, arguments: Any = None) -> Connection:
        self.world.statements.append(sql)
        if sql == "SET TRANSACTION READ ONLY":
            self.rows = []
            return self
        assert sql.startswith("SELECT "), sql
        assert arguments[:2] == (self.world.context.workspace_id, self.world.context.principal_id)
        if "FROM discovery_publishers " in sql:
            selected = {str(value) for value in arguments[2]}
            self.rows = [
                copy.deepcopy(value)
                for key, value in self.world.publishers.items()
                if key in selected
            ]
        else:
            tables = {
                "discovery_sources": self.world.registry_sources,
                "mcp_discovery_bindings": self.world.bindings,
                "mcp_qualification_suites": self.world.suites,
            }
            table = next(name for name in tables if "FROM " + name + " " in sql)
            values = sorted(
                (key for key, value in tables[table].items() if value["state"] != "revoked")
            )
            self.rows = [{"id": key} for key in values[: arguments[-1]]]
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class World:
    def __init__(self) -> None:
        self.context = WorkspaceContext(UUID(identity("workspace")), UUID(identity("principal")))
        self.task = {
            "episodeId": identity("episode"),
            "contractRevisionId": identity("contract"),
            "contractSha256": digest("contract"),
            "repositoryStateId": identity("repository"),
            "repositorySha256": digest("repository"),
            "semanticReleaseId": identity("release"),
            "semanticReleaseSha256": digest("release"),
        }
        record = parse_curated_manifest(
            (ROOT / "examples/registry/curated-capabilities.json").read_bytes()
        )[0]
        source, publisher, snapshot, owner = (
            identity(label) for label in ("source", "publisher", "snapshot", "registry-owner")
        )
        self.registry_sources = {
            source: {
                "id": source,
                "revision": 1,
                "state": "active",
                "configSha256": digest("source-config"),
                "bindingSha256": digest("source-head"),
                "approvalValid": True,
                "snapshotId": snapshot,
                "snapshotSha256": digest("registry-snapshot"),
                "lastErrorCode": None,
                "createdAt": "display-time",
                "updatedAt": "display-time",
            }
        }
        self.publishers = {
            publisher: {
                "id": publisher,
                "sourceId": source,
                "revision": 1,
                "state": "approved",
                "bindingSha256": digest("publisher"),
                "approvalValid": True,
            }
        }
        self.registry = [
            {
                "id": owner,
                "sourceId": source,
                "snapshotId": snapshot,
                "candidateSha256": record.sha256,
                "observation": record.to_dict(),
                "sourceState": "active",
                "publisherId": publisher,
                "publisherState": "approved",
                "sourceApprovalValid": True,
                "publisherApprovalValid": True,
                "stale": False,
                "canReview": False,
                "reasonCodes": ["distribution_inspection_only"],
                "executable": False,
                "score": None,
                "scoreReasons": [],
            }
        ]
        operation_id = digest("operation")
        self.operations = [
            {
                "id": operation_id,
                "kind": "tool",
                "reference": "tool://commit-inspection",
                "name": "Commit tool",
                "state": "installed_unqualified",
                "reasons": ["qualification_not_established", "accounting_scope_limited"],
                "contextObjectId": identity("tool"),
                "version": "1",
                "fingerprintSha256": digest("tool"),
                "updateAvailable": False,
                "sources": [],
                "release": None,
                "recentInvocations": [],
                "accounting": {"calls": 0},
            }
        ]
        binding, entry, mcp_snapshot = (
            identity(label) for label in ("binding", "entry", "mcp-snapshot")
        )
        self.bindings = {
            binding: {
                "id": binding,
                "agentId": identity("agent"),
                "revision": 1,
                "state": "enabled",
                "bindingSha256": digest("binding"),
                "approvalValid": True,
                "installationSha256": digest("installation"),
                "configurationSha256": digest("configuration"),
                "definitionSha256": digest("installed-definition"),
                "toolName": "configured-tool",
                "toolFingerprint": digest("tool-fingerprint"),
                "transport": "stdio",
                "snapshotId": mcp_snapshot,
                "snapshotSha256": digest("mcp-snapshot"),
                "stale": False,
                "lastErrorCode": None,
                "activeAttemptId": None,
                "createdAt": "display-time",
                "updatedAt": "display-time",
            }
        }
        self.entries = {
            binding: [
                {
                    "id": entry,
                    "bindingId": binding,
                    "snapshotId": mcp_snapshot,
                    "snapshotSha256": digest("mcp-snapshot"),
                    "kind": "tool",
                    "identity": "configured-tool",
                    "name": "Configured MCP tool",
                    "description": "Inspect repository commits and revision history.",
                    "definition": {"name": "configured-tool", "inputSchema": {"type": "object"}},
                    "definitionSha256": digest("wire-definition"),
                    "configuredTool": True,
                    "referenceAvailable": True,
                    "reasonCodes": [],
                    "executable": False,
                }
            ]
        }
        self.references = {
            entry: {
                "schema": "potbot-mcp-discovery-reference/v1",
                "bindingId": binding,
                "entryId": entry,
                "bindingSha256": digest("binding"),
                "snapshotId": mcp_snapshot,
                "definitionSha256": digest("wire-definition"),
                "expiresAt": "stable-expiry",
            }
        }
        self.suites: dict[str, dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.skill_views: dict[str, dict[str, Any]] = {}
        self.statements: list[str] = []
        self.installed: list[dict[str, Any]] = []

    @contextmanager
    def transaction(self, context: WorkspaceContext) -> Iterator[Connection]:
        assert context == self.context
        yield Connection(self)

    def add_report(self, label: str = "pass", *, passed: bool = True) -> None:
        binding = next(iter(self.bindings.values()))
        entry = self.entries[binding["id"]][0]
        suite, report, attempt = (
            identity(label + suffix) for suffix in ("-suite", "-report", "-attempt")
        )
        self.suites[suite] = {
            "id": suite,
            "bindingId": binding["id"],
            "state": "enabled",
            "approvalValid": True,
            "authorityCurrent": True,
            "reasonCodes": [],
            "activeAttemptId": None,
            "lastReportId": report,
            "lastAttemptId": attempt,
            "taskBinding": copy.deepcopy(self.task),
            "suiteSha256": digest(label + "-suite"),
            "suiteFileSha256": digest(label + "-file"),
            "consumerSha256": digest("consumer"),
            **{
                key: binding[key]
                for key in ("agentId", "installationSha256", "configurationSha256")
            },
        }
        native = {
            **{
                key: self.suites[suite][key]
                for key in (
                    "suiteSha256",
                    "suiteFileSha256",
                    "consumerSha256",
                    "agentId",
                    "installationSha256",
                    "configurationSha256",
                    "taskBinding",
                )
            },
            "workspaceId": str(self.context.workspace_id),
            "principalId": str(self.context.principal_id),
            "attemptId": attempt,
            "definitionSha256": binding["definitionSha256"],
            "referenceSha256": content_hash(self.references[entry["id"]]),
        }
        self.reports[report] = {
            "id": report,
            "suiteId": suite,
            "attemptId": attempt,
            "bindingId": binding["id"],
            "sha256": digest(label + "-report"),
            "approvalValid": True,
            "authorityCurrent": True,
            "complete": True,
            "passed": passed,
            "reasonCodes": [],
            "evidence": native,
            "cases": [{"status": "passed" if passed else "failed"}],
        }

    def add_acquired_skill(self) -> tuple[dict[str, Any], dict[str, Any]]:
        skill_id = identity("acquired-skill")
        item = copy.deepcopy(self.operations[0])
        item.update(
            id=digest("skill-operation"),
            kind="skill",
            reference="skill:" + skill_id,
            name="retained-notes",
            contextObjectId=identity("skill-context"),
            fingerprintSha256=digest("skill-release"),
            reasons=[
                "skill_is_untrusted_data",
                "acquired_skill_record_only",
                "skill_activation_signature_not_evaluated",
            ],
            sources=[
                {
                    "family": "published_skill",
                    "recordId": skill_id,
                    "revision": 1,
                    "sha256": digest("skill-release"),
                }
            ],
        )
        self.operations.append(item)
        skill = {
            "id": skill_id,
            "origin": "acquired",
            "ownerPrincipalId": str(self.context.principal_id),
            "owned": True,
            "contextObjectId": item["contextObjectId"],
            "revision": 1,
            "state": "active",
            "enabled": True,
            "approvalValid": True,
            "releaseSha256": item["fingerprintSha256"],
            "contentSha256": digest("skill-content"),
            "packageId": identity("skill-package"),
            "sourceSha256": digest("skill-source"),
            "manifest": {"description": "Keep exact retained references and review notes."},
        }
        self.skill_views[skill_id] = skill
        return item, skill


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    world = World()

    class Registry:
        def __init__(self, database: Any, **kwargs: Any) -> None:
            assert database is world

        def _token(self) -> str:
            return "s" * 32

        def _publisher_view(self, conn: Any, row: Any, token: Any) -> Any:
            return row

        def source(self, context: Any, identity: UUID) -> dict[str, Any]:
            return {"source": copy.deepcopy(world.registry_sources[str(identity)])}

        def candidates(self, context: Any, *, limit: int, after: str | None) -> dict[str, Any]:
            offset = int(after) if after else 0
            items = world.registry[offset : offset + limit]
            return {
                "items": copy.deepcopy(items),
                "nextCursor": str(offset + limit) if offset + limit < len(world.registry) else None,
            }

        def candidate(self, context: Any, identity: UUID) -> dict[str, Any]:
            return {
                "candidate": copy.deepcopy(
                    next(row for row in world.registry if row["id"] == str(identity))
                )
            }

    class Operations:
        def __init__(self, database: Any) -> None:
            assert database is world

        def _snapshot(self, context: Any) -> list[dict[str, Any]]:
            return copy.deepcopy(world.operations)

        def inspect(self, context: Any, identity: str) -> dict[str, Any]:
            return {
                "item": copy.deepcopy(
                    next(row for row in world.operations if row["id"] == identity)
                )
            }

    class Mcp:
        def __init__(self, database: Any, **kwargs: Any) -> None:
            assert database is world

        def binding(self, context: Any, identity: UUID) -> dict[str, Any]:
            return {"binding": copy.deepcopy(world.bindings[str(identity)])}

        def entries(self, context: Any, identity: UUID, **kwargs: Any) -> dict[str, Any]:
            offset = int(kwargs["after"]) if kwargs["after"] else 0
            items = world.entries[str(identity)]
            limit = kwargs["limit"]
            return {
                "items": copy.deepcopy(items[offset : offset + limit]),
                "nextCursor": str(offset + limit) if offset + limit < len(items) else None,
            }

        def reference(self, context: Any, binding: UUID, entry: UUID) -> dict[str, Any]:
            return {"reference": copy.deepcopy(world.references[str(entry)])}

        def validate_reference(self, context: Any, reference: dict[str, Any]) -> dict[str, Any]:
            assert reference == world.references[reference["entryId"]]
            return copy.deepcopy(
                next(
                    row
                    for row in world.entries[reference["bindingId"]]
                    if row["id"] == reference["entryId"]
                )
            )

    class Qualification:
        def __init__(self, database: Any, **kwargs: Any) -> None:
            assert database is world

        def suite(self, context: Any, identity: UUID) -> dict[str, Any]:
            return {"suite": copy.deepcopy(world.suites[str(identity)])}

        def report(self, context: Any, identity: UUID) -> dict[str, Any]:
            return {"report": copy.deepcopy(world.reports[str(identity)])}

    class Skills:
        def __init__(self, database: Any, **kwargs: Any) -> None:
            assert database is world

        def inspect(self, context: Any, identity: UUID) -> dict[str, Any]:
            if str(identity) not in world.skill_views:
                raise ScopeDeniedError("The exact existing acquired-skill owner is unavailable")
            return {"skill": copy.deepcopy(world.skill_views[str(identity)])}

    def installed(conn: Any, context: Any, token: Any, candidates: Any) -> Any:
        assert context == world.context and token == "s" * 32
        shas = {candidate.sha256 for candidate in candidates}
        rows = copy.deepcopy(world.installed)
        for row in rows:
            if "candidateSha256" in row:
                row["candidateSha256"] = sorted(shas)
        return sorted(shas) if rows else [], rows

    monkeypatch.setattr(sources, "DiscoveryService", Registry)
    monkeypatch.setattr(sources, "OperationCatalogue", Operations)
    monkeypatch.setattr(sources, "McpDiscoveryService", Mcp)
    monkeypatch.setattr(sources, "McpQualificationService", Qualification)
    monkeypatch.setattr(sources, "SkillAcquisition", Skills)
    monkeypatch.setattr(sources, "installed_skill_preference", installed)
    return world


def collect(world: World) -> dict[str, Any]:
    return sources.collect_task_candidates(world, world.context, world.task)  # type: ignore[arg-type]


def of_kind(result: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(row for row in result["items"] if row["kind"] == kind)


def test_collection_reuses_read_owners_and_retains_explicit_unknown_ports(world: World) -> None:
    result = collect(world)
    assert result["counts"] == {"registry": 1, "operations": 1, "mcp": 1}
    assert len(result["typesSha256"]) == len(result["catalogueSha256"]) == 64
    registry = of_kind(result, "registry")
    assert registry["authorityCurrent"] and not world.registry[0]["canReview"]
    for kind in ("operation", "mcp"):
        row = of_kind(result, kind)
        assert all(value == [] for value in row["capabilities"].values())
        assert row["requiredTaskCapability"] is None
        assert "canonical_ports_unknown" in row["reasonCodes"]
    assert all(
        sql.startswith("SELECT ") or sql == "SET TRANSACTION READ ONLY" for sql in world.statements
    )
    payload = canonical_json(result)
    assert "inputSchema" not in payload and "oracle" not in payload and "nativePath" not in payload


def test_display_timestamps_and_recent_accounting_do_not_change_selection_owner_hashes(
    world: World,
) -> None:
    first = collect(world)
    next(iter(world.registry_sources.values()))["updatedAt"] = "later"
    next(iter(world.bindings.values()))["createdAt"] = "later"
    world.operations[0]["accounting"] = {"calls": 999999}
    world.operations[0]["recentInvocations"] = [{"state": "indeterminate", "recordedAt": "later"}]
    world.operations[0]["reasons"].append("invocation_indeterminate")
    assert collect(world) == first
    next(iter(world.publishers.values()))["revision"] += 1
    changed = collect(world)
    assert changed["catalogueSha256"] != first["catalogueSha256"]
    assert of_kind(changed, "registry")["ownerSha256"] != of_kind(first, "registry")["ownerSha256"]


def test_only_the_configured_tool_enters_task_discovery(world: World) -> None:
    binding = next(iter(world.entries))
    unrelated = copy.deepcopy(world.entries[binding][0])
    unrelated.update(
        id=identity("unrelated-entry"), configuredTool=False, name="Commit everything tool"
    )
    world.entries[binding].append(unrelated)
    result = collect(world)
    assert result["counts"]["mcp"] == 1
    unrelated["configuredTool"] = True
    with pytest.raises(ScopeDeniedError, match="exactly one"):
        collect(world)


def test_current_exact_task_report_is_joined_but_never_grants_execution(world: World) -> None:
    world.add_report()
    result = collect(world)
    row = of_kind(result, "mcp")
    assert row["taskEvidence"]["status"] == "matching_pass"
    assert row["taskEvidence"]["passedCases"] == row["taskEvidence"]["caseCount"] == 1
    ranked = rank_task_candidates(
        [row], "commit history", {"requireTaskEvidence": True}, task_capabilities=[]
    )
    assert ranked["items"][0]["features"]["evidence"] == 250
    assert ranked["items"][0]["executable"] is False


@pytest.mark.parametrize(
    "field",
    [
        "episodeId",
        "contractRevisionId",
        "contractSha256",
        "repositoryStateId",
        "repositorySha256",
        "semanticReleaseId",
        "semanticReleaseSha256",
    ],
)
def test_report_for_any_different_task_binding_is_historical_without_preference(
    world: World, field: str
) -> None:
    world.add_report()
    world.task[field] = digest("different") if field.endswith("Sha256") else identity("different")
    row = of_kind(collect(world), "mcp")
    assert row["taskEvidence"]["status"] == "historical"
    ranked = rank_task_candidates([row], "commit history", None, task_capabilities=[])
    assert ranked["items"][0]["features"]["evidence"] == 0
    assert "task_evidence_historical" in row["reasonCodes"]


@pytest.mark.parametrize(
    "owner,field,value",
    [
        ("report", "authorityCurrent", False),
        ("suite", "authorityCurrent", False),
        ("suite", "activeAttemptId", identity("active-attempt")),
        ("suite", "lastAttemptId", identity("changed-attempt")),
        ("evidence", "referenceSha256", digest("other-reference")),
        ("evidence", "configurationSha256", digest("other-configuration")),
        ("evidence", "installationSha256", digest("other-installation")),
        ("evidence", "definitionSha256", digest("other-definition")),
        ("evidence", "agentId", identity("other-agent")),
        ("evidence", "workspaceId", identity("other-workspace")),
        ("evidence", "principalId", identity("other-principal")),
    ],
)
def test_report_currentness_and_exact_installed_reference_pins_are_required(
    world: World,
    owner: str,
    field: str,
    value: Any,
) -> None:
    world.add_report()
    selected = next(iter(world.reports.values()))
    target = (
        next(iter(world.suites.values()))
        if owner == "suite"
        else selected["evidence"]
        if owner == "evidence"
        else selected
    )
    target[field] = value
    row = of_kind(collect(world), "mcp")
    assert row["taskEvidence"]["status"] == "historical"


def test_invalid_report_and_suite_admission_never_supply_measured_case_evidence(
    world: World,
) -> None:
    world.add_report()
    selected = next(iter(world.reports.values()))
    selected["approvalValid"] = False
    row = of_kind(collect(world), "mcp")
    assert row["taskEvidence"]["status"] == "none"
    assert row["taskEvidence"]["caseCount"] == 0 and "task_evidence_invalid" in row["reasonCodes"]
    selected["approvalValid"] = True
    next(iter(world.suites.values()))["approvalValid"] = False
    assert of_kind(collect(world), "mcp")["taskEvidence"]["status"] == "none"


def test_exact_failure_takes_precedence_over_separate_suite_pass_without_time_order_claim(
    world: World,
) -> None:
    world.add_report("pass")
    world.add_report("failure", passed=False)
    row = of_kind(collect(world), "mcp")
    assert row["taskEvidence"]["status"] == "matching_failure"
    assert row["taskEvidence"]["passedCases"] == 0
    assert "exact_task_report_failed" in row["reasonCodes"]
    ranked = rank_task_candidates(
        [row], "commit history", {"requireTaskEvidence": True}, task_capabilities=[]
    )
    assert ranked["noSuitableCandidate"]


def test_stale_disabled_and_revoked_binding_cannot_be_promoted_by_task_evidence(
    world: World,
) -> None:
    world.add_report()
    binding = next(iter(world.bindings.values()))
    binding["stale"] = True
    row = of_kind(collect(world), "mcp")
    assert not row["authorityCurrent"] and not row["installed"]
    assert row["taskEvidence"]["status"] == "historical"
    assert rank_task_candidates([row], "commit history", None, task_capabilities=[])[
        "noSuitableCandidate"
    ]
    binding["state"] = "revoked"
    assert collect(world)["counts"]["mcp"] == 0


def test_registry_publisher_blocks_and_operation_disabled_state_are_preserved_as_exclusions(
    world: World,
) -> None:
    next(iter(world.publishers.values()))["state"] = "blocked"
    world.registry[0]["publisherState"] = "blocked"
    world.operations[0]["state"] = "policy_disabled"
    result = collect(world)
    assert not of_kind(result, "registry")["authorityCurrent"]
    assert not of_kind(result, "operation")["authorityCurrent"]


def test_incomplete_owner_scans_and_candidate_overflow_fail_instead_of_silent_partial_catalogue(
    world: World,
) -> None:
    original = copy.deepcopy(world.operations[0])
    world.operations = [{**original, "id": digest(str(index))} for index in range(256)]
    with pytest.raises(ScopeDeniedError, match="256-candidate"):
        collect(world)
    world.operations = []
    world.installed = [{"preferenceLimit": 256, "truncated": True}]
    with pytest.raises(ScopeDeniedError, match="completely"):
        collect(world)
    world.installed = []
    next(iter(world.registry_sources.values()))["snapshotSha256"] = None
    with pytest.raises(ScopeDeniedError, match="integrity"):
        collect(world)


def test_nonprogressing_or_duplicate_pages_are_rejected_without_looping() -> None:
    with pytest.raises(ScopeDeniedError, match="progress"):
        sources._pages(lambda cursor: {"items": [], "nextCursor": "again"}, maximum=10)
    with pytest.raises(RevisionConflictError):
        sources._pages(
            lambda cursor: {"items": [{"id": "same"}], "nextCursor": "again"}, maximum=10
        )
    with pytest.raises(ScopeDeniedError, match="allowance"):
        sources._pages(
            lambda cursor: {"items": [{"id": "1"}, {"id": "2"}], "nextCursor": None}, maximum=1
        )


def test_inspection_rechecks_owner_scope_and_fingerprint_and_returns_only_bounded_detail(
    world: World,
) -> None:
    result = collect(world)
    for candidate in result["items"]:
        detail = sources.inspect_task_candidate(world, world.context, candidate)  # type: ignore[arg-type]
        assert detail["id"] == candidate["id"] and detail["ownerSha256"] == candidate["ownerSha256"]
        assert len(canonical_json(detail).encode()) <= 128 * 1024
    candidate = of_kind(result, "operation")
    wrong_context = WorkspaceContext(
        world.context.workspace_id, UUID(identity("different-principal"))
    )
    with pytest.raises(ScopeDeniedError, match="scope"):
        sources.inspect_task_candidate(world, wrong_context, candidate)  # type: ignore[arg-type]
    world.operations[0]["version"] = "2"
    with pytest.raises(RevisionConflictError, match="changed"):
        sources.inspect_task_candidate(world, world.context, candidate)  # type: ignore[arg-type]


def test_inspection_has_no_definition_expansion_past_the_public_byte_bound(world: World) -> None:
    candidate = of_kind(collect(world), "mcp")
    binding = next(iter(world.entries))
    world.entries[binding][0]["definition"]["description"] = "x" * (128 * 1024)
    with pytest.raises(InvalidInputError, match="byte allowance"):
        sources.inspect_task_candidate(world, world.context, candidate)  # type: ignore[arg-type]


def test_verified_acquired_skill_description_and_control_use_the_existing_owner(
    world: World,
) -> None:
    item, skill = world.add_acquired_skill()
    result = collect(world)
    candidate = next(row for row in result["items"] if row["ownerId"] == item["id"])
    assert candidate["description"] == skill["manifest"]["description"]
    assert candidate["installed"] and candidate["authorityCurrent"]
    assert "acquired_skill_control_current" in candidate["reasonCodes"]
    assert "skill_activation_signature_not_evaluated" not in candidate["reasonCodes"]
    assert all(value == [] for value in candidate["capabilities"].values())
    ranked = rank_task_candidates(
        [candidate], "Keep retained references", None, task_capabilities=[]
    )
    assert ranked["items"][0]["features"]["installed"] == 2000
    assert ranked["items"][0]["executable"] is False
    inspected = sources.inspect_task_candidate(world, world.context, candidate)  # type: ignore[arg-type]
    assert inspected["detail"]["acquiredSkill"]["description"] == candidate["description"]
    assert "manifest" not in inspected["detail"]["acquiredSkill"]
    skill["revision"] += 1
    with pytest.raises(RevisionConflictError, match="changed"):
        sources.inspect_task_candidate(world, world.context, candidate)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value", [("enabled", False), ("state", "revoked"), ("approvalValid", False)]
)
def test_unavailable_acquired_skill_control_never_creates_a_current_installed_candidate(
    world: World,
    field: str,
    value: Any,
) -> None:
    item, skill = world.add_acquired_skill()
    skill[field] = value
    candidate = next(row for row in collect(world)["items"] if row["ownerId"] == item["id"])
    assert not candidate["authorityCurrent"] and not candidate["installed"]
    assert "acquired_skill_control_unavailable" in candidate["reasonCodes"]
    assert rank_task_candidates(
        [candidate], "Keep retained references", None, task_capabilities=[]
    )["noSuitableCandidate"]


def test_shared_acquired_skill_does_not_borrow_this_principals_installed_preference(
    world: World,
) -> None:
    item, skill = world.add_acquired_skill()
    skill.update(ownerPrincipalId=identity("other-owner"), owned=False)
    candidate = next(row for row in collect(world)["items"] if row["ownerId"] == item["id"])
    assert candidate["authorityCurrent"] and not candidate["installed"]
    assert candidate["description"] == skill["manifest"]["description"]


def test_missing_or_changed_acquired_skill_owner_is_explicit_without_context_text_fallback(
    world: World,
) -> None:
    item, skill = world.add_acquired_skill()
    del world.skill_views[skill["id"]]
    candidate = next(row for row in collect(world)["items"] if row["ownerId"] == item["id"])
    assert not candidate["authorityCurrent"] and candidate["description"] == ""
    world.skill_views[skill["id"]] = skill
    skill["releaseSha256"] = digest("replaced-release")
    with pytest.raises(RevisionConflictError, match="catalogue head"):
        collect(world)
