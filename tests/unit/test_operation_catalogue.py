"""Catalogue projection contracts, including unqualified and private observations."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest

from potbot.controller.operation_catalogue import OperationCatalogue, project_observations
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    RevisionConflictError,
)


def observations():
    return {
        key: []
        for key in (
            "runtime",
            "tool_history",
            "upgrades",
            "models",
            "deployments",
            "skills",
            "syn",
            "recent",
            "accounting",
        )
    }


def runtime(reference="shared-tool:example", kind="tool"):
    return {
        "id": str(uuid4()),
        "kind": kind,
        "reference": reference,
        "fingerprint": "a" * 64,
        "revision": 1,
        "state": "active",
    }


def upgrade(*, state="active", phase="released", created="2026-01-01T00:00:00+00:00"):
    return {
        "id": str(uuid4()),
        "slug": "example",
        "agent_id": str(uuid4()),
        "old_commit": "a" * 40,
        "new_commit": "b" * 40,
        "revision": 5,
        "state": state,
        "release_phase": phase,
        "release_generation": 0 if phase == "none" else 1,
        "evidence_sha256": "c" * 64,
        "suite_sha256": "d" * 64,
        "release_binding": {"private": "host-path-canary"} if phase != "none" else None,
        "release_evidence": {"private": "postflight-canary"} if phase == "released" else None,
        "created_at": created,
        "updated_at": created,
    }


def invocation(model, reservation, source="reasoning"):
    return {
        "id": str(uuid4()),
        "reference": model,
        "state": "settled",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "reservation_id": reservation,
        "source": source,
        "kind": "model",
    }


class ReadDatabase:
    def __init__(self, data):
        self.data, self.calls = data, []

    @contextmanager
    def transaction(self, context):
        self.calls.append(context)
        owner = self

        class Connection:
            def execute(self, query, params=None):
                assert query == "SET TRANSACTION READ ONLY" or query.startswith("WITH\n")
                if params is not None:
                    assert params["workspace"] == context.workspace_id
                    assert params["principal"] == context.principal_id
                return self

            def fetchone(self):
                return {"observations": copy.deepcopy(owner.data)}

        yield Connection()


def test_source_runtime_release_and_model_claims_never_become_current_qualification():
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["runtime"] = [runtime()]
    data["upgrades"] = [upgrade()]
    data["models"] = [{"id": str(uuid4()), "canonical_model": "vendor/model", "role": "reasoning"}]
    data["deployments"] = [
        {
            "id": str(uuid4()),
            "canonical_model": "vendor/model",
            "content_hash": "f" * 64,
            "health": "healthy",
            "qualified": True,
        }
    ]
    data["syn"] = [
        {"id": str(uuid4()), "ref": "syn://cog/available-source", "source_hash": "e" * 64}
    ]
    result = project_observations(actor, data)
    assert {row["state"] for row in result} == {"source_only", "installed_unqualified"}
    tool = next(row for row in result if row["kind"] == "tool")
    assert tool["release"]["releaseState"] == "released"
    assert "release_record_not_current_admission" in tool["reasons"]
    assert "host-path-canary" not in json.dumps(result)
    assert "postflight-canary" not in json.dumps(result)
    assert all("callable" not in row and "authorized" not in row for row in result)


@pytest.mark.parametrize(
    ("state", "phase", "expected", "reason"),
    [
        ("executing", "none", "policy_disabled", "activation_in_progress"),
        ("awaiting_repin", "none", "policy_disabled", "runtime_repin_required"),
        ("awaiting_repin", "repinned", "policy_disabled", "postflight_required"),
        ("awaiting_repin", "testing", "policy_disabled", "postflight_running"),
        ("awaiting_repin", "failed", "degraded", "postflight_failed"),
        ("indeterminate", "none", "indeterminate", "release_indeterminate"),
    ],
)
def test_release_records_keep_specific_blockers(state, phase, expected, reason):
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["upgrades"] = [upgrade(state=state, phase=phase)]
    item = project_observations(actor, data)[0]
    assert item["state"] == expected and reason in item["reasons"]
    if phase == "none":
        assert item["release"]["generation"] is None


def test_pending_new_update_does_not_replace_prior_activation_or_imply_disabled_tool():
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["runtime"] = [runtime()]
    active = upgrade()
    pending = upgrade(state="awaiting_approval", phase="none", created="2026-02-01T00:00:00+00:00")
    data["upgrades"] = [pending, active]
    item = project_observations(actor, data)[0]
    assert item["state"] == "installed_unqualified"
    assert item["updateAvailable"] is True
    assert item["release"]["operationId"] == active["id"]
    assert len(item["sources"]) == 3


def test_accounting_is_deduplicated_preserves_origins_and_never_attaches_to_same_named_tool():
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["models"] = [{"id": str(uuid4()), "canonical_model": "shared-name", "role": "reasoning"}]
    data["runtime"] = [runtime("shared-name")]
    reservation, other = str(uuid4()), str(uuid4())
    data["recent"] = [
        invocation("shared-name", reservation),
        invocation("shared-name", reservation, "hosted"),
        invocation("shared-name", other),
    ]
    data["accounting"] = [
        {
            "id": reservation,
            "status": "settled",
            "amounts": {"tokens": 7, "concurrency": 1},
            "origin": "synthetic",
        },
        {"id": other, "status": "settled", "amounts": {"tokens": 3}, "origin": "provider_reported"},
    ]
    result = project_observations(actor, data)
    tool = next(row for row in result if row["kind"] == "tool")
    model = next(row for row in result if row["kind"] == "model")
    assert tool["recentInvocations"] == [] and tool["accounting"]["totals"] == []
    assert model["reference"] == "model:shared-name"
    assert model["accounting"]["settledCount"] == 2
    assert model["accounting"]["totals"] == [
        {"unit": "tokens", "value": "3", "provenance": "provider_reported"},
        {"unit": "tokens", "value": "7", "provenance": "synthetic"},
    ]
    assert model["accounting"]["scope"] == "linked_recent_invocations"
    assert model["accounting"]["complete"] is False


def test_missing_accounting_is_unavailable_and_private_reference_is_redacted():
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["runtime"] = [runtime("/etc/private-secret-canary")]
    item = project_observations(actor, data)[0]
    assert item["reference"].startswith("opaque:")
    assert "private-secret-canary" not in json.dumps(item)
    assert item["accounting"]["totals"] == []
    assert "unsupported_reference" in item["reasons"]


@pytest.mark.parametrize("kind", ["tool", "workflow", "connector", "model"])
def test_installed_native_thing_reference_has_an_exact_route_without_qualification(kind):
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["runtime"] = [runtime("synny://thing/fixture-operation", kind)]
    service = OperationWorkbench(ReadDatabase(data))
    item = service.catalogue.list(actor)["items"][0]
    assert item["kind"] == kind and item["reference"] == "syn://thing/fixture-operation"
    assert item["state"] == "installed_unqualified"
    contract = service.describe(actor, item["id"])
    assert contract["supported"] and contract["jobType"] == "cog.execute"
    assert contract["executionReference"] == item["reference"]
    assert "worker_check_required" in contract["reasonCodes"]


def test_published_skill_release_does_not_imply_installation_or_execute_text():
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["skills"] = [
        {
            "id": str(uuid4()),
            "slug": "example",
            "owner_agent_id": str(uuid4()),
            "context_object_id": str(uuid4()),
            "source_revision": 4,
            "latest_sha256": "e" * 64,
            "content": "DO NOT EXECUTE private-text-canary",
        }
    ]
    item = project_observations(actor, data)[0]
    assert item["state"] == "source_only" and item["version"] == "e" * 64
    assert item["sources"][0]["revision"] == 4
    assert "skill_is_untrusted_data" in item["reasons"]
    assert "private-text-canary" not in json.dumps(item)


def test_stable_cursor_scope_filters_snapshot_and_detail_consistency():
    actor, data = WorkspaceContext(uuid4(), uuid4()), observations()
    data["runtime"] = [runtime("one"), runtime("two"), runtime("third")]
    service = OperationCatalogue(ReadDatabase(data))
    page = service.list(actor, kind="tool", query="", limit=1)
    second = service.list(actor, kind="tool", limit=1, after=page["nextCursor"])
    assert page["items"][0]["id"] != second["items"][0]["id"]
    detail = service.inspect(actor, page["items"][0]["id"])
    assert detail["snapshotSha256"] == page["snapshotSha256"]
    filtered = service.list(actor, query="third")
    assert filtered["snapshotSha256"] == detail["snapshotSha256"]
    with pytest.raises(RevisionConflictError):
        service.list(
            WorkspaceContext(actor.workspace_id, uuid4()), kind="tool", after=page["nextCursor"]
        )
    with pytest.raises(RevisionConflictError):
        service.list(actor, query="one", kind="tool", after=page["nextCursor"])
    data["runtime"][0]["revision"] += 1
    with pytest.raises(RevisionConflictError):
        service.list(actor, kind="tool", after=page["nextCursor"])


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": True},
        {"limit": 101},
        {"query": "x" * 161},
        {"query": "bad\0"},
        {"kind": "execute"},
        {"state": "runnable"},
        {"after": "not a cursor"},
    ],
)
def test_invalid_filters_and_cursor_are_rejected(options):
    service = OperationCatalogue(ReadDatabase(observations()))
    with pytest.raises(InvalidInputError):
        service.list(WorkspaceContext(uuid4(), uuid4()), **options)


def test_source_bound_never_returns_silently_partial_catalogue():
    data = observations()
    data["runtime"] = [runtime(f"tool-{i}") for i in range(2049)]
    with pytest.raises(CapabilityUnavailableError, match="no partial"):
        project_observations(WorkspaceContext(uuid4(), uuid4()), data)
