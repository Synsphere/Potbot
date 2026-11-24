"""Retained T05 excerpts enter a checked run only through explicit selection."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from potbot.api.routed_reasoning_cli import main as reasoning_cli
from potbot.controller.information_needs import InformationNeedStore, NeedSpec
from potbot.controller.research_store import ResearchStore
from potbot.controller.research_workbench import ResearchWorkbench
from potbot.controller.routed_reasoning import RoutedReasoning
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from tests.integration.test_operation_checks import TOKEN
from tests.integration.test_research_external import (
    TOKEN as RESEARCH_TOKEN,
)
from tests.integration.test_research_external import (
    SyntheticSources,
    _setup_external,
    _worker,
)
from tests.integration.test_routed_reasoning import routed as routed

pytestmark = pytest.mark.postgres


def _completed(app_env, tmp_path, *, html=None, freshness=0):
    actor = app_env.context
    fixture = _setup_external(app_env, tmp_path, queue=not freshness)
    if freshness:
        action = replace(
            fixture.action,
            action_id=uuid4(),
            query=replace(fixture.action.query, freshness_seconds=freshness),
        )
        ResearchStore(app_env.db).admit(actor, action, "routed-test-research-action")
        fixture.store.queue_external(
            actor,
            action.action_id,
            fixture.policy,
            "routed-test-research-queue",
            setup_token=RESEARCH_TOKEN,
            disclosure_confirmed=True,
        )
    sources = SyntheticSources(html=html)
    completed = _worker(fixture, sources).run_once(actor, "routed-citation-source")
    assert completed["run"]["state"] == "succeeded", completed
    run_id = UUID(completed["run"]["id"])
    research = ResearchWorkbench(
        app_env.db, artifacts=fixture.rig.artifacts, setup_token=lambda: RESEARCH_TOKEN
    )
    result = research.result(actor, run_id)
    assert result["verification"] == "verified", result
    document = result["batch"]["documents"][0]
    source = research.source(actor, run_id, document["sourceId"])
    return fixture, research, source, sources


def _selected(routed, fixture, source):
    plan = deepcopy(routed.plan)
    for stage in plan["stages"]:
        stage["request"]["episode_id"] = str(fixture.scope.task_id)
    quote = b"beta"
    start = source["extraction"]["text"].encode("utf-8").index(quote)
    plan["evidence_citations"] = {
        "repository_state_id": str(fixture.scope.repository_state_id),
        "allow_hosted_disclosure": True,
        "citations": [
            {
                "runId": source["execution"]["run"]["id"],
                "sourceId": source["source"]["sourceId"],
                "startByte": start,
                "endByte": start + len(quote),
                "quoteSha256": hashlib.sha256(quote).hexdigest(),
            }
        ],
    }
    return plan


def test_citation_pin_survives_concurrent_start_and_source_unavailability(
    app_env, routed, tmp_path, monkeypatch
):
    fixture, research, source, sources = _completed(app_env, tmp_path)
    actor = routed.actor
    plan = _selected(routed, fixture, source)
    run_id = uuid4()
    original_calls = list(sources.calls)
    original_reader = RoutedReasoning(app_env.db, research=research).research.citation
    reads = []

    def citation(self, *args, **kwargs):
        reads.append(1)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(
        "potbot.controller.routed_reasoning_evidence.ResearchEvidence.citation", citation
    )

    def start():
        return RoutedReasoning(app_env.db, research=research).start(actor, run_id, plan)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(start), pool.submit(start)
        initial = first.result(timeout=15)
        assert second.result(timeout=15) == initial
    assert len(reads) == 1
    assert sources.calls == original_calls
    assert initial["state"] == "active" and initial["decision_id"] is None

    with app_env.db.transaction(actor) as connection:
        row = connection.execute(
            "SELECT plan,plan_sha256 FROM routed_reasoning_runs WHERE workspace_id=%s AND id=%s",
            (actor.workspace_id, run_id),
        ).fetchone()
    assert row["plan_sha256"] == content_hash(row["plan"])
    assert "evidence_citations" not in row["plan"]
    packet = row["plan"]["evidence"][-1]
    assert packet["selection_sha256"] == content_hash(plan["evidence_citations"])
    assert packet["items"][0]["quote"] == "beta"
    assert packet["items"][0]["trust"] == "untrusted_literal_excerpt"
    assert (
        packet["items"][0]["quote_sha256"]
        == plan["evidence_citations"]["citations"][0]["quoteSha256"]
    )
    assert "https://" not in json.dumps(packet)
    assert "Alpha source says" not in json.dumps(packet)

    def offline(*args, **kwargs):
        pytest.fail("a retained plan retry or compiler read reopened a source")

    monkeypatch.setattr(
        "potbot.controller.routed_reasoning_evidence.ResearchEvidence.citation", offline
    )
    assert start() == initial
    changed = deepcopy(plan)
    changed["evidence_citations"]["citations"][0]["quoteSha256"] = "a" * 64
    with pytest.raises(StateMismatchError, match="cannot be rebound"):
        RoutedReasoning(app_env.db).start(actor, run_id, changed)

    advanced = RoutedReasoning(app_env.db).advance(actor, run_id, setup_token=TOKEN)
    assert advanced["check_job_id"] and advanced["job_id"] is None, advanced
    with app_env.db.transaction(actor) as connection:
        compiled = connection.execute(
            "SELECT facts FROM model_route_compiled_prompts "
            "WHERE workspace_id=%s AND decision_id=%s",
            (actor.workspace_id, advanced["decision_id"]),
        ).fetchone()
    assert compiled["facts"]["evidence"][-1] == packet
    assert sources.calls == original_calls


def test_hosted_attachment_requires_disclosure_before_reading_source(
    app_env, routed, tmp_path, monkeypatch
):
    fixture, research, source, _ = _completed(app_env, tmp_path)
    plan = _selected(routed, fixture, source)
    plan["evidence_citations"]["allow_hosted_disclosure"] = False

    def unexpected(*args, **kwargs):
        pytest.fail("a source was opened without hosted disclosure consent")

    monkeypatch.setattr(
        "potbot.controller.routed_reasoning_evidence.ResearchEvidence.citation", unexpected
    )
    with pytest.raises(ScopeDeniedError, match="explicit hosted disclosure"):
        RoutedReasoning(app_env.db, research=research).start(routed.actor, uuid4(), plan)


def test_attachment_refuses_changed_hash_or_need_revision(app_env, routed, tmp_path):
    fixture, research, source, _ = _completed(app_env, tmp_path)
    plan = _selected(routed, fixture, source)
    service = RoutedReasoning(app_env.db, research=research)
    foreign_task = deepcopy(routed.plan)
    foreign_task["evidence_citations"] = plan["evidence_citations"]
    with pytest.raises(ScopeDeniedError):
        service.start(routed.actor, uuid4(), foreign_task)
    bad = deepcopy(plan)
    bad["evidence_citations"]["citations"][0]["quoteSha256"] = "0" * 64
    with pytest.raises(StateMismatchError, match="exact bytes"):
        service.start(routed.actor, uuid4(), bad)

    needs = InformationNeedStore(app_env.db)
    retained = needs.inspect(routed.actor, fixture.scope.need_id)
    needs.revise(
        routed.actor,
        fixture.scope.need_id,
        retained["update_token"],
        fixture.scope.need_revision_id,
        NeedSpec(
            "What does the revised destination require?",
            "Choose a bounded implementation",
            ("inspect", "stop"),
            ("current source bytes",),
            ("exact citation",),
            "current task",
        ),
        "routed-reasoning-test-revised-need",
    )
    with pytest.raises(StateMismatchError, match="original open information-need revision"):
        service.start(routed.actor, uuid4(), plan)


def test_attachment_refuses_stale_reported_publication(app_env, routed, tmp_path):
    fixture, research, source, _ = _completed(
        app_env,
        tmp_path,
        html=(
            b'<html><head><meta property="article:published_time" '
            b'content="2020-01-01T00:00:00Z"><title>Guide</title></head>'
            b"<body><p>Alpha source says beta.</p></body></html>"
        ),
        freshness=3600,
    )
    with pytest.raises(StateMismatchError, match="fresh source-reported publication"):
        RoutedReasoning(app_env.db, research=research).start(
            routed.actor, uuid4(), _selected(routed, fixture, source)
        )


def test_cli_accepts_explicit_evidence_selection(app_env, routed, tmp_path, monkeypatch, capsys):
    fixture, research, source, _ = _completed(app_env, tmp_path)
    actor = routed.actor
    plan = _selected(routed, fixture, source)
    plan_file = tmp_path / "cited-plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr("potbot.api.jobs_cli.database_context", lambda args: (app_env.db, actor))
    monkeypatch.setattr("potbot.controller.routed_reasoning.ResearchWorkbench", lambda db: research)
    run_id = uuid4()
    args = [
        "--workspace",
        str(actor.workspace_id),
        "--principal",
        str(actor.principal_id),
        "start",
        str(run_id),
        "--plan",
        str(plan_file),
    ]
    assert reasoning_cli(args) == 0
    received = json.loads(capsys.readouterr().out)
    assert received["run_id"] == str(run_id)
    assert reasoning_cli(args) == 0
    assert json.loads(capsys.readouterr().out) == received
