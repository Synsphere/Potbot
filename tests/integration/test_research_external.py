"""Real PostgreSQL external-research ownership with explicit synthetic HTTP fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from potbot.controller.research_external_worker import ExternalResearchWorker
from potbot.controller.research_store import ResearchStore
from potbot.controller.research_worker import LocalResearchWorker, ResearchRunStore
from potbot.domain.errors import (
    BudgetUnavailableError,
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.persistence.artifacts import ArtifactStore
from potbot.research.contracts import ResearchCategory, ResearchOperation
from potbot.research.providers import fetch_request, search_request
from potbot.research.source_policy import (
    ExternalResearchPolicy,
    ResearchFetchHostRule,
    ResearchProviderRule,
)

from .test_phase7_research_store import _setup

pytestmark = pytest.mark.postgres
TOKEN = "research-fixture-operator-capability"


def policy_fixture(**changes):
    return replace(
        ExternalResearchPolicy(
            True,
            (ResearchProviderRule("brave", 3, 0), ResearchProviderRule("tavily", 5, 0)),
            (ResearchFetchHostRule("docs.example.com", True, 0),),
            ("rate_limited", "quota_exhausted", "unavailable"),
            max_results=3,
            max_documents=2,
            max_requests=6,
            max_source_bytes=16384,
            timeout_seconds=1,
        ),
        **changes,
    )


def _setup_external(app_env, tmp_path, policy=None, *, queue=True):
    from psycopg.types.json import Jsonb

    rig, scope, original = _setup(app_env, tmp_path)
    selected = policy or policy_fixture()
    action = replace(
        original,
        action_id=uuid4(),
        query=replace(
            original.query,
            category=ResearchCategory.DOCUMENTATION,
            operation=ResearchOperation.QUERY_SOURCE,
            locator="external-research",
            search_text="bounded source evidence",
            target_version=selected.fingerprint,
            max_results=selected.max_results,
            max_source_bytes=selected.max_source_bytes,
        ),
    )
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE budget_limits SET dimensions=dimensions||%s WHERE workspace_id=%s AND id=%s",
            (
                Jsonb({"bytes_read": 10_000_000, "money_microunits": 1_000_000}),
                app_env.context.workspace_id,
                scope.task_id,
            ),
        )
    ResearchStore(app_env.db, rig.artifacts).admit(app_env.context, action, "external-action")
    store = ResearchRunStore(app_env.db, rig.artifacts, setup_token=lambda: TOKEN)
    execution = (
        store.queue_external(
            app_env.context,
            action.action_id,
            selected,
            "external-queue",
            setup_token=TOKEN,
            disclosure_confirmed=True,
        )
        if queue
        else None
    )
    return SimpleNamespace(
        rig=rig, scope=scope, action=action, store=store, policy=selected, execution=execution
    )


class SyntheticSources:
    """Actual HTTP adapter/stream path with no network or live-provider claims."""

    def __init__(
        self,
        *,
        before_response=None,
        brave_status=200,
        robots=b"User-agent: *\nAllow: /\n",
        html=None,
    ):
        self.before_response = before_response
        self.brave_status = brave_status
        self.robots = robots
        self.html = (
            html or b"<html><title>Retained guide</title><p>Alpha source says beta.</p></html>"
        )
        self.calls = []

    def get(self, name):
        assert name in {"research.brave", "research.tavily"}
        return "fixture-provider-credential"

    def transport(self, prepared, authenticated):
        def response(request):
            self.calls.append((prepared.kind, prepared.provider, str(request.url)))
            if self.before_response:
                self.before_response(prepared)
            if prepared.kind == "search":
                if prepared.provider == "brave":
                    value = {
                        "web": {
                            "results": [
                                {
                                    "url": "https://docs.example.com/guide",
                                    "title": "Retained guide",
                                    "description": "Search snippet is not source text",
                                }
                            ]
                        }
                    }
                    status = self.brave_status
                else:
                    value = {
                        "results": [
                            {
                                "url": "https://docs.example.com/guide",
                                "title": "Retained guide",
                                "content": "Independent engine, same publisher",
                            }
                        ]
                    }
                    status = 200
                raw, media = json.dumps(value).encode(), "application/json"
            elif prepared.kind == "robots":
                raw, media, status = self.robots, "text/plain", 200
            else:
                raw, media, status = self.html, "text/html; charset=utf-8", 200
            return httpx.Response(
                status, headers={"content-type": media}, stream=httpx.ByteStream(raw)
            )

        return httpx.MockTransport(response)


def _worker(fixture, sources):
    return ExternalResearchWorker(
        fixture.store,
        fixture.policy,
        credentials=sources,
        transport_factory=sources.transport,
        usage_origin="synthetic",
    )


def _reservation(app_env, fixture):
    with app_env.db.transaction(app_env.context) as conn:
        return conn.execute(
            "SELECT * FROM budget_reservations WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(fixture.execution["run"]["reservationId"])),
        ).fetchone()


def test_external_end_to_end_retains_two_engines_one_source_typed_receipts_and_usage(
    app_env, tmp_path
):
    fixture = _setup_external(app_env, tmp_path)
    sources = SyntheticSources()
    completed = _worker(fixture, sources).run_once(app_env.context, "research-fixture")
    assert completed["run"]["state"] == "succeeded", completed
    assert completed["run"]["usageOrigin"] == "synthetic"
    assert completed["run"]["requestCount"] == 4
    run_id = UUID(completed["run"]["id"])
    batch = fixture.store.audit_external(app_env.context, run_id)
    assert len(batch["searchResults"]) == 2
    assert len(batch["mergedResults"]) == 1
    assert len(batch["mergedResults"][0]["lineage"]) == 2
    assert batch["documents"][0]["status"] == "extracted"
    extraction = json.loads(
        fixture.rig.artifacts.read(
            app_env.context, UUID(batch["documents"][0]["extractionArtifactId"])
        )
    )
    assert "Alpha source says beta." in extraction["text"]
    assert "Search snippet" not in extraction["text"]
    assert completed["receipt"]["typedTransformation"]["synActivation"] == "not_qualified"
    assert completed["receipt"]["needResolved"] is False
    reservation = _reservation(app_env, fixture)
    assert reservation["status"] == "settled"
    assert reservation["actual"]["calls"] == 4
    assert reservation["actual"]["money_microunits"] == 8
    events = fixture.store.events_external(app_env.context, run_id, limit=100)
    phases = [event["telemetry"]["phase"] for event in events["items"]]
    assert phases.count("request_dispatched") == phases.count("request_observed") == 4
    assert phases[0] == "research_started" and phases[-1] == "research_completed"
    assert events["items"][-1]["output_sha256"] == completed["run"]["resultSha256"]
    assert fixture.store.claim_external(app_env.context, "another-worker", fixture.policy) is None
    assert len(sources.calls) == 4
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT status FROM need_status WHERE workspace_id=%s AND need_id=%s",
                (app_env.context.workspace_id, fixture.scope.need_id),
            ).fetchone()["status"]
            == "open"
        )


def test_external_queue_exact_replay_cannot_double_reserve_and_local_worker_cannot_claim(
    app_env, tmp_path
):
    fixture = _setup_external(app_env, tmp_path)
    assert (
        fixture.store.queue_external(
            app_env.context,
            fixture.action.action_id,
            fixture.policy,
            "external-queue",
            setup_token="stale-on-replay",
            disclosure_confirmed=True,
        )
        == fixture.execution
    )
    assert LocalResearchWorker(fixture.store).run_once(app_env.context, "local-only") is None
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM research_runs WHERE workspace_id=%s",
                (app_env.context.workspace_id,),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM budget_reservations WHERE workspace_id=%s AND reservation_key LIKE %s",
                (app_env.context.workspace_id, "research.external:%"),
            ).fetchone()["n"]
            == 1
        )
    other = ResearchRunStore(
        app_env.other_db,
        ArtifactStore(app_env.other_db, fixture.rig.artifacts.backend),
        setup_token=lambda: TOKEN,
    )
    with pytest.raises(ScopeDeniedError):
        other.inspect_external(app_env.other_context, UUID(fixture.execution["run"]["id"]))


@pytest.mark.parametrize("defect", ["token", "disclosure", "budget", "selector"])
def test_external_admission_failure_creates_no_run_or_disclosure(app_env, tmp_path, defect):
    fixture = _setup_external(app_env, tmp_path, queue=False)
    token, confirmed = TOKEN, True
    if defect == "token":
        token = "wrong"
    if defect == "disclosure":
        confirmed = False
    if defect == "budget":
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE budget_limits SET dimensions=jsonb_set(dimensions,'{calls}','0') WHERE workspace_id=%s AND id=%s",
                (app_env.context.workspace_id, fixture.scope.task_id),
            )
    selected = replace(fixture.policy, max_results=2) if defect == "selector" else fixture.policy
    with pytest.raises((ScopeDeniedError, InvalidInputError, BudgetUnavailableError)):
        fixture.store.queue_external(
            app_env.context,
            fixture.action.action_id,
            selected,
            "denied",
            setup_token=token,
            disclosure_confirmed=confirmed,
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM research_runs WHERE workspace_id=%s",
                (app_env.context.workspace_id,),
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM evidence_events WHERE workspace_id=%s AND producer_id='potbot.external_research'",
                (app_env.context.workspace_id,),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("change", ["task", "need", "policy", "consumer", "token"])
def test_stale_queued_authority_blocks_before_any_send_and_releases_budget(
    app_env, tmp_path, monkeypatch, change
):
    fixture = _setup_external(app_env, tmp_path)
    if change in {"task", "need"}:
        with app_env.db.transaction(app_env.context) as conn:
            if change == "task":
                conn.execute(
                    "UPDATE episodes SET status='paused' WHERE workspace_id=%s AND id=%s",
                    (app_env.context.workspace_id, fixture.scope.task_id),
                )
            else:
                conn.execute(
                    "UPDATE need_status SET status='blocked' WHERE workspace_id=%s AND need_id=%s",
                    (app_env.context.workspace_id, fixture.scope.need_id),
                )
    elif change == "policy":
        fixture.policy = replace(fixture.policy, max_requests=5)
    elif change == "consumer":
        monkeypatch.setattr(
            "potbot.controller.research_external.research_consumer_sha256", lambda: "a" * 64
        )
    else:
        fixture.store.setup_token = lambda: "rotated"
    sources = SyntheticSources()
    assert _worker(fixture, sources).run_once(app_env.context, "stale-worker") is None
    state = fixture.store.inspect_external(app_env.context, UUID(fixture.execution["run"]["id"]))
    assert state["run"]["state"] == "blocked"
    assert sources.calls == []
    assert _reservation(app_env, fixture)["status"] == "released"


def test_cancel_after_send_retains_response_and_prevents_second_disclosure(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path)
    run_id = UUID(fixture.execution["run"]["id"])
    sources = SyntheticSources(
        before_response=lambda _: fixture.store.cancel_external(app_env.context, run_id, "stop-now")
    )
    outcome = _worker(fixture, sources).run_once(app_env.context, "cancelled-worker")
    assert outcome["run"]["state"] == "blocked"
    assert outcome["run"]["errorCode"] == "research_cancelled"
    assert len(sources.calls) == 1
    assert _reservation(app_env, fixture)["actual"]["calls"] == 1
    assert (
        fixture.store.cancel_external(app_env.context, run_id, "stop-now")["run"]["cancelRequested"]
        is True
    )


def test_unsolicited_fetch_cannot_use_an_existing_query_grant(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path)
    lease = fixture.store.claim_external(app_env.context, "native-worker", fixture.policy)
    assert lease is not None
    with pytest.raises(ScopeDeniedError):
        fixture.store.before_send_external(
            app_env.context,
            lease,
            fixture.policy,
            fetch_request(fixture.policy, "https://docs.example.com/arbitrary"),
        )
    assert fixture.store.inspect_external(app_env.context, lease.run_id)["run"]["requestCount"] == 0


def test_lost_dispatched_worker_keeps_budget_indeterminate_and_never_retries(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path)
    lease = fixture.store.claim_external(app_env.context, "lost-worker", fixture.policy)
    assert lease is not None
    request = search_request(fixture.policy, "brave", fixture.action.query.search_text)
    fixture.store.before_send_external(app_env.context, lease, fixture.policy, request)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE research_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, lease.run_id),
        )
    assert fixture.store.quarantine_external(app_env.context) == 1
    assert fixture.store.quarantine_external(app_env.context) == 0
    assert (
        fixture.store.inspect_external(app_env.context, lease.run_id)["run"]["state"]
        == "indeterminate"
    )
    assert _reservation(app_env, fixture)["status"] == "indeterminate"
    assert fixture.store.claim_external(app_env.context, "replacement", fixture.policy) is None
    with pytest.raises(RevisionConflictError):
        fixture.store.before_send_external(app_env.context, lease, fixture.policy, request)


def test_permitted_quota_fallback_and_robots_denial_retain_truthful_partial_result(
    app_env, tmp_path
):
    fixture = _setup_external(app_env, tmp_path)
    sources = SyntheticSources(brave_status=402, robots=b"User-agent: *\nDisallow: /\n")
    completed = _worker(fixture, sources).run_once(app_env.context, "bounded-fallback")
    assert completed["run"]["state"] == "succeeded", completed
    batch = fixture.store.audit_external(app_env.context, UUID(completed["run"]["id"]))
    assert [row[0] for row in sources.calls] == ["search", "search", "robots"]
    assert batch["documents"][0]["status"] == "not_fetched"
    assert batch["documents"][0]["rawArtifactId"] is None
    assert "robots_disallowed" in batch["reasonCodes"]
    assert "provider_quota_exhausted" in batch["reasonCodes"]


def test_no_permitted_fallback_stops_after_confirmed_provider_failure(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path, policy_fixture(fallback_statuses=()))
    sources = SyntheticSources(brave_status=402)
    completed = _worker(fixture, sources).run_once(app_env.context, "no-fallback")
    assert completed["run"]["state"] == "blocked"
    assert len(sources.calls) == 1
    assert _reservation(app_env, fixture)["status"] == "settled"


def test_cancel_remains_possible_after_setup_rotation_and_stale_task(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path)
    fixture.store.setup_token = lambda: None
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE episodes SET status='paused' WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, fixture.scope.task_id),
        )
    cancelled = fixture.store.cancel_external(
        app_env.context, UUID(fixture.execution["run"]["id"]), "negative-only"
    )
    assert cancelled["run"]["state"] == "blocked"
    assert cancelled["authorization"] is None
    assert cancelled["run"]["recordIntegrityValid"] is False
    assert _reservation(app_env, fixture)["status"] == "released"


def test_missing_provider_credentials_never_create_a_dispatch_receipt(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path)
    sources = SyntheticSources()
    sources.get = lambda _name: None
    result = _worker(fixture, sources).run_once(app_env.context, "missing-credential")
    assert result["run"]["state"] == "blocked"
    assert result["run"]["errorCode"] == "provider_credential_unavailable"
    assert result["run"]["requestCount"] == 0
    assert sources.calls == []
    assert _reservation(app_env, fixture)["status"] == "released"


def test_postprocessing_failure_retains_indeterminate_hold_and_cannot_repeat(
    app_env, tmp_path, monkeypatch
):
    fixture = _setup_external(app_env, tmp_path)
    sources = SyntheticSources()

    def failed(*_args, **_kwargs):
        raise RuntimeError("lost process after durable processing-start")

    monkeypatch.setattr("potbot.controller.research_external._build_batch", failed)
    result = _worker(fixture, sources).run_once(app_env.context, "lost-parser")
    assert result["run"]["state"] == "indeterminate"
    assert result["run"]["resultArtifactId"] is None
    assert _reservation(app_env, fixture)["status"] == "indeterminate"
    assert len(sources.calls) == 4
    assert _worker(fixture, sources).run_once(app_env.context, "must-not-repeat") is None
    assert len(sources.calls) == 4


@pytest.mark.parametrize(
    "change",
    [
        "signed_response",
        "budget_actual",
        "raw_artifact",
        "extraction_artifact",
        "consumer",
        "monitoring_dispatch",
        "monitoring_response",
        "monitoring_completion",
    ],
)
def test_positive_audit_rejects_tampering_and_history_remains_readable(
    app_env, tmp_path, monkeypatch, change
):
    from psycopg.types.json import Jsonb

    from potbot.domain.errors import DomainError
    from potbot.domain.serialization import content_hash
    from potbot.persistence.database import admin_connection

    fixture = _setup_external(app_env, tmp_path)
    completed = _worker(fixture, SyntheticSources()).run_once(app_env.context, "audited-worker")
    assert completed["run"]["state"] == "succeeded"
    run_id = UUID(completed["run"]["id"])
    batch = fixture.store.audit_external(app_env.context, run_id)
    if change == "signed_response":
        with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
            row = conn.execute(
                "SELECT * FROM research_external_responses WHERE run_id=%s ORDER BY created_at LIMIT 1",
                (run_id,),
            ).fetchone()
            content = dict(row["content"])
            content["response"] = {
                **content["response"],
                "elapsedMs": content["response"]["elapsedMs"] + 1,
            }
            conn.execute("ALTER TABLE research_external_responses DISABLE TRIGGER USER")
            conn.execute(
                "UPDATE research_external_responses SET content=%s,response_sha256=%s WHERE id=%s",
                (Jsonb(content), content_hash(content), row["id"]),
            )
            conn.execute("ALTER TABLE research_external_responses ENABLE TRIGGER USER")
    elif change == "budget_actual":
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE budget_reservations SET actual=jsonb_set(actual,'{calls}','0') WHERE workspace_id=%s AND id=%s",
                (app_env.context.workspace_id, UUID(completed["run"]["reservationId"])),
            )
    elif change == "consumer":
        monkeypatch.setattr(
            "potbot.controller.research_external.research_consumer_sha256", lambda: "f" * 64
        )
    elif change.startswith("monitoring_"):
        phase = {
            "monitoring_dispatch": "request_dispatched",
            "monitoring_response": "request_observed",
            "monitoring_completion": "research_completed",
        }[change]
        with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
            conn.execute("ALTER TABLE syn_execution_events DISABLE TRIGGER USER")
            conn.execute(
                "UPDATE syn_execution_events SET telemetry=jsonb_set(telemetry,'{phase}',%s) "
                "WHERE telemetry->>'researchRunId'=%s AND telemetry->>'phase'=%s",
                (Jsonb("tampered_phase"), str(run_id), phase),
            )
            conn.execute("ALTER TABLE syn_execution_events ENABLE TRIGGER USER")
    else:
        key = "rawArtifactId" if change == "raw_artifact" else "extractionArtifactId"
        artifact_id = UUID(batch["documents"][0][key])
        metadata = fixture.rig.artifacts.metadata(app_env.context, artifact_id)
        path = fixture.rig.artifacts.backend.root / metadata["locator"]
        path.chmod(0o600)
        path.write_bytes(b"tampered retained source")
    with pytest.raises(DomainError):
        fixture.store.audit_external(app_env.context, run_id)
    assert fixture.store.inspect_external(app_env.context, run_id)["run"]["state"] == "succeeded"


def test_rolled_back_cancel_projection_cannot_restore_dispatch_authority(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path)
    lease = fixture.store.claim_external(app_env.context, "cancel-fence", fixture.policy)
    assert lease is not None
    fixture.store.cancel_external(app_env.context, lease.run_id, "cancel-fence")
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE research_runs SET cancel_requested=false WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, lease.run_id),
        )
    with pytest.raises(OperationCancelledError):
        fixture.store.before_send_external(
            app_env.context,
            lease,
            fixture.policy,
            search_request(fixture.policy, "brave", fixture.action.query.search_text),
        )
    assert fixture.store.inspect_external(app_env.context, lease.run_id)["run"]["requestCount"] == 0


@pytest.mark.parametrize("moment", ["claim", "next_send", "publication"])
def test_native_policy_reader_refuses_config_change_at_each_effect_boundary(
    app_env, tmp_path, monkeypatch, moment
):
    from potbot.research import extraction

    fixture = _setup_external(app_env, tmp_path)
    current = [fixture.policy]
    changed = replace(fixture.policy, max_results=2)
    sources = SyntheticSources()
    if moment == "claim":
        current[0] = changed
    elif moment == "next_send":
        sources.before_response = lambda _: current.__setitem__(0, changed)
    else:
        original = extraction.extract_document

        def replace_after_processing(*args, **kwargs):
            result = original(*args, **kwargs)
            current[0] = changed
            return result

        monkeypatch.setattr(extraction, "extract_document", replace_after_processing)
    worker = ExternalResearchWorker(
        fixture.store,
        fixture.policy,
        credentials=sources,
        transport_factory=sources.transport,
        usage_origin="synthetic",
        policy_provider=lambda: current[0],
    )
    result = worker.run_once(app_env.context, "live-policy-reader")
    if moment == "claim":
        assert result is None and sources.calls == []
        assert _reservation(app_env, fixture)["status"] == "released"
    elif moment == "next_send":
        assert result["run"]["state"] == "blocked" and len(sources.calls) == 1
        assert _reservation(app_env, fixture)["actual"]["calls"] == 1
    else:
        assert result["run"]["state"] == "indeterminate" and len(sources.calls) == 4
        assert result["run"]["resultArtifactId"] is None
        assert _reservation(app_env, fixture)["status"] == "indeterminate"


def test_historical_completed_source_audit_survives_task_and_need_staleness(app_env, tmp_path):
    fixture = _setup_external(app_env, tmp_path)
    completed = _worker(fixture, SyntheticSources()).run_once(app_env.context, "historical-worker")
    run_id = UUID(completed["run"]["id"])
    before = fixture.store.audit_external(app_env.context, run_id)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE episodes SET status='paused' WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, fixture.scope.task_id),
        )
        conn.execute(
            "UPDATE need_status SET status='cancelled' WHERE workspace_id=%s AND need_id=%s",
            (app_env.context.workspace_id, fixture.scope.need_id),
        )
    assert fixture.store.audit_external(app_env.context, run_id) == before


def test_workspace_shared_reader_preserves_original_actor_and_cannot_create_second_attempt(
    app_env, tmp_path
):
    from potbot.persistence.database import provision_principal

    fixture = _setup_external(app_env, tmp_path)
    with app_env.other_db.transaction(app_env.other_context) as conn:
        role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    reader = provision_principal(
        app_env.admin_dsn, app_env.schema, role, app_env.context.workspace_id
    )
    store = ResearchRunStore(
        app_env.other_db,
        ArtifactStore(app_env.other_db, fixture.rig.artifacts.backend),
        setup_token=lambda: TOKEN,
    )
    viewed = store.inspect_external(reader, UUID(fixture.execution["run"]["id"]))
    assert viewed["scope"]["principalId"] == str(reader.principal_id)
    assert viewed["authorization"]["actorPrincipalId"] == str(app_env.context.principal_id)
    reused = store.queue_external(
        reader,
        fixture.action.action_id,
        fixture.policy,
        "external-queue",
        setup_token=TOKEN,
        disclosure_confirmed=True,
    )
    assert reused["scope"]["principalId"] == str(reader.principal_id)
    assert reused["run"]["id"] == fixture.execution["run"]["id"]
    assert _reservation(app_env, fixture)["status"] == "reserved"


def test_migration_46_to_47_preserves_local_queue_and_denies_binding_rewrites(app_env, tmp_path):
    from potbot.persistence.database import (
        Database,
        RepositoryStore,
        migrate,
        provision_principal,
        provision_workspace,
    )
    from potbot.persistence.schema import application_migrations

    schema = "before_external_research"
    revisions = application_migrations()[:47]
    assert revisions[-1].version == 47
    migrate(app_env.admin_dsn, schema, revisions[:46])
    with app_env.db.transaction(app_env.context) as conn:
        role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    root = tmp_path / "legacy-workspace"
    workspace = provision_workspace(
        app_env.admin_dsn, schema, "legacy local research", sandbox_root=root
    )
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    db = Database(app_env.db.dsn, schema)
    repository = RepositoryStore(db).create(actor, name="legacy", source_uri="fixture://legacy")
    legacy_env = replace(
        app_env,
        db=db,
        context=actor,
        repository_id=repository["id"],
        schema=schema,
        artifact_root=root / "artifacts" / "objects",
    )
    rig, _scope, action = _setup(legacy_env, tmp_path / "local")
    ResearchStore(db, rig.artifacts).admit(actor, action, "legacy-action")
    store = ResearchRunStore(db, rig.artifacts, setup_token=lambda: TOKEN)
    queued = store.queue(actor, action.action_id, "legacy-queue")
    assert migrate(app_env.admin_dsn, schema, revisions)[-1] == 47
    assert migrate(app_env.admin_dsn, schema, revisions)[-1] == 47
    with db.transaction(actor) as conn:
        row = conn.execute(
            "SELECT execution_kind,external_authorization,external_mac FROM research_runs WHERE id=%s",
            (UUID(queued["run_id"]),),
        ).fetchone()
        assert row == {
            "execution_kind": "local",
            "external_authorization": None,
            "external_mac": None,
        }
    completed = LocalResearchWorker(store).run_once(actor, "upgraded-local-worker")
    assert completed["state"] == "succeeded"
    with pytest.raises(RevisionConflictError):
        with db.transaction(actor) as conn:
            conn.execute(
                "UPDATE research_runs SET action_identity=%s WHERE workspace_id=%s AND id=%s",
                ("a" * 64, workspace, UUID(queued["run_id"])),
            )
