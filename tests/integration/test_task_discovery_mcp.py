"""Task-aware MCP evidence uses real Q owners and preserves populated v44.

Only the native MCP peer/worker is the existing explicitly marked fixture.
Reports are produced by the actual gateway, receipts and independent oracles.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from potbot.controller.task_discovery import TaskDiscoveryService
from potbot.controller.task_discovery_ranking import normalize_constraints
from potbot.controller.task_discovery_sources import collect_task_candidates
from potbot.persistence.database import admin_connection, migrate
from potbot.persistence.schema import application_migrations
from scripts.postgres_qualification_fixture import provisioned_app_env
from tests.integration import test_mcp_qualification as native

pytestmark = pytest.mark.postgres
rig = native.rig
runtime_rig = native.runtime_rig
qualification = native.qualification
__all__ = ["rig", "runtime_rig", "qualification"]


@pytest.fixture
def app_env(tmp_path, request, monkeypatch):
    # Only the migration test requests a populated historical prefix. All
    # provisioning, scoped roles and signed owners remain production code.
    version = getattr(request, "param", 45)
    with monkeypatch.context() as patch:
        patch.setattr(
            "scripts.postgres_qualification_fixture.migrate",
            lambda dsn: migrate(dsn, migrations=application_migrations()[:version]),
        )
        with provisioned_app_env(tmp_path) as environment:
            yield environment


def sources(r, task=None):
    return collect_task_candidates(
        r.env.db,
        r.actor,
        task or r.task_binding,
        setup_token=lambda: r.token[0],
        clock=lambda: r.clock[0],
    )


@pytest.mark.parametrize("passed", [True, False], ids=["pass", "failure"])
def test_only_exact_current_task_report_can_contribute_preference(qualification, passed):
    r = qualification
    r.returned_matches = 1 if passed else 2
    completed = native.complete(r)
    catalogue = sources(r)
    candidate = next(item for item in catalogue["items"] if item["kind"] == "mcp")
    assert candidate["taskEvidence"]["status"] == (
        "matching_pass" if passed else "matching_failure"
    )
    assert candidate["taskEvidence"]["reportId"] == completed["reportId"]
    assert candidate["taskEvidence"]["passedCases"] == (2 if passed else 0)
    assert (
        candidate["capabilities"]["inputTypes"] == [] and candidate["capabilities"]["effects"] == []
    )
    service = TaskDiscoveryService(
        r.env.db, setup_token=lambda: r.token[0], clock=lambda: r.clock[0]
    )
    reviewed = service.context(r.actor, r.execution.episode_id, r.execution.state["id"])["context"]
    search = service.create(
        r.actor,
        {
            "episodeId": str(r.execution.episode_id),
            "stateId": str(r.execution.state["id"]),
            "contextSha256": reviewed["contextSha256"],
            "query": "search",
            "constraints": normalize_constraints({"requireTaskEvidence": True}),
            "mode": "lexical",
            "limit": 10,
            "embedding": None,
            "confirm": True,
        },
        "task-report-search",
        setup_token=r.token[0],
    )["search"]
    retained = service.result(r.actor, UUID(search["resultId"]))["result"]
    matching = [item for item in retained["items"] if item["kind"] == "mcp"]
    if passed:
        assert len(matching) == 1 and matching[0]["features"]["evidence"] > 0
        assert matching[0]["executable"] is False and matching[0]["taskPermission"] == "unknown"
    else:
        assert matching == [] and retained["noSuitableCandidate"]
    # Changing only a task binding cannot borrow a matching report from the
    # original task. The real owner still validates the original report first.
    other_binding = {**r.task_binding, "contractSha256": "0" * 64}
    historical = next(item for item in sources(r, other_binding)["items"] if item["kind"] == "mcp")
    assert historical["taskEvidence"]["status"] == "historical"
    assert len(r.calls) == 2


def test_new_task_contract_and_revoked_binding_invalidate_old_shortlist(qualification):
    r = qualification
    native.complete(r)
    service = TaskDiscoveryService(
        r.env.db, setup_token=lambda: r.token[0], clock=lambda: r.clock[0]
    )
    reviewed = service.context(r.actor, r.execution.episode_id, r.execution.state["id"])["context"]
    search = service.create(
        r.actor,
        {
            "episodeId": str(r.execution.episode_id),
            "stateId": str(r.execution.state["id"]),
            "contextSha256": reviewed["contextSha256"],
            "query": "search",
            "constraints": normalize_constraints(),
            "mode": "lexical",
            "limit": 10,
            "embedding": None,
            "confirm": True,
        },
        "current-evidence-search",
        setup_token=r.token[0],
    )["search"]
    result = service.result(r.actor, UUID(search["resultId"]))["result"]
    r.execution.tasks.revise(
        r.actor,
        r.execution.episode_id,
        r.execution.contract_id,
        replace(native.specification(), objective="New task scope"),
        "task-change",
    )
    current = service.result(r.actor, UUID(result["id"]))["result"]
    assert not current["authorityCurrent"] and current["items"] == result["items"]
    newer = service.context(r.actor, r.execution.episode_id, r.execution.state["id"])["context"]
    updated_sources = sources(r, newer["taskBinding"])
    mcp = next(item for item in updated_sources["items"] if item["kind"] == "mcp")
    assert mcp["taskEvidence"]["status"] == "historical"
    binding = r.discovery.service.binding(r.actor, UUID(r.discovery.binding["id"]))["binding"]
    r.discovery.service.control(
        r.actor,
        UUID(binding["id"]),
        "revoke",
        binding["revision"],
        binding["bindingSha256"],
        "revoke-binding",
        setup_token=r.token[0],
    )
    assert not any(item["kind"] == "mcp" for item in sources(r, newer["taskBinding"])["items"])
    assert len(r.calls) == 2


@pytest.mark.parametrize("app_env", [44], indirect=True)
def test_populated_v44_upgrade_preserves_exact_signed_q_evidence(qualification):
    r = qualification
    completed = native.complete(r)
    original = r.service.report(r.actor, UUID(completed["reportId"]))
    assert original["report"]["approvalValid"] and original["report"]["passed"]
    tables = (
        "mcp_discovery_bindings",
        "mcp_discovery_attempts",
        "mcp_discovery_snapshots",
        "mcp_discovery_events",
        "mcp_qualification_suites",
        "mcp_qualification_attempts",
        "mcp_qualification_reports",
        "mcp_qualification_events",
    )
    with r.env.db.transaction(r.actor) as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in tables
        }
    with admin_connection(r.env.admin_dsn, r.env.schema) as conn:
        checksums = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert len(checksums) == 44
    # Preserve this historical 44 -> 45 evidence-retention gate. The selected
    # execution migration has its own populated 45 -> 46 acceptance test.
    revisions = tuple(item for item in application_migrations() if item.version <= 45)
    assert migrate(r.env.admin_dsn, r.env.schema, revisions) == tuple(range(1, 46))
    assert migrate(r.env.admin_dsn, r.env.schema, revisions) == tuple(range(1, 46))
    with r.env.db.transaction(r.actor) as conn:
        for table, rows in before.items():
            assert conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        for table in (
            "task_discovery_searches",
            "task_discovery_results",
            "task_discovery_selections",
            "task_discovery_events",
        ):
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == []
    with admin_connection(r.env.admin_dsn, r.env.schema) as conn:
        assert (
            conn.execute(
                "SELECT * FROM schema_migrations WHERE version<=44 ORDER BY version"
            ).fetchall()
            == checksums
        )
    assert r.service.report(r.actor, UUID(completed["reportId"])) == original
    assert (
        next(item for item in sources(r)["items"] if item["kind"] == "mcp")["taskEvidence"][
            "status"
        ]
        == "matching_pass"
    )
