"""Specialist research through actual admission, HTTP, PostgreSQL and audit owners.

Only provider HTTP responses are synthetic; these tests grant no live-provider
or deployed-Syn qualification.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from potbot.controller.research_external import ResearchRateWait
from potbot.controller.research_store import ResearchStore
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.research.providers import metadata_request, search_request
from potbot.research.source_policy import ResearchProviderRule
from potbot.research.specialist import SPECIALIST_ENDPOINTS

from .test_research_evidence import _client, _workbench
from .test_research_external import TOKEN, _reservation, _setup_external, _worker, policy_fixture

pytestmark = pytest.mark.postgres
DOI = "10.1234/example"


def policy(*providers, **changes):
    return policy_fixture(
        providers=tuple(ResearchProviderRule(provider, 2, 0) for provider in providers),
        max_documents=0,
        max_requests=8,
        **changes,
    )


class SpecialistSources:
    def __init__(self, *, search_ids=None, summary_ids=None, statuses=None, callback=None):
        self.search_ids = ["12345"] if search_ids is None else search_ids
        self.summary_ids = self.search_ids if summary_ids is None else summary_ids
        self.statuses = statuses or {}
        self.callback = callback
        self.calls = []

    def get(self, name):
        assert name in {"research.openalex", "research.semantic_scholar"}
        return None

    def transport(self, prepared, authenticated):
        assert not authenticated

        def response(actual):
            assert actual.method == "GET" and actual.content == b""
            self.calls.append((prepared.kind, prepared.provider, str(actual.url)))
            if self.callback:
                self.callback(prepared)
            if prepared.provider == "crossref":
                assert actual.url.host == "api.crossref.org"
                payload = {
                    "status": "ok",
                    "message": {
                        "items": [
                            {
                                "DOI": DOI,
                                "title": ["A retained scientific paper"],
                                "author": [{"given": "Ada", "family": "Lovelace"}],
                                "published": {"date-parts": [[2020, 2]]},
                                "event": {"start": {"date-parts": [[2019, 12, 1]]}},
                                "relation": {
                                    "is-corrected-by": [
                                        {"id-type": "doi", "id": "10.1234/correction"}
                                    ]
                                },
                            }
                        ]
                    },
                }
            elif prepared.provider == "openalex":
                assert actual.url.host == "api.openalex.org"
                payload = {
                    "results": [
                        {
                            "id": "https://openalex.org/W12345",
                            "doi": "https://doi.org/" + DOI,
                            "title": "A retained scientific paper",
                            "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
                            "publication_date": "2020-02-01",
                            "is_retracted": True,
                        }
                    ]
                }
            elif prepared.kind == "metadata":
                assert prepared.provider == "pubmed"
                assert actual.url.path.endswith("/esummary.fcgi")
                assert actual.url.params["id"] == ",".join(self.search_ids)
                payload = {
                    "result": {
                        "uids": self.summary_ids,
                        **{
                            identity: {
                                "uid": identity,
                                "title": "A retained scientific paper",
                                "authors": [{"name": "Lovelace A"}],
                                "pubdate": "2020 Feb",
                                "articleids": [{"idtype": "doi", "value": DOI}],
                                "pubtype": ["Journal Article"],
                            }
                            for identity in self.summary_ids
                        },
                    }
                }
            else:
                assert prepared.provider == "pubmed"
                assert actual.url.path.endswith("/esearch.fcgi")
                payload = {"esearchresult": {"idlist": self.search_ids}}
            return httpx.Response(
                self.statuses.get((prepared.provider, prepared.kind), 200), json=payload
            )

        return httpx.MockTransport(response)


def test_specialist_http_result_retains_metadata_disagreement_and_independent_lineage(
    app_env,
    tmp_path,
):
    fixture = _setup_external(app_env, tmp_path, policy("crossref", "openalex", "pubmed"))
    sources = SpecialistSources()
    completed = _worker(fixture, sources).run_once(app_env.context, "specialist-fixture")
    assert completed["run"]["state"] == "succeeded", completed
    run_id = UUID(completed["run"]["id"])
    batch = fixture.store.audit_external(app_env.context, run_id)
    assert [(kind, provider) for kind, provider, _ in sources.calls] == [
        ("search", "crossref"),
        ("search", "openalex"),
        ("search", "pubmed"),
        ("metadata", "pubmed"),
    ]
    assert len(batch["searchResults"]) == 3 and len(batch["mergedResults"]) == 1
    lineage = batch["mergedResults"][0]["lineage"]
    assert len(lineage) == 3 and len({row["canonicalUrl"] for row in lineage}) == 3
    assert {row["specialist"]["doi"] for row in lineage} == {DOI}
    crossref, openalex, pubmed = [row["specialist"] for row in lineage]
    assert crossref["publicationDate"]["precision"] == "month"
    assert crossref["eventDate"]["value"] == "2019-12-01"
    assert crossref["retraction"]["state"] == "unknown"
    assert crossref["retraction"]["notices"][0]["kind"] == "correction"
    assert openalex["retraction"]["state"] == "reported_retracted"
    assert pubmed["publicationDate"]["value"] == "2020 Feb"
    assert all(row["publishedAt"] is None for row in lineage)
    assert batch["documents"] == []
    assert completed["receipt"]["needResolved"] is False
    assert completed["receipt"]["typedTransformation"]["synActivation"] == "not_qualified"
    for item in batch["requests"]:
        assert item["rawArtifactId"] is not None
        assert fixture.rig.artifacts.read(app_env.context, UUID(item["rawArtifactId"]))
    reservation = _reservation(app_env, fixture)
    assert reservation["status"] == "settled"
    assert reservation["actual"]["calls"] == 4
    assert reservation["actual"]["money_microunits"] == 8
    service = _workbench(app_env, fixture)
    client = _client(app_env, service)
    headers = {"Authorization": "Bearer ordinary"}
    context = client.get(
        "/v1/ui/research/context",
        headers=headers,
        params={
            "episodeId": str(fixture.scope.task_id),
            "repositoryStateId": str(fixture.scope.repository_state_id),
            "needId": str(fixture.scope.need_id),
        },
    )
    result = client.get(f"/v1/ui/research/runs/{run_id}/result", headers=headers)
    execution = client.get(f"/v1/ui/research/runs/{run_id}", headers=headers)
    assert context.status_code == result.status_code == execution.status_code == 200
    assert result.json()["verification"] == "verified"
    wire = {
        "description": "Actual PostgreSQL/HTTP owners with synthetic provider responses; no live qualification.",
        "workspaceId": str(app_env.context.workspace_id),
        "principalId": str(app_env.context.principal_id),
        "episodeId": str(fixture.scope.task_id),
        "repositoryStateId": str(fixture.scope.repository_state_id),
        "needId": str(fixture.scope.need_id),
        "runId": str(run_id),
        "context": context.json(),
        "result": result.json(),
        "execution": execution.json(),
    }
    if destination := os.getenv("POTBOT_SPECIALIST_HTTP_FIXTURE"):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(wire, indent=2) + "\n", encoding="utf-8")


def test_pubmed_child_ids_are_reconstructed_before_dispatch_and_never_replayed(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, policy("pubmed"))
    sources = SpecialistSources()
    worker = _worker(fixture, sources)
    lease = fixture.store.claim_external(app_env.context, "pubmed-worker", fixture.policy)
    primary = search_request(fixture.policy, "pubmed", fixture.action.query.search_text)
    response = asyncio.run(worker._send(app_env.context, lease, primary))
    expected = metadata_request(fixture.policy, response.data)
    forged = metadata_request(fixture.policy, b'{"esearchresult":{"idlist":["999"]}}')
    with pytest.raises(ScopeDeniedError, match="differs"):
        fixture.store.before_send_external(app_env.context, lease, fixture.policy, forged)
    with pytest.raises(ResearchRateWait):
        fixture.store.before_send_external(app_env.context, lease, fixture.policy, expected)
    asyncio.run(worker._send(app_env.context, lease, expected))
    with pytest.raises(StateMismatchError, match="never retries"):
        fixture.store.before_send_external(app_env.context, lease, fixture.policy, expected)
    result = fixture.store.complete_external(app_env.context, lease, fixture.policy)
    assert result["run"]["state"] == "succeeded" and len(sources.calls) == 2


@pytest.mark.parametrize("summary_ids", [["999"], [], ["12345", "999"]])
def test_pubmed_injected_or_missing_metadata_never_publishes_verified_result(
    app_env,
    tmp_path,
    summary_ids,
):
    fixture = _setup_external(app_env, tmp_path, policy("pubmed"))
    sources = SpecialistSources(summary_ids=summary_ids)
    completed = _worker(fixture, sources).run_once(app_env.context, "pubmed-bad-metadata")
    assert completed["run"]["state"] == "blocked"
    assert completed["run"]["errorCode"] == "research_failed"
    assert completed["run"]["resultArtifactId"] is None
    assert len(sources.calls) == 2
    with pytest.raises(StateMismatchError):
        fixture.store.audit_external(app_env.context, UUID(completed["run"]["id"]))


def test_empty_pubmed_search_needs_no_child_but_remains_complete_observation(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, policy("pubmed"))
    sources = SpecialistSources(search_ids=[])
    completed = _worker(fixture, sources).run_once(app_env.context, "empty-pubmed")
    assert completed["run"]["state"] == "succeeded", completed
    batch = fixture.store.audit_external(app_env.context, UUID(completed["run"]["id"]))
    assert batch["mergedResults"] == [] and batch["searchResults"][0]["items"] == []
    assert len(sources.calls) == 1


def test_explicit_provider_unavailability_permits_only_configured_fallback(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, policy("crossref", "openalex"))
    sources = SpecialistSources(statuses={("crossref", "search"): 503})
    completed = _worker(fixture, sources).run_once(app_env.context, "specialist-fallback")
    assert completed["run"]["state"] == "succeeded", completed
    batch = fixture.store.audit_external(app_env.context, UUID(completed["run"]["id"]))
    assert batch["reasonCodes"] == ["provider_unavailable"]
    assert [row["provider"] for row in batch["searchResults"]] == ["openalex"]


def test_pubmed_admission_reserves_the_potential_metadata_call(app_env, tmp_path):
    selected = replace(policy("pubmed"), max_requests=1)
    with pytest.raises(InvalidInputError, match="allowance"):
        _setup_external(app_env, tmp_path, selected)


def test_all_provider_routes_are_preflighted_before_run_or_budget_admission(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, policy("crossref", "wikidata"), queue=False)
    action = replace(
        fixture.action,
        action_id=uuid4(),
        query=replace(fixture.action.query, search_text="界" * 160),
    )
    # The first provider can encode this query; the later provider cannot.
    search_request(fixture.policy, "crossref", action.query.search_text)
    ResearchStore(app_env.db, fixture.rig.artifacts).admit(
        app_env.context, action, "encoded-query-action"
    )
    with app_env.db.transaction(app_env.context) as conn:
        reservations_before = conn.execute(
            "SELECT id FROM budget_reservations WHERE workspace_id=%s AND episode_id=%s",
            (app_env.context.workspace_id, action.scope.task_id),
        ).fetchall()
    with pytest.raises(InvalidInputError):
        fixture.store.queue_external(
            app_env.context,
            action.action_id,
            fixture.policy,
            "encoded-query-queue",
            setup_token=TOKEN,
            disclosure_confirmed=True,
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT id FROM research_runs WHERE workspace_id=%s AND action_id=%s",
                (app_env.context.workspace_id, action.action_id),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT id FROM budget_reservations WHERE workspace_id=%s AND episode_id=%s",
                (app_env.context.workspace_id, action.scope.task_id),
            ).fetchall()
            == reservations_before
        )
    assert (
        fixture.store.claim_external(app_env.context, "no-partial-disclosure", fixture.policy)
        is None
    )


def test_unapproved_specialist_provider_cannot_reuse_another_disclosure(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, policy("crossref"))
    lease = fixture.store.claim_external(app_env.context, "bound-worker", fixture.policy)
    broadened = policy("crossref", "openalex")
    request = search_request(broadened, "openalex", fixture.action.query.search_text)
    assert request.url == SPECIALIST_ENDPOINTS["openalex"]
    with pytest.raises(ScopeDeniedError):
        fixture.store.before_send_external(app_env.context, lease, fixture.policy, request)
