"""Sealed source metadata through real Syn, artifact and discovery owners.

These fixture declarations qualify metadata provenance only. They do not claim
that an installed native worker, hosted provider or a model has been qualified.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from potbot.controller.operation_catalogue import OperationCatalogue
from potbot.controller.operation_discovery_metadata import OperationDiscoveryMetadata
from potbot.controller.task_discovery import TaskDiscoveryService
from potbot.controller.task_discovery_ranking import normalize_constraints
from potbot.controller.task_discovery_sources import (
    collect_task_candidates,
    inspect_task_candidate,
)
from potbot.domain.errors import RevisionConflictError
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.semantics.service import SynService
from potbot.tools.mcp_qualification_host import read_mcp_task_binding
from tests.integration.test_execution_gateway import setup, specification
from tests.unit.test_operation_discovery_metadata import INPUT, OUTPUT, REFERENCE, cog_snapshot

pytestmark = pytest.mark.postgres
TOKEN = "task-metadata-fixture-" + "s" * 48
CAPABILITY = "cog.execute:syn://thing/retained-file-inspection"


@pytest.fixture
def metadata_rig(app_env, tmp_path, monkeypatch):
    monkeypatch.delenv("POTBOT_SYN_ARTIFACT_ROOT", raising=False)
    execution = setup(app_env, tmp_path)
    spec = specification()
    revised = execution.tasks.revise(
        app_env.context,
        execution.episode_id,
        execution.contract_id,
        replace(spec, capabilities=(*spec.capabilities, CAPABILITY)),
        "metadata-capability",
    )
    execution.contract_id = UUID(revised["current_contract"]["id"])
    syn = SynService(app_env.db, execution.artifacts)
    imported = syn.import_snapshot(
        app_env.context, app_env.repository_id, "metadata-fixture", cog_snapshot()
    )
    task = read_mcp_task_binding(
        app_env.db,
        app_env.context,
        episode_id=execution.episode_id,
        repository_state_id=execution.state["id"],
    )
    item = next(
        value
        for value in OperationCatalogue(app_env.db)._snapshot(app_env.context)
        if value["reference"] == REFERENCE
    )
    return SimpleNamespace(
        env=app_env,
        execution=execution,
        syn=syn,
        imported=imported,
        task=task,
        item=item,
        reader=OperationDiscoveryMetadata(
            app_env.db, app_env.context, artifacts=execution.artifacts
        ),
    )


def collect(r):
    return collect_task_candidates(
        r.env.db,
        r.env.context,
        r.task,
        setup_token=lambda: TOKEN,
        artifacts=r.execution.artifacts,
    )


def selected(r):
    return next(row for row in collect(r)["items"] if row["ownerId"] == r.item["id"])


def counts(r):
    with r.env.db.transaction(r.env.context) as connection:
        return {
            table: connection.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
            for table in (
                "artifacts",
                "embedding_sources",
                "embedding_source_heads",
                "syn_snapshots",
            )
        }


def test_actual_syn_and_artifact_owners_enrich_shortlist_without_mutating_source_catalogue(
    metadata_rig,
):
    r = metadata_rig
    before = counts(r)
    candidate = selected(r)
    assert candidate["description"] == "Inspect retained repository files and source paths."
    assert candidate["capabilities"]["inputTypes"] == [INPUT]
    assert candidate["capabilities"]["outputTypes"] == [OUTPUT]
    assert candidate["requiredTaskCapability"] == CAPABILITY
    assert candidate["authorityCurrent"] and not candidate["installed"]
    detail = inspect_task_candidate(
        r.env.db,
        r.env.context,
        candidate,
        setup_token=lambda: TOKEN,
        artifacts=r.execution.artifacts,
    )["detail"]
    assert (
        detail["operationMetadata"]["provenance"][0]["snapshotId"]
        == r.imported["scope"]["snapshot_id"]
    )
    assert "description" not in detail["item"]
    assert counts(r) == before


def test_selected_cog_retains_metadata_and_rejects_new_conflicting_owner_head(metadata_rig):
    r = metadata_rig
    discovery = TaskDiscoveryService(
        r.env.db, setup_token=lambda: TOKEN, artifacts=r.execution.artifacts
    )
    context = discovery.context(r.env.context, r.execution.episode_id, r.execution.state["id"])[
        "context"
    ]
    search = discovery.create(
        r.env.context,
        {
            "episodeId": str(r.execution.episode_id),
            "stateId": str(r.execution.state["id"]),
            "contextSha256": context["contextSha256"],
            "query": "Inspect retained repository files",
            "constraints": normalize_constraints({"inputTypes": [INPUT], "outputTypes": [OUTPUT]}),
            "mode": "lexical",
            "limit": 10,
            "embedding": None,
            "confirm": True,
        },
        "metadata-search",
        setup_token=TOKEN,
    )["search"]
    result = discovery.result(r.env.context, UUID(search["resultId"]))["result"]
    candidate = next(row for row in result["items"] if row["ownerId"] == r.item["id"])
    assert candidate["typeFit"] == "exact" and candidate["taskPermission"] == "permitted"
    assert candidate["executable"] is False
    selection = discovery.select(
        r.env.context,
        UUID(search["id"]),
        search["revision"],
        search["bindingSha256"],
        candidate["id"],
        "metadata-select",
        setup_token=TOKEN,
    )["selection"]
    assert selection["authorityCurrent"]
    r.syn.import_snapshot(
        r.env.context,
        r.env.repository_id,
        "conflicting-owner",
        cog_snapshot(description="A different declared operation."),
    )
    value = discovery.selection(r.env.context, UUID(selection["id"]))["selection"]
    assert not value["authorityCurrent"]
    unknown = selected(r)
    assert "syn_cog_metadata_conflicting" in unknown["reasonCodes"]
    assert unknown["description"] == "" and unknown["capabilities"]["inputTypes"] == []


def test_tampered_complete_source_artifact_removes_metadata_and_invalidates_lazy_inspection(
    metadata_rig,
):
    r = metadata_rig
    candidate = selected(r)
    artifact = r.execution.artifacts.metadata(
        r.env.context, UUID(r.imported["snapshot"]["artifact_id"])
    )
    path = r.env.artifact_root / artifact["locator"]
    original = path.read_bytes()
    try:
        path.write_bytes(
            original.replace(b"explicit metadata fixture", b"replaced metadata fixture", 1)
        )
        unknown = selected(r)
        assert "syn_cog_metadata_unavailable" in unknown["reasonCodes"]
        assert unknown["description"] == "" and unknown["capabilities"]["inputTypes"] == []
        with pytest.raises(RevisionConflictError, match="changed"):
            inspect_task_candidate(
                r.env.db,
                r.env.context,
                candidate,
                artifacts=r.execution.artifacts,
                setup_token=lambda: TOKEN,
            )
    finally:
        path.write_bytes(original)
    assert selected(r)["ownerSha256"] == candidate["ownerSha256"]


def test_other_workspace_cannot_resolve_retained_metadata_using_foreign_snapshot_pointers(
    metadata_rig,
):
    r = metadata_rig
    other_artifacts = ArtifactStore(r.env.other_db, LocalArtifactBackend(r.env.artifact_root))
    reader = OperationDiscoveryMetadata(
        r.env.other_db, r.env.other_context, artifacts=other_artifacts
    )
    value = reader.for_operation(r.item)
    assert value["reasonCodes"] == ["syn_cog_metadata_unavailable"]
    assert value["description"] == "" and value["provenance"] == []
    # A caller-provided workspace value also cannot replace the restricted
    # PostgreSQL principal's actual membership.
    wrong_scope = OperationDiscoveryMetadata(
        r.env.other_db, r.env.context, artifacts=other_artifacts
    ).for_operation(r.item)
    assert wrong_scope["reasonCodes"] == ["syn_cog_metadata_unavailable"]


def test_default_root_never_guesses_an_alternate_syn_cli_artifact_directory(
    metadata_rig, monkeypatch
):
    r = metadata_rig
    monkeypatch.setenv("POTBOT_NATIVE_COG_CONFIG", "/private/unrelated/native-profile.json")
    before = counts(r)
    configured = r.env.artifact_root.parent
    assert configured.name == "artifacts" and r.env.artifact_root.name == "objects"
    directory_entries = sorted(path.name for path in configured.iterdir())
    value = OperationDiscoveryMetadata(r.env.db, r.env.context).for_operation(r.item)
    assert value["reasonCodes"] == ["syn_cog_metadata_unavailable"]
    assert counts(r) == before
    assert sorted(path.name for path in configured.iterdir()) == directory_entries
    assert r.reader.for_operation(r.item)["source"] == "sealed_syn_declaration"


def test_snapshot_metadata_binding_does_not_treat_task_semantic_release_as_syn_snapshot(
    metadata_rig,
):
    r = metadata_rig
    assert r.task["semanticReleaseId"] != r.imported["scope"]["snapshot_id"]
    candidate = selected(r)
    assert candidate["description"]
    result = collect(r)
    assert result["typesSha256"] and result["catalogueSha256"]


def test_explicit_operator_artifact_configuration_reaches_real_discovery_without_dependency_injection(
    metadata_rig, monkeypatch
):
    r = metadata_rig
    expected = selected(r)
    monkeypatch.setenv("POTBOT_SYN_ARTIFACT_ROOT", str(r.env.artifact_root))
    catalogue = collect_task_candidates(r.env.db, r.env.context, r.task, setup_token=lambda: TOKEN)
    actual = next(row for row in catalogue["items"] if row["ownerId"] == r.item["id"])
    assert actual == expected
    discovery = TaskDiscoveryService(r.env.db, setup_token=lambda: TOKEN)
    current = discovery.context(r.env.context, r.execution.episode_id, r.execution.state["id"])[
        "context"
    ]
    assert current["catalogueSha256"] == catalogue["catalogueSha256"]
    detail = inspect_task_candidate(r.env.db, r.env.context, actual, setup_token=lambda: TOKEN)
    assert detail["detail"]["operationMetadata"]["source"] == "sealed_syn_declaration"


def test_operator_artifact_configuration_cannot_escape_registered_workspace(
    metadata_rig, monkeypatch, tmp_path
):
    r = metadata_rig
    outside = tmp_path / "unrelated-artifacts"
    outside.mkdir()
    before = counts(r)
    monkeypatch.setenv("POTBOT_SYN_ARTIFACT_ROOT", str(outside))
    reader = OperationDiscoveryMetadata(r.env.db, r.env.context)
    value = reader.for_operation(r.item)
    assert value["reasonCodes"] == ["syn_cog_metadata_unavailable"]
    assert value["description"] == "" and value["provenance"] == []
    assert list(outside.iterdir()) == [] and counts(r) == before
    # A different workspace cannot borrow the first workspace's valid path.
    monkeypatch.setenv("POTBOT_SYN_ARTIFACT_ROOT", str(r.env.artifact_root))
    other = OperationDiscoveryMetadata(r.env.other_db, r.env.other_context).for_operation(r.item)
    assert other["reasonCodes"] == ["syn_cog_metadata_unavailable"]
