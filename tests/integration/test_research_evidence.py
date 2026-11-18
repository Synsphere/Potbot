"""Real HTTP/DB research workbench and immutable cited-source comparisons.

Provider transport is explicitly synthetic; no live provider or host isolation
qualification is claimed by this fixture. All admission, artifacts, receipts,
accounting, extraction, HTTP responses and citation comparisons use real owners.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_research import register_research_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.controller.information_needs import InformationNeedStore, NeedSpec
from potbot.controller.research_store import ResearchStore
from potbot.controller.research_workbench import ResearchWorkbench, parse_need_spec
from potbot.domain.errors import DomainError, RevisionConflictError, ScopeDeniedError
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.evidence import EvidenceStore
from potbot.research.contracts import ResearchQuery

from .test_research_external import TOKEN, SyntheticSources, _setup_external, _worker

pytestmark = pytest.mark.postgres


def _workbench(app_env, fixture):
    return ResearchWorkbench(
        app_env.db,
        artifacts=fixture.rig.artifacts,
        policy=fixture.policy,
        setup_token=lambda: TOKEN,
    )


def _spec(question="Which value does the retained source report?"):
    return NeedSpec(
        question,
        "Select a value supported by an exact source quote",
        ("use source value", "leave unresolved"),
        ("fetched source text and immutable byte citation",),
        ("literal source agreement with explicit conflicts",),
        "selected task and repository state",
    )


def _context(app_env, fixture, service):
    return service.context(
        app_env.context,
        fixture.scope.task_id,
        fixture.scope.repository_state_id,
        fixture.scope.need_id,
    )


def _citation_spec(source, quote):
    raw = source["extraction"]["text"].encode("utf-8")
    chosen = quote.encode("utf-8")
    start = raw.index(chosen)
    return {
        "runId": source["execution"]["run"]["id"],
        "sourceId": source["source"]["sourceId"],
        "startByte": start,
        "endByte": start + len(chosen),
        "quoteSha256": hashlib.sha256(chosen).hexdigest(),
    }


def _completed(app_env, tmp_path, *, html=None, freshness=0):
    fixture = _setup_external(app_env, tmp_path, queue=freshness == 0)
    if freshness:
        fixture.action = replace(
            fixture.action,
            action_id=uuid4(),
            query=replace(fixture.action.query, freshness_seconds=freshness),
        )
        ResearchStore(app_env.db).admit(app_env.context, fixture.action, "freshness-action")
        fixture.execution = fixture.store.queue_external(
            app_env.context,
            fixture.action.action_id,
            fixture.policy,
            "freshness-queue",
            setup_token=TOKEN,
            disclosure_confirmed=True,
        )
    sources = SyntheticSources(html=html)
    completed = _worker(fixture, sources).run_once(app_env.context, "evidence-fixture")
    assert completed["run"]["state"] == "succeeded", completed
    service = _workbench(app_env, fixture)
    run_id = UUID(completed["run"]["id"])
    result = service.result(app_env.context, run_id)
    assert result["verification"] == "verified", result
    document = result["batch"]["documents"][0]
    source = service.source(app_env.context, run_id, document["sourceId"])
    return fixture, service, source, sources


def _claims(*specs):
    return [{"key": "reported-value", "label": "Literal reported value", "citations": list(specs)}]


def _compare(app_env, fixture, service, specs, *, key="comparison"):
    context = _context(app_env, fixture, service)
    return service.compare(
        app_env.context,
        fixture.scope.task_id,
        fixture.scope.need_id,
        context["contextSha256"],
        _claims(*specs),
        key,
        setup_token=TOKEN,
    )


def _client(app_env, service):
    app = FastAPI()

    def authenticate(value):
        if value != "Bearer ordinary":
            raise ScopeDeniedError("Research test authentication failed")
        return app_env.context

    def authorize(value):
        if value != TOKEN:
            raise ScopeDeniedError("Research test setup capability failed")

    @app.exception_handler(DomainError)
    async def failed(request, error):
        result = error_response(
            error,
            semantics=OperationSemantics(
                read_only=request.method == "GET",
                idempotency_key=hasattr(request.state, "idempotency_key"),
            ),
        )
        return JSONResponse(result.body, status_code=result.status)

    register_research_routes(
        app,
        app_env.db,
        authenticate,
        ("https://operator.example",),
        app_env.context.workspace_id,
        authorize,
        service=service,
    )
    return TestClient(app)


def test_actual_http_research_workflow_exports_browser_contract_fixture(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, queue=False)
    service = _workbench(app_env, fixture)
    client = _client(app_env, service)
    episode, state = str(fixture.scope.task_id), str(fixture.scope.repository_state_id)
    base = "/v1/ui/research"
    ordinary = {
        "Authorization": "Bearer ordinary",
        "Origin": "https://operator.example",
        "X-Potbot-Expected-Principal": str(app_env.context.principal_id),
    }

    def read(path, **params):
        response = client.get(base + path, params=params, headers=ordinary)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        return response.json()

    def write(path, body, *, key, queued=False):
        response = client.post(
            base + path,
            json={**body, "confirm": True},
            headers={**ordinary, "X-Potbot-Setup-Token": TOKEN, "Idempotency-Key": key},
        )
        assert response.status_code == (202 if queued else 200), response.text
        return response.json()

    context_before = read("/context", episodeId=episode, repositoryStateId=state)
    assert not context_before["current"] and context_before["need"] is None
    assert context_before["task"]["semanticReleaseSha256"] == fixture.scope.semantic_release_digest
    created = write(
        "/needs",
        {"episodeId": episode, "repositoryStateId": state, "spec": _spec().to_dict()},
        key="http-need",
    )
    need_id = created["need"]["id"]
    context = read("/context", episodeId=episode, repositoryStateId=state, needId=need_id)
    action = write(
        "/actions",
        {
            "episodeId": episode,
            "repositoryStateId": state,
            "needId": need_id,
            "contextSha256": context["contextSha256"],
            "query": context["queryTemplate"],
        },
        key="http-action",
    )
    queued = write(
        "/runs",
        {
            "actionId": action["action"]["action_id"],
            "policySha256": context["policy"]["sha256"],
            "disclosureConfirmed": True,
        },
        key="http-queue",
        queued=True,
    )
    run_id = queued["execution"]["run"]["id"]
    pending = read(f"/runs/{run_id}/result")
    assert pending["verification"] == "pending" and pending["batch"] is None
    sources = SyntheticSources(
        html="<title>Unicode guide</title><p>Alpha source says β🍀.</p>".encode()
    )
    completed = _worker(fixture, sources).run_once(app_env.context, "http-evidence-fixture")
    assert completed["run"]["id"] == run_id and completed["run"]["state"] == "succeeded"
    execution = read(f"/runs/{run_id}")
    events = read(f"/runs/{run_id}/events", limit=100, after=0)
    runs = read("/runs", episodeId=episode, limit=25)
    result = read(f"/runs/{run_id}/result")
    assert result["verification"] == "verified", result
    source_id = result["batch"]["documents"][0]["sourceId"]
    source = read(f"/runs/{run_id}/sources/{source_id}")
    spec = _citation_spec(source, "β🍀")
    response = client.post(base + "/citations", json=spec, headers=ordinary)
    assert response.status_code == 200, response.text
    citation = response.json()
    comparison = write(
        "/comparisons",
        {
            "episodeId": episode,
            "needId": need_id,
            "contextSha256": context["contextSha256"],
            "claims": _claims(spec),
        },
        key="http-comparison",
    )
    read_comparison = read("/comparisons/" + comparison["evidenceId"])
    assert read_comparison == comparison
    assert comparison["report"]["status"] == "supported"
    assert not comparison["report"]["needResolved"]
    assert len(sources.calls) == 4
    assert (
        InformationNeedStore(app_env.db).inspect(app_env.context, UUID(need_id))["status"] == "open"
    )
    wire = {
        "description": (
            "Actual HTTP/PostgreSQL research lifecycle with explicitly synthetic provider "
            "transport; this does not qualify live providers, deployed SYN activation or PDF isolation."
        ),
        "workspaceId": str(app_env.context.workspace_id),
        "principalId": str(app_env.context.principal_id),
        "episodeId": episode,
        "repositoryStateId": state,
        "needId": need_id,
        "runId": run_id,
        "contextBeforeNeed": context_before,
        "createdNeed": created,
        "context": context,
        "action": action,
        "queued": queued,
        "pending": pending,
        "execution": execution,
        "events": events,
        "runs": runs,
        "result": result,
        "source": source,
        "citation": citation,
        "comparison": comparison,
        "readComparison": read_comparison,
    }
    if destination := os.getenv("POTBOT_RESEARCH_HTTP_FIXTURE"):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(wire, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_exact_source_citation_and_report_round_trip_without_resolving_need(app_env, tmp_path):
    fixture, service, source, sources = _completed(app_env, tmp_path)
    spec = _citation_spec(source, "beta")
    citation = service.citation(app_env.context, spec)["citation"]
    assert citation["quote"] == "beta"
    assert citation["rawSha256"] == source["source"]["rawSha256"]
    assert citation["researchScope"]["need_revision_id"] == str(fixture.scope.need_revision_id)
    report = _compare(app_env, fixture, service, [spec])
    assert report["report"]["status"] == "supported"
    assert report["report"]["qualification"] == "verified_cited_source_agreement_only"
    assert not report["report"]["needResolved"]
    assert service.comparison(app_env.context, UUID(report["evidenceId"])) == report
    assert (
        InformationNeedStore(app_env.db).inspect(app_env.context, fixture.scope.need_id)["status"]
        == "open"
    )
    assert len(sources.calls) == 4, "citation/report reads must not send network requests"


def test_conflicting_retained_source_values_stay_unresolved(app_env, tmp_path):
    fixture, service, first, _ = _completed(app_env, tmp_path)
    second_action = replace(
        fixture.action,
        action_id=uuid4(),
        query=replace(fixture.action.query, search_text="independent follow-up evidence"),
    )
    ResearchStore(app_env.db).admit(app_env.context, second_action, "conflicting-action")
    fixture.store.queue_external(
        app_env.context,
        second_action.action_id,
        fixture.policy,
        "conflicting-queue",
        setup_token=TOKEN,
        disclosure_confirmed=True,
    )
    completed = _worker(
        fixture, SyntheticSources(html=b"<p>Alpha source says gamma.</p>")
    ).run_once(app_env.context, "conflict-fixture")
    assert completed["run"]["state"] == "succeeded"
    run_id = UUID(completed["run"]["id"])
    result = service.result(app_env.context, run_id)
    second = service.source(app_env.context, run_id, result["batch"]["documents"][0]["sourceId"])
    comparison = _compare(
        app_env, fixture, service, [_citation_spec(first, "beta"), _citation_spec(second, "gamma")]
    )
    claim = comparison["report"]["claims"][0]
    assert claim["status"] == comparison["report"]["status"] == "unresolved"
    assert claim["values"] == ["beta", "gamma"]
    assert claim["reasonCodes"] == ["conflicting_cited_values"]
    assert service.comparison(app_env.context, UUID(comparison["evidenceId"])) == comparison


@pytest.mark.parametrize("changed", ["rawArtifactId", "extractionArtifactId", "comparison"])
def test_tampered_artifacts_cannot_retain_a_positive_comparison(app_env, tmp_path, changed):
    fixture, service, source, _ = _completed(app_env, tmp_path)
    spec = _citation_spec(source, "beta")
    comparison = _compare(app_env, fixture, service, [spec])
    evidence_id = UUID(comparison["evidenceId"])
    artifact_id = (
        EvidenceStore(app_env.db).read(app_env.context, evidence_id)["payload_id"]
        if changed == "comparison"
        else UUID(source["source"][changed])
    )
    metadata = fixture.rig.artifacts.metadata(app_env.context, artifact_id)
    path = fixture.rig.artifacts.backend.root / metadata["locator"]
    path.write_bytes(b"changed fixture bytes")
    with pytest.raises(DomainError):
        service.comparison(app_env.context, evidence_id)
    if changed != "comparison":
        with pytest.raises(DomainError):
            service.citation(app_env.context, spec)
        assert service.result(app_env.context, UUID(spec["runId"]))["verification"] == "unavailable"


def test_foreign_workspace_cannot_read_source_citation_or_comparison(app_env, tmp_path):
    fixture, service, source, _ = _completed(app_env, tmp_path)
    spec = _citation_spec(source, "beta")
    comparison = _compare(app_env, fixture, service, [spec])
    other = ResearchWorkbench(
        app_env.other_db,
        artifacts=ArtifactStore(app_env.other_db, fixture.rig.artifacts.backend),
        policy=fixture.policy,
        setup_token=lambda: TOKEN,
    )
    for read in (
        lambda: other.source(app_env.other_context, UUID(spec["runId"]), spec["sourceId"]),
        lambda: other.citation(app_env.other_context, spec),
        lambda: other.comparison(app_env.other_context, UUID(comparison["evidenceId"])),
    ):
        with pytest.raises(ScopeDeniedError):
            read()


def test_new_need_revision_refuses_old_citations_but_preserves_historical_report(app_env, tmp_path):
    fixture, service, source, _ = _completed(app_env, tmp_path)
    spec = _citation_spec(source, "beta")
    historical = _compare(app_env, fixture, service, [spec])
    needs = InformationNeedStore(app_env.db)
    retained = needs.inspect(app_env.context, fixture.scope.need_id)
    needs.revise(
        app_env.context,
        fixture.scope.need_id,
        retained["update_token"],
        UUID(retained["current_revision_id"]),
        _spec("Which value is required by the revised decision?"),
        "revised-need",
    )
    new = _compare(app_env, fixture, service, [spec], key="revised-comparison")
    assert new["report"]["status"] == "unsupported"
    assert "citation_scope_mismatch" in new["report"]["claims"][0]["reasonCodes"]
    assert service.comparison(app_env.context, UUID(historical["evidenceId"])) == historical


@pytest.mark.parametrize(
    "date,reason",
    [
        ("2020-01-01T00:00:00Z", "publication_time_stale"),
        ("2020-01-01", "publication_time_unknown"),
    ],
)
def test_recent_fetch_does_not_satisfy_publication_freshness(app_env, tmp_path, date, reason):
    html = f'<meta property="article:published_time" content="{date}"><p>value beta</p>'.encode()
    fixture, service, source, _ = _completed(app_env, tmp_path, html=html, freshness=3600)
    comparison = _compare(app_env, fixture, service, [_citation_spec(source, "beta")])
    assert comparison["report"]["status"] == "unresolved"
    assert reason in comparison["report"]["claims"][0]["reasonCodes"]


def test_original_facade_commands_recover_after_policy_and_task_drift(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, queue=False)
    service = _workbench(app_env, fixture)
    actor, episode, state = (
        app_env.context,
        fixture.scope.task_id,
        fixture.scope.repository_state_id,
    )
    need = service.create_need(
        actor, episode, state, _spec().to_dict(), "recover-need", setup_token=TOKEN
    )
    need_id = UUID(need["need"]["id"])
    context = service.context(actor, episode, state, need_id)
    args = (
        actor,
        episode,
        state,
        need_id,
        context["contextSha256"],
        context["queryTemplate"],
        "recover-action",
    )
    action = service.admit(*args, setup_token=TOKEN)
    run_args = (
        actor,
        UUID(action["action"]["action_id"]),
        fixture.policy.fingerprint,
        "recover-run",
    )
    queued = service.queue(*run_args, setup_token=TOKEN, disclosure_confirmed=True)
    service.configured_policy = replace(fixture.policy, max_requests=5)
    with app_env.db.transaction(actor) as conn:
        conn.execute(
            "UPDATE episodes SET status='paused' WHERE workspace_id=%s AND id=%s",
            (actor.workspace_id, episode),
        )
    assert (
        service.create_need(
            actor, episode, state, _spec().to_dict(), "recover-need", setup_token=TOKEN
        )
        == need
    )
    assert service.admit(*args, setup_token=TOKEN) == action
    assert service.queue(*run_args, setup_token=TOKEN, disclosure_confirmed=True) == queued
    with pytest.raises(RevisionConflictError):
        service.admit(*args[:-1], "new-action-after-drift", setup_token=TOKEN)
    with app_env.db.transaction(actor) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM research_runs WHERE workspace_id=%s",
                (actor.workspace_id,),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM budget_reservations WHERE workspace_id=%s AND reservation_key LIKE %s",
                (actor.workspace_id, "research.external:%"),
            ).fetchone()["n"]
            == 1
        )


def test_comparison_original_key_recovers_after_admission_becomes_unavailable(app_env, tmp_path):
    fixture, service, source, _ = _completed(app_env, tmp_path)
    context = _context(app_env, fixture, service)
    args = (
        app_env.context,
        fixture.scope.task_id,
        fixture.scope.need_id,
        context["contextSha256"],
        _claims(_citation_spec(source, "beta")),
        "recover-comparison",
    )
    report = service.compare(*args, setup_token=TOKEN)
    service.configured_policy = replace(fixture.policy, allow_query_disclosure=False)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE episodes SET status='paused' WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, fixture.scope.task_id),
        )
    assert service.compare(*args, setup_token=TOKEN) == report
    assert service.comparison(app_env.context, UUID(report["evidenceId"])) == report
    with pytest.raises(RevisionConflictError):
        service.compare(*args[:-1], "new-comparison-after-drift", setup_token=TOKEN)


def test_multibyte_need_template_remains_a_valid_bounded_research_query(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, queue=False)
    service = _workbench(app_env, fixture)
    created = service.create_need(
        app_env.context,
        fixture.scope.task_id,
        fixture.scope.repository_state_id,
        _spec("🍀" * 1000).to_dict(),
        "unicode-need",
        setup_token=TOKEN,
    )
    context = service.context(
        app_env.context,
        fixture.scope.task_id,
        fixture.scope.repository_state_id,
        UUID(created["need"]["id"]),
    )
    query = ResearchQuery.from_dict(context["queryTemplate"])
    assert len(query.search_text.encode("utf-8")) <= 1024
    assert len(query.search_text) <= 500
    assert parse_need_spec(context["need"]["spec"]).question == "🍀" * 1000


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"providers": ()}, "research_providers_unavailable"),
        ({"max_requests": 1}, "research_request_limit_insufficient"),
    ],
)
def test_unusable_policy_never_offers_a_queueable_context(app_env, tmp_path, change, reason):
    fixture = _setup_external(app_env, tmp_path, queue=False)
    service = _workbench(app_env, fixture)
    service.configured_policy = replace(fixture.policy, **change)
    context = _context(app_env, fixture, service)
    assert context["current"] is False
    assert context["queryTemplate"] is None
    assert reason in context["reasonCodes"]
    with pytest.raises(RevisionConflictError):
        service.admit(
            app_env.context,
            fixture.scope.task_id,
            fixture.scope.repository_state_id,
            fixture.scope.need_id,
            context["contextSha256"],
            fixture.action.query.to_dict(),
            "unusable-policy-action",
            setup_token=TOKEN,
        )


@pytest.mark.parametrize("defect", ["quote_hash", "source_id", "partial_codepoint"])
def test_operator_cannot_forge_a_citation_from_retained_source(app_env, tmp_path, defect):
    fixture, service, source, _ = _completed(app_env, tmp_path, html="<p>value β🍀</p>".encode())
    spec = _citation_spec(source, "β🍀")
    if defect == "quote_hash":
        spec["quoteSha256"] = "f" * 64
    elif defect == "source_id":
        spec["sourceId"] = "f" * 64
    else:
        spec["startByte"] += 1
    with pytest.raises(DomainError):
        service.citation(app_env.context, spec)
    comparison = _compare(app_env, fixture, service, [spec])
    assert comparison["report"]["status"] == "unsupported"
    assert comparison["report"]["claims"][0]["unavailableCitationCount"] == 1


def test_changed_consumer_cannot_retain_positive_historical_comparison(
    app_env, tmp_path, monkeypatch
):
    fixture, service, source, _ = _completed(app_env, tmp_path)
    spec = _citation_spec(source, "beta")
    report = _compare(app_env, fixture, service, [spec])
    monkeypatch.setattr(
        "potbot.controller.research_external.research_consumer_sha256", lambda: "f" * 64
    )
    assert service.result(app_env.context, UUID(spec["runId"]))["verification"] == "unavailable"
    with pytest.raises(DomainError):
        service.comparison(app_env.context, UUID(report["evidenceId"]))
