"""Real owner/SQL/HTTP task discovery, with explicitly synthetic local vectors.

The registry transport is a local fixture; all signatures, task bindings,
source currentness, database scopes, budget accounting and routes are genuine.
No provider quality or live native isolation is inferred from these tests.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from potbot.api.wire import OperationSemantics, error_response
from potbot.controller.task_discovery import (
    TaskDiscoveryService,
    embedding_documents,
    refinement_claim_sha256,
)
from potbot.controller.task_discovery_ranking import normalize_constraints
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.tasks import TaskStatus
from potbot.memory.embeddings import EmbeddingService
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import provision_principal
from tests.integration import test_discovery as registry
from tests.integration.test_execution_gateway import setup, specification
from tests.integration.test_transient_embeddings import SyntheticTransientProvider

pytestmark = pytest.mark.postgres
rig = registry.rig
runtime_rig = registry.runtime_rig
__all__ = ["rig", "runtime_rig"]


@pytest.fixture
def task_rig(rig, tmp_path):
    r = rig
    candidate = registry.admit(r)
    execution = setup(r.env, tmp_path)
    contract = replace(
        specification(),
        budget=(("tokens", 100_000), ("calls", 100), ("tool_ms", 1_000_000), ("concurrency", 4)),
    )
    revised = execution.tasks.revise(
        r.actor, execution.episode_id, execution.contract_id, contract, "discovery-budget"
    )
    execution.contract_id = UUID(revised["current_contract"]["id"])
    execution.task = revised
    service = TaskDiscoveryService(
        r.env.db, setup_token=lambda: r.token[0], clock=lambda: r.clock[0]
    )
    return SimpleNamespace(
        registry=r,
        env=r.env,
        actor=r.actor,
        token=r.token,
        service=service,
        execution=execution,
        candidate=candidate,
        contract=contract,
    )


def context(r):
    return r.service.context(r.actor, r.execution.episode_id, r.execution.state["id"])["context"]


def body(r, *, mode="lexical", constraints=None, embedding=None, query="inspect file"):
    return {
        "episodeId": str(r.execution.episode_id),
        "stateId": str(r.execution.state["id"]),
        "contextSha256": context(r)["contextSha256"],
        "query": query,
        "constraints": normalize_constraints(constraints),
        "mode": mode,
        "limit": 10,
        "embedding": embedding,
        "confirm": True,
    }


def create(r, *, key="create", **changes):
    return r.service.create(r.actor, body(r, **changes), key, setup_token=r.token[0])["search"]


def result(r, search):
    return r.service.result(r.actor, UUID(search["resultId"]))["result"]


def select(r, search, *, key="select", candidate_id=None):
    candidate_id = candidate_id or result(r, search)["items"][0]["id"]
    return r.service.select(
        r.actor,
        UUID(search["id"]),
        search["revision"],
        search["bindingSha256"],
        candidate_id,
        key,
        setup_token=r.token[0],
    )["selection"]


def embedding(r):
    provider = SyntheticTransientProvider()
    owner = EmbeddingService(
        r.env.db, ArtifactStore(r.env.db, LocalArtifactBackend(r.env.artifact_root)), provider
    )
    space = owner.register_space(r.actor)["id"]
    return (
        owner,
        provider,
        {
            "spaceId": str(space),
            "embeddingName": "general_text",
            "profileSha256": provider.profile.fingerprint,
            "maxCandidates": 16,
            "runtimeBudgetMs": 60_000,
        },
    )


def claim(r, search, *, key="native-claim"):
    return r.service.claim(
        r.actor,
        UUID(search["id"]),
        search["revision"],
        search["bindingSha256"],
        key,
        setup_token=r.token[0],
        fresh_only=True,
    )


def compute(r, search, owner, claimed):
    return owner.rank_transient(
        r.actor,
        r.execution.episode_id,
        UUID(search["embedding"]["spaceId"]),
        search["query"],
        embedding_documents(claimed["semanticCandidates"]),
        repository_id=r.env.repository_id,
        request_binding_sha256=refinement_claim_sha256(
            r.actor, UUID(search["id"]), UUID(claimed["workerToken"])
        ),
        cancelled=lambda: (
            not bool(
                r.service.validate_claim(r.actor, UUID(search["id"]), UUID(claimed["workerToken"]))
            )
        ),
    )


def table_counts(r):
    with r.env.db.transaction(r.actor) as conn:
        return {
            table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "task_discovery_searches",
                "task_discovery_results",
                "task_discovery_selections",
                "task_discovery_events",
                "embedding_sources",
                "embedding_vectors",
                "artifacts",
                "budget_reservations",
                "action_runs",
                "command_results",
            )
        }


def test_read_only_context_and_typed_constraints_do_not_infer_missing_ports(task_rig):
    r = task_rig
    before, calls = table_counts(r), list(r.registry.client.calls)
    reviewed = context(r)
    assert reviewed == context(r)
    assert reviewed["taskStatus"] == "running" and reviewed["taskCapabilities"] == ["test"]
    assert reviewed["counts"]["registry"] == 1
    assert r.service.searches(r.actor, episode_id=r.execution.episode_id)["items"] == []
    assert table_counts(r) == before and r.registry.client.calls == calls
    constrained = create(r, constraints={"inputTypes": ["syn://type/document"]})
    retained = result(r, constrained)
    assert retained["noSuitableCandidate"] and retained["items"] == []
    assert retained["constraints"]["inputTypes"] == ["syn://type/document"]
    assert retained["excluded"] and "select" not in constrained["actions"]
    with pytest.raises(InvalidInputError):
        create(
            r,
            key="unknown-type",
            constraints={"inputTypes": ["synny://type/unknown-discovery-type"]},
        )


def test_exact_lexical_handoff_inspection_and_command_replay(task_rig):
    r = task_rig
    request = body(r)
    search = r.service.create(r.actor, request, "once", setup_token=r.token[0])["search"]
    assert (
        search["status"] == "completed" and search["approvalValid"] and search["authorityCurrent"]
    )
    assert search["revision"] == 2 and search["actions"] == ["select"]
    retained = result(r, search)
    candidates = [item for item in retained["items"] if item["kind"] == "registry"]
    assert len(candidates) == 1 and candidates[0]["ownerId"] == r.candidate["id"]
    assert all(item["executable"] is False for item in retained["items"])
    assert retained["semantic"] is None
    inspected = r.service.inspect_candidate(r.actor, UUID(search["id"]), candidates[0]["id"])
    assert inspected["candidate"] == candidates[0]
    assert inspected["detail"]["candidate"]["id"] == r.candidate["id"]
    before = table_counts(r)
    selected = select(r, search, candidate_id=candidates[0]["id"])
    assert selected["authorityCurrent"] and selected["approvalValid"]
    assert selected["handoff"] == {
        "kind": "registry",
        "identity": r.candidate["id"],
        "bindingId": None,
    }
    assert select(r, search, candidate_id=candidates[0]["id"]) == selected
    assert r.service.create(r.actor, request, "once", setup_token=r.token[0])["search"] == search
    current = r.service.search(r.actor, UUID(search["id"]))["search"]
    assert current["status"] == "selected" and current["selectionId"] == selected["id"]
    after = table_counts(r)
    assert after["task_discovery_selections"] == before["task_discovery_selections"] + 1
    assert all(
        after[name] == before[name]
        for name in (
            "action_runs",
            "artifacts",
            "embedding_sources",
            "embedding_vectors",
            "budget_reservations",
        )
    )
    with pytest.raises(RevisionConflictError):
        r.service.create(
            r.actor, {**request, "query": "another request"}, "once", setup_token=r.token[0]
        )
    for table, identity in (
        ("task_discovery_results", search["resultId"]),
        ("task_discovery_selections", selected["id"]),
        ("task_discovery_events", None),
    ):
        with (
            pytest.raises((ScopeDeniedError, RevisionConflictError)),
            r.env.db.transaction(r.actor) as conn,
        ):
            conn.execute(
                f"DELETE FROM {table}" + (" WHERE id=%s" if identity else ""),
                (UUID(identity),) if identity else (),
            )


def test_same_workspace_other_principal_cannot_read_search_results_or_receipts(task_rig):
    r = task_rig
    search = create(r)
    selected = select(r, search)
    role = urlsplit(r.env.other_db.dsn).username
    other = provision_principal(r.env.admin_dsn, r.env.schema, role, r.actor.workspace_id)
    service = TaskDiscoveryService(
        r.env.other_db, setup_token=lambda: r.token[0], clock=lambda: r.registry.clock[0]
    )
    assert service.searches(other)["items"] == []
    for method, identity in (
        (service.search, search["id"]),
        (service.result, search["resultId"]),
        (service.selection, selected["id"]),
    ):
        with pytest.raises(ScopeDeniedError):
            method(other, UUID(identity))
    with r.env.other_db.transaction(other) as conn:
        for table in (
            "task_discovery_searches",
            "task_discovery_results",
            "task_discovery_selections",
            "task_discovery_events",
        ):
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == []
        assert (
            conn.execute(
                "SELECT * FROM command_results WHERE operation LIKE 'task.discovery.%'"
            ).fetchall()
            == []
        )
    with pytest.raises(ScopeDeniedError):
        r.service.search(r.env.other_context, UUID(search["id"]))


@pytest.mark.parametrize(
    "change", ["source", "publisher", "contract", "task", "consumer", "key"], ids=str
)
def test_drift_preserves_history_and_blocks_fresh_selection(task_rig, monkeypatch, change):
    r = task_rig
    search = create(r)
    retained = result(r, search)
    if change == "source":
        registry.control(r.registry, "source", r.registry.source, "disable")
    elif change == "publisher":
        registry.control(r.registry, "publisher", r.registry.publisher, "block")
    elif change == "contract":
        r.execution.tasks.revise(
            r.actor,
            r.execution.episode_id,
            r.execution.contract_id,
            replace(r.contract, objective="Changed task objective"),
            "revise-task",
        )
    elif change == "task":
        r.execution.tasks.transition(
            r.actor,
            r.execution.episode_id,
            r.execution.task["update_token"],
            TaskStatus.PAUSED,
            "Pause discovery",
            idempotency_key="pause",
        )
    elif change == "consumer":
        monkeypatch.setattr(
            "potbot.controller.task_discovery.discovery_consumer_sha256", lambda: "0" * 64
        )
    else:
        r.token[0] = "rotated-setup-capability-" + "z" * 48
    current = r.service.search(r.actor, UUID(search["id"]))["search"]
    assert not current["authorityCurrent"] and "select" not in current["actions"]
    historical = result(r, search)
    assert not historical["authorityCurrent"] and historical["items"] == retained["items"]
    with pytest.raises(ScopeDeniedError):
        select(r, search)
    assert table_counts(r)["task_discovery_selections"] == 0


def test_hybrid_native_claim_budgeted_completion_and_replay(task_rig):
    r = task_rig
    owner, provider, profile = embedding(r)
    initial = table_counts(r)
    search = create(r, mode="hybrid", embedding=profile)
    assert search["status"] == "requested" and search["resultId"] is None
    assert (
        provider.calls == []
        and table_counts(r)["budget_reservations"] == initial["budget_reservations"]
    )
    claimed = claim(r, search)
    report = compute(r, search, owner, claimed)
    completed = r.service.complete(
        r.actor, UUID(search["id"]), UUID(claimed["workerToken"]), report
    )["search"]
    retained = result(r, completed)
    assert retained["authorityCurrent"] and retained["semantic"]["usageOrigin"] == "synthetic"
    assert retained["semantic"]["calls"] == len(provider.calls)
    assert any(item["features"]["semantic"] > 0 for item in retained["items"])
    usage = owner.budgets.inspect(r.actor, r.execution.episode_id)
    assert usage["used"]["calls"] == len(provider.calls) and usage["held"] == {}
    assert table_counts(r)["embedding_sources"] == initial["embedding_sources"]
    assert table_counts(r)["embedding_vectors"] == initial["embedding_vectors"]
    assert table_counts(r)["artifacts"] == initial["artifacts"]
    prior_calls = len(provider.calls)
    replay = r.service.execution_replay(
        r.actor,
        UUID(search["id"]),
        search["revision"],
        search["bindingSha256"],
        "native-claim",
        setup_token=r.token[0],
    )
    assert replay["search"] == completed
    assert claim(r, search)["replay"]["search"] == completed
    assert len(provider.calls) == prior_calls
    assert select(r, completed)["authorityCurrent"]


@pytest.mark.parametrize(
    "tamper", ["query", "documents", "candidate", "score", "usage", "reservation", "extra"], ids=str
)
def test_native_result_must_match_frozen_inputs_and_actual_task_usage(task_rig, tamper):
    r = task_rig
    owner, _provider, profile = embedding(r)
    search = create(r, mode="hybrid", embedding=profile)
    claimed = claim(r, search)
    actual = compute(r, search, owner, claimed)
    forged = deepcopy(actual)
    if tamper in {"query", "documents"}:
        forged[tamper + "Sha256"] = "0" * 64
    elif tamper == "candidate":
        forged["scores"][0]["id"] = "0" * 64
    elif tamper == "score":
        forged["scores"][0]["scoreMicros"] = 0
    elif tamper == "usage":
        forged["inputTokens"] += 1
    elif tamper == "reservation":
        forged["reservationIds"] = [str(uuid4())]
    else:
        forged["passed"] = True
    with pytest.raises((InvalidInputError, ScopeDeniedError, StateMismatchError)):
        r.service.complete(r.actor, UUID(search["id"]), UUID(claimed["workerToken"]), forged)
    assert table_counts(r)["task_discovery_results"] == 0
    completed = r.service.complete(
        r.actor, UUID(search["id"]), UUID(claimed["workerToken"]), actual
    )["search"]
    assert completed["status"] == "completed"


def test_cancellation_fences_native_work_and_tainted_admission_is_not_laundered(task_rig):
    r = task_rig
    owner, provider, profile = embedding(r)
    search = create(r, mode="hybrid", embedding=profile)
    claimed = claim(r, search)
    current = r.service.search(r.actor, UUID(search["id"]))["search"]
    cancelled = r.service.cancel(
        r.actor,
        UUID(search["id"]),
        current["revision"],
        current["bindingSha256"],
        "cancel",
        setup_token=r.token[0],
    )["search"]
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ScopeDeniedError):
        r.service.validate_claim(r.actor, UUID(search["id"]), UUID(claimed["workerToken"]))
    assert (
        provider.calls == []
        and owner.budgets.inspect(r.actor, r.execution.episode_id)["held"] == {}
    )
    forged = create(r, key="tainted", mode="hybrid", embedding=profile)
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE task_discovery_searches SET mac=%s WHERE id=%s", ("0" * 64, UUID(forged["id"]))
        )
    invalid = r.service.search(r.actor, UUID(forged["id"]))["search"]
    assert not invalid["approvalValid"] and invalid["actions"] == ["cancel"]
    retired = r.service.cancel(
        r.actor,
        UUID(invalid["id"]),
        invalid["revision"],
        invalid["bindingSha256"],
        "retire",
        setup_token=r.token[0],
    )["search"]
    assert (
        retired["status"] == "indeterminate"
        and not retired["approvalValid"]
        and not retired["authorityCurrent"]
    )
    assert retired["resultId"] is None and retired["selectionId"] is None


def test_concurrent_native_claim_has_one_winner_and_same_key_recovers(task_rig):
    r = task_rig
    _owner, _provider, profile = embedding(r)
    search = create(r, mode="hybrid", embedding=profile)

    def attempt(key):
        try:
            return claim(r, search, key=key)
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ["claim-a", "claim-b"]))
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
    winner = "claim-a" if isinstance(outcomes[0], dict) else "claim-b"
    assert claim(r, search, key=winner)["replay"]["search"]["status"] == "running"
    assert table_counts(r)["budget_reservations"] == 0


def test_rolled_back_active_rows_can_be_retired_beyond_the_immutable_event_head(task_rig):
    r = task_rig
    _owner, provider, profile = embedding(r)
    search = create(r, mode="hybrid", embedding=profile)
    with r.env.db.transaction(r.actor) as conn:
        original = dict(
            conn.execute(
                "SELECT * FROM task_discovery_searches WHERE id=%s", (UUID(search["id"]),)
            ).fetchone()
        )
    claimed = claim(r, search)
    with r.env.db.transaction(r.actor) as conn:
        running = dict(
            conn.execute(
                "SELECT * FROM task_discovery_searches WHERE id=%s", (UUID(search["id"]),)
            ).fetchone()
        )

    def rollback(row):
        with r.env.db.transaction(r.actor) as conn:
            conn.execute(
                "UPDATE task_discovery_searches SET revision=%s,status=%s,payload=%s,worker_token=%s,mac=%s WHERE id=%s",
                (
                    row["revision"],
                    row["status"],
                    Jsonb(row["payload"]),
                    row["worker_token"],
                    row["mac"],
                    row["id"],
                ),
            )

    rollback(original)
    observed = r.service.search(r.actor, UUID(search["id"]))["search"]
    assert not observed["approvalValid"] and observed["actions"] == ["cancel"]
    retired = r.service.cancel(
        r.actor,
        UUID(search["id"]),
        observed["revision"],
        observed["bindingSha256"],
        "retire-rollback",
        setup_token=r.token[0],
    )["search"]
    assert (
        retired["revision"] == 3
        and retired["status"] == "indeterminate"
        and not retired["approvalValid"]
    )
    rollback(running)
    failed = r.service.fail(
        r.actor, UUID(search["id"]), UUID(claimed["workerToken"]), "search_failed"
    )["search"]
    assert (
        failed["revision"] == 4
        and failed["status"] == "indeterminate"
        and not failed["approvalValid"]
    )
    assert provider.calls == [] and table_counts(r)["task_discovery_results"] == 0


def test_native_evidence_cannot_be_reused_for_another_same_input_claim(task_rig):
    r = task_rig
    owner, _provider, profile = embedding(r)
    first = create(r, key="first", mode="hybrid", embedding=profile)
    second = create(r, key="second", mode="hybrid", embedding=profile)
    first_claim = claim(r, first, key="first-claim")
    second_claim = claim(r, second, key="second-claim")
    report = compute(r, first, owner, first_claim)
    # Display timestamps are not native authority. Even a rewritten timestamp
    # cannot attach another claim's matching query/documents/usage to this one.
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE task_discovery_searches SET updated_at='2000-01-01T00:00:00Z' WHERE id=%s",
            (UUID(second["id"]),),
        )
    with pytest.raises(ScopeDeniedError):
        r.service.complete(r.actor, UUID(second["id"]), UUID(second_claim["workerToken"]), report)
    assert table_counts(r)["task_discovery_results"] == 0
    completed = r.service.complete(
        r.actor, UUID(first["id"]), UUID(first_claim["workerToken"]), report
    )["search"]
    assert result(r, completed)["authorityCurrent"]
    second_report = compute(r, second, owner, second_claim)
    assert second_report["reservationIds"] != report["reservationIds"]
    assert (
        r.service.complete(
            r.actor, UUID(second["id"]), UUID(second_claim["workerToken"]), second_report
        )["search"]["status"]
        == "completed"
    )


def test_verified_acquired_skill_is_ranked_by_manifest_and_fenced_by_owner_control(task_rig):
    from potbot.context.skill_acquisition import SkillAcquisition
    from potbot.context.skill_packages import package_from_files

    r = task_rig
    registry_owner = r.registry
    registry_owner.client.package = package_from_files(
        registry_owner.client.package.files,
        source={**registry_owner.client.package.source, "verification": "content-integrity-only"},
    )
    reviewed = registry.review(registry_owner, candidate=r.candidate)
    packages = SkillAcquisition(r.env.db, setup_token_resolver=lambda: r.token[0])
    package_id = UUID(reviewed["packageId"])
    plan = packages.plan(r.actor, package_id)["plan"]
    installed = packages.install(
        r.actor,
        package_id,
        plan["sha256"],
        plan["expectedRevision"],
        "install-task-skill",
        setup_token=r.token[0],
    )["skill"]
    active = packages.control(
        r.actor,
        UUID(installed["id"]),
        "activate",
        installed["revision"],
        "activate-task-skill",
        setup_token=r.token[0],
    )["skill"]
    search = create(r, query="retained fixture reference")
    retained = result(r, search)
    matches = [item for item in retained["items"] if item["kind"] == "operation"]
    assert len(matches) == 1 and matches[0]["installed"] and matches[0]["authorityCurrent"]
    assert matches[0]["description"] == "Read a retained fixture reference"
    inspected = r.service.inspect_candidate(r.actor, UUID(search["id"]), matches[0]["id"])
    assert inspected["candidate"] == matches[0]
    selected = select(r, search, candidate_id=matches[0]["id"])
    assert selected["handoff"]["kind"] == "operation"
    packages.control(
        r.actor,
        UUID(active["id"]),
        "disable",
        active["revision"],
        "disable-task-skill",
        setup_token=r.token[0],
    )
    assert not r.service.selection(r.actor, UUID(selected["id"]))["selection"]["authorityCurrent"]
    assert not result(r, search)["authorityCurrent"]


def test_empty_hybrid_pool_completes_without_native_work(task_rig):
    r = task_rig
    _owner, provider, profile = embedding(r)
    search = create(r, mode="hybrid", embedding=profile, constraints={"requireTaskEvidence": True})
    retained = result(r, search)
    assert search["status"] == "completed" and retained["noSuitableCandidate"]
    assert retained["semantic"] is None and provider.calls == []
    assert table_counts(r)["budget_reservations"] == 0


def test_actual_http_lifecycle_exports_task_discovery_contract(task_rig):
    from potbot.api.ui_task_discovery import register_task_discovery_routes

    r = task_rig
    app, origin = FastAPI(), "http://127.0.0.1:3100"
    reads = {"Authorization": "Bearer task-discovery-fixture"}
    writes = {**reads, "Origin": origin, "X-Potbot-Setup-Token": r.token[0]}

    def authenticate(value):
        if value != reads["Authorization"]:
            raise ScopeDeniedError("Unknown fixture identity")
        return r.actor

    def authorize(value):
        if value != r.token[0]:
            raise ScopeDeniedError("Missing fixture setup authority")

    @app.exception_handler(DomainError)
    async def failed(request, error):
        response = error_response(
            error,
            semantics=OperationSemantics(
                read_only=request.method == "GET",
                idempotency_key=getattr(request.state, "idempotency_key", None) is not None,
            ),
        )
        return JSONResponse(response.body, status_code=response.status)

    register_task_discovery_routes(
        app, r.env.db, authenticate, (origin,), r.actor.workspace_id, authorize, service=r.service
    )
    fixture = {"requests": {}}
    prefix = "/v1/ui/task-discovery"
    with TestClient(app) as client:

        def get(suffix):
            response = client.get(prefix + suffix, headers=reads)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            return response.json()

        def post(suffix, value, key):
            response = client.post(
                prefix + suffix, headers={**writes, "Idempotency-Key": key}, json=value
            )
            assert response.status_code == 200, response.text
            return response.json()

        fixture["context"] = get(
            f"/context?episodeId={r.execution.episode_id}&stateId={r.execution.state['id']}"
        )
        fixture["emptySearches"] = get("/searches")
        fixture["requests"]["create"] = body(r)
        fixture["createdSearch"] = post("/searches", fixture["requests"]["create"], "http-create")
        assert (
            post("/searches", fixture["requests"]["create"], "http-create")
            == fixture["createdSearch"]
        )
        search = fixture["createdSearch"]["search"]
        fixture["search"] = get("/searches/" + search["id"])
        fixture["result"] = get("/results/" + search["resultId"])
        candidate = fixture["result"]["result"]["items"][0]
        fixture["inspection"] = get(f"/searches/{search['id']}/candidates/{candidate['id']}")
        selected_body = {
            "revision": search["revision"],
            "digest": search["bindingSha256"],
            "candidateId": candidate["id"],
            "confirm": True,
        }
        fixture["requests"]["select"] = selected_body
        fixture["selected"] = post(f"/searches/{search['id']}/select", selected_body, "http-select")
        assert (
            post(f"/searches/{search['id']}/select", selected_body, "http-select")
            == fixture["selected"]
        )
        fixture["selection"] = get("/selections/" + fixture["selected"]["selection"]["id"])
        fixture["selectedSearch"] = get("/searches/" + search["id"])
        owner, provider, profile = embedding(r)
        fixture["hybridContext"] = get(
            f"/context?episodeId={r.execution.episode_id}&stateId={r.execution.state['id']}"
        )
        fixture["requests"]["hybrid"] = body(r, mode="hybrid", embedding=profile)
        fixture["requestedSearch"] = post("/searches", fixture["requests"]["hybrid"], "http-hybrid")
        pending = fixture["requestedSearch"]["search"]
        claimed = claim(r, pending, key="http-native")
        fixture["runningSearch"] = get("/searches/" + pending["id"])
        native = compute(r, pending, owner, claimed)
        r.service.complete(r.actor, UUID(pending["id"]), UUID(claimed["workerToken"]), native)
        fixture["hybridSearch"] = get("/searches/" + pending["id"])
        fixture["hybridResult"] = get("/results/" + fixture["hybridSearch"]["search"]["resultId"])
        waiting = post("/searches", fixture["requests"]["hybrid"], "http-cancel-request")["search"]
        fixture["requests"]["cancel"] = {
            "revision": waiting["revision"],
            "digest": waiting["bindingSha256"],
            "confirm": True,
        }
        fixture["cancelledSearch"] = post(
            f"/searches/{waiting['id']}/cancel", fixture["requests"]["cancel"], "http-cancel"
        )
        fixture["searches"] = get("/searches?episodeId=" + str(r.execution.episode_id))
        fixture["firstPage"] = get("/searches?limit=1")
        fixture["secondPage"] = get("/searches?limit=1&after=" + fixture["firstPage"]["nextCursor"])
        registry.control(r.registry, "source", r.registry.source, "disable")
        fixture["staleSearch"] = get("/searches/" + search["id"])
        fixture["staleResult"] = get("/results/" + search["resultId"])
        fixture["staleSelection"] = get("/selections/" + fixture["selected"]["selection"]["id"])
        assert not fixture["staleSelection"]["selection"]["authorityCurrent"]
        assert client.get(prefix + "/searches").status_code != 200
        assert (
            client.post(
                prefix + "/searches", headers=reads, json=fixture["requests"]["create"]
            ).status_code
            != 200
        )
        assert (
            client.post(
                prefix + f"/searches/{pending['id']}/complete", headers=writes, json=native
            ).status_code
            != 200
        )
        assert (
            client.post(
                prefix + "/searches",
                headers={**writes, "Idempotency-Key": "forged"},
                json={**fixture["requests"]["create"], "scores": []},
            ).status_code
            != 200
        )
        assert len(provider.calls) == native["calls"]
    encoded = json.dumps(fixture, sort_keys=True, indent=2)
    for forbidden in (
        r.token[0],
        "workerToken",
        str(r.env.artifact_root),
        str(r.registry.runtime.rootfs),
    ):
        assert forbidden not in encoded
    if destination := os.environ.get("POTBOT_TASK_DISCOVERY_CONTRACT_OUTPUT"):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded + "\n", encoding="utf-8")
