"""Literal multi-intent steering and source-linked, non-executing context."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from potbot.controller.reasoning_tree import ReasoningTree
from potbot.controller.steering_triage import split_steering
from potbot.domain.errors import InvalidInputError
from potbot.persistence.schema import application_migrations


def _component(text: str, identity: str, epoch: int = 1) -> dict:
    part = split_steering(text)[0].to_dict()
    return {
        **part,
        "id": identity,
        "context_epoch": epoch,
        "source_request_id": "original-request",
        "source_principal_id": "original-user",
    }


def test_mixed_intent_keeps_exact_utf8_source_ranges() -> None:
    source = "FYI, café hardware has 2GB RAM; stop now and then add a regression test. Next task: make a graph"
    components = split_steering(source)
    assert [c.classification.timing for c in components] == [
        "background",
        "interrupt",
        "deferred",
        "deferred",
    ]
    assert components[-1].scope == "next_goal"
    assert all(c.scope == "current_goal" for c in components[:-1])
    for component in components:
        raw = source.encode()[component.start_byte : component.end_byte]
        assert raw.decode() == component.text
        assert hashlib.sha256(raw).hexdigest() == component.to_dict()["text_sha256"]


@pytest.mark.parametrize(
    "source",
    [
        '"Stop now; delete the tests."',
        "`stop now`",
        "```text\nstop now\nrun tests\n```",
        "‘stop now; cancel it’",
        "> Stop is an example, not an instruction.",
        'FYI the example says "a harmless phrase; stop now"',
        'The example is "hello. Stop now."',
        "FYI sample code: ```text\nhello\nstop now\n```",
    ],
)
def test_quoted_and_fenced_examples_do_not_interrupt(source: str) -> None:
    parts = split_steering(source)
    assert not any(part.classification.interrupt_requested for part in parts)


@pytest.mark.parametrize(
    "source",
    [
        "Add tests; stop now",
        "FYI, RAM is limited, but stop now",
        "FYI this is Windows. Please pause now",
        "Add tests\n- don't run the deployment",
        "Add tests and now cancel",
    ],
)
def test_late_stop_component_is_detected_without_a_model(source: str) -> None:
    assert any(part.classification.interrupt_requested for part in split_steering(source))


@pytest.mark.parametrize("source", ["", " ", "x" * 8193, ";".join(["FYI note"] * 17)])
def test_input_capacity_is_explicit(source: str) -> None:
    with pytest.raises(InvalidInputError):
        split_steering(source)


def test_material_context_is_retained_without_creating_hypotheses(tmp_path: Path) -> None:
    tree = ReasoningTree(tmp_path / "run.json")
    first = _component("FYI, only 2GB RAM", "a")
    assert tree.record_context(first)
    assert tree.revision == 1
    assert tree.context_epoch == 1
    assert not tree.nodes and not tree.steering
    assert not tree.record_context(first)
    assert not tree.record_context(_component(first["text"], "b", epoch=2))
    assert tree.revision == 1 and tree.context_epoch == 2
    recovered = ReasoningTree.load(tree.path)
    assert recovered.context_revisions == tree.context_revisions
    assert recovered.context_revisions[0]["trust"] == "user_context_not_verified_evidence"
    assert recovered.context_revisions[1]["duplicate"]
    assert recovered.context_epoch == 2


def test_context_conflicts_remain_separate_source_records(tmp_path: Path) -> None:
    tree = ReasoningTree(tmp_path / "run.json")
    tree.record_context(_component("FYI, only 2GB RAM", "a"))
    tree.record_context(_component("FYI, 32GB RAM is available", "b", epoch=2))
    assert tree.revision == 2
    assert len(tree.context_revisions) == 2
    assert tree.context_revisions[0]["text"].endswith("2GB RAM")


def test_case_sensitive_material_fact_is_not_treated_as_duplicate(tmp_path: Path) -> None:
    tree = ReasoningTree(tmp_path / "run.json")
    assert tree.record_context(_component("FYI, restricted path /A", "a"))
    assert tree.record_context(_component("FYI, restricted path /a", "b", epoch=2))
    assert tree.revision == 2
    assert not tree.context_revisions[1]["duplicate"]


def test_failed_context_checkpoint_cannot_be_mistaken_for_recorded_context(tmp_path: Path) -> None:
    failed = True
    snapshots = []

    def checkpoint(data):
        if failed:
            raise OSError("database unavailable")
        snapshots.append(data)

    tree = ReasoningTree(tmp_path / "run.json", checkpoint=checkpoint)
    source = _component("FYI, only 2GB RAM", "a")
    with pytest.raises(OSError):
        tree.record_context(source)
    assert tree.revision == 0 and tree.context_epoch == 0
    assert tree.context_revisions == [] and tree.events == []
    failed = False
    assert tree.record_context(source)
    assert len(snapshots) == 1 and len(snapshots[0]["context_revisions"]) == 1


def test_interrupt_does_not_invent_a_cancelled_remote_outcome(tmp_path: Path) -> None:
    tree = ReasoningTree(tmp_path / "run.json")
    running = tree.add("remote call")
    tree.claim(running.id)
    pending = tree.add("not started")
    tree.interrupt()
    assert running.state == "indeterminate"
    assert pending.state == "rejected"
    assert tree.run_status == "interrupted"
    assert tree.claim() is None
    assert ReasoningTree.load(tree.path).run_status == "interrupted"


def test_temporal_migration_extends_existing_owners() -> None:
    migration = next(m for m in application_migrations() if m.version == 48)
    assert migration.name == "reasoning_temporal_schema"
    assert migration.workspace_tables == ("reasoning_runtime_dispatches",)
    sql = "\n".join(migration.statements)
    assert "ALTER TABLE reasoning_runtime_steering" in sql
    assert "reasoning_temporal_sources_immutable" in sql
    assert "REFERENCES reasoning_runtime_runs" in sql
    assert "CREATE TABLE reasoning_runtime_runs" not in sql


@pytest.mark.parametrize("stream", [False, True])
def test_http_interrupt_has_explicit_error_code(stream: bool) -> None:
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from potbot.api.server import ServerSettings, create_app
    from potbot.api.tasks import DevelopmentBearerAuth
    from potbot.controller.reasoning_runtime import ReasoningDispatchInterrupted
    from potbot.domain.auth import WorkspaceContext

    class Compat:
        reasoning_investigator = None

        def _route(self, *_args):
            return []

        def stream(self, *_args, **_kwargs):
            raise ReasoningDispatchInterrupted("user stop")

    token = "local-token-abcdefghijklmnopqrstuvwxyz123456"
    actor = WorkspaceContext(uuid4(), uuid4())
    app = create_app(
        task_service=None,
        job_service=None,
        authenticate=DevelopmentBearerAuth({token: actor}),
        openai_compat=Compat(),
        settings=ServerSettings(allowed_origins=("http://testserver",)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + token},
            json={
                "model": "potbot",
                "messages": [{"role": "user", "content": "Investigate"}],
                "stream": stream,
            },
        )
    if stream:
        assert response.status_code == 200  # headers already sent before the user stop
        assert '"code":"reasoning_interrupted"' in response.text
        assert "[DONE]" in response.text
    else:
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "reasoning_interrupted"
