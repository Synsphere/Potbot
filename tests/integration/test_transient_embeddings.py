"""Real SQL admission and privacy; deterministic vectors are explicitly synthetic.

These tests qualify the existing embedding owner and supervision/accounting
boundaries, not Qwen's retrieval quality. No model download is attempted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.errors import (
    BudgetUnavailableError,
    InvalidInputError,
    OperationCancelledError,
    OperationTimeoutError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.memory.embedding_profile import EmbeddingBatch, EmbeddingProfile
from potbot.memory.embeddings import EmbeddingService
from potbot.memory.supervised_encoder import SupervisedEncodingFailure, SupervisedQwenEncoder
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

pytestmark = pytest.mark.postgres

QUERY = "PRIVATE_TRANSIENT_QUERY_91a7 database search"
DOCUMENTS = (
    ("d" * 64, "PRIVATE_TRANSIENT_DOCUMENT_d positive database"),
    ("b" * 64, "PRIVATE_TRANSIENT_DOCUMENT_b negative database"),
    ("c" * 64, "PRIVATE_TRANSIENT_DOCUMENT_c orthogonal web"),
    ("a" * 64, "PRIVATE_TRANSIENT_DOCUMENT_a positive database"),
    ("e" * 64, "PRIVATE_TRANSIENT_DOCUMENT_e orthogonal web"),
)


class SyntheticTransientProvider:
    """Controlled vectors with explicit synthetic usage, never live model evidence."""

    def __init__(self, profile=None):
        self.profile = profile or EmbeddingProfile(max_input_tokens=256, max_batch_size=2)
        self.calls = []
        self.preparations = []
        self.before_prepare = None
        self.after_encode = None

    def count_tokens(self, texts, kind):
        self.preparations.append((kind, texts))
        if self.before_prepare:
            self.before_prepare()
        return sum(len(text.split()) for text in texts)

    def encode(self, texts, kind):
        self.calls.append((kind, texts))
        if self.after_encode:
            self.after_encode()
        vectors = []
        for text in texts:
            vector = [0.0] * 1024
            vector[1 if "orthogonal" in text else 0] = -1.0 if "negative" in text else 1.0
            vectors.append(tuple(vector))
        return EmbeddingBatch(
            tuple(vectors), sum(len(text.split()) for text in texts), 3, "synthetic"
        )


class SupervisedTransientFixture(SyntheticTransientProvider):
    """Exercise supervised dispatch with usage visibly labeled synthetic."""

    def __init__(self):
        super().__init__()
        self.timeouts = []

    def encode_supervised(self, texts, kind, *, timeout_ms, cancelled=None):
        self.timeouts.append(timeout_ms)
        assert cancelled is not None and not cancelled()
        return self.encode(texts, kind)


def setup_service(app_env, provider=None, *, token_limit=100_000):
    provider = provider or SyntheticTransientProvider()
    service = EmbeddingService(
        app_env.db, ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root)), provider
    )
    spec = ContractSpec(
        "Rank a private discovery shortlist",
        "fixture-baseline",
        DeliverableType.INVESTIGATION,
        (Requirement("R1", "Rank candidates", "Keep private candidate text out of shared corpus"),),
        (),
        ("embed",),
        "v1",
        (("tokens", token_limit), ("calls", 50), ("tool_ms", 500_000), ("concurrency", 1)),
    )
    tasks = TaskService(app_env.db)
    task = tasks.submit(app_env.context, app_env.repository_id, spec, "transient-task")
    episode = UUID(task["task_id"])
    for status in (TaskStatus.INITIALIZING, TaskStatus.RUNNING):
        task = tasks.transition(
            app_env.context,
            episode,
            task["update_token"],
            status,
            "fixture",
            idempotency_key=status.value,
        )
    return service, provider, episode, service.register_space(app_env.context)["id"]


def rank(app_env, service, episode, space, **kwargs):
    return service.rank_transient(
        app_env.context,
        episode,
        space,
        QUERY,
        DOCUMENTS,
        repository_id=app_env.repository_id,
        **kwargs,
    )


def assert_private_inputs_not_retained(app_env):
    with app_env.db.transaction(app_env.context) as conn:
        for table in (
            "artifacts",
            "embedding_sources",
            "embedding_source_heads",
            "embedding_vectors",
        ):
            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
        for table in ("evidence_events", "budget_reservations", "budget_usage", "command_results"):
            records = conn.execute(f"SELECT row_to_json(t) AS row FROM {table} t").fetchall()
            retained = json.dumps(records, default=str)
            for text in (QUERY, *(text for _, text in DOCUMENTS)):
                assert text not in retained
    assert not any(path.is_file() for path in app_env.artifact_root.rglob("*"))


def test_transient_ranking_is_private_deterministic_and_reserved_before_preparation(app_env):
    service, provider, episode, space = setup_service(app_env)
    snapshots = []
    provider.before_prepare = lambda: snapshots.append(
        service.budgets.inspect(app_env.context, episode)
    )
    result = rank(app_env, service, episode, space, request_binding_sha256="f" * 64)
    assert result == {
        "schema": "potbot-transient-ranking/v1",
        "spaceId": str(space),
        "embeddingName": "general_text",
        "profileSha256": provider.profile.fingerprint,
        "querySha256": hashlib.sha256(QUERY.encode()).hexdigest(),
        "documentsSha256": content_hash(
            [{"id": identity, "text": text} for identity, text in sorted(DOCUMENTS)]
        ),
        "scores": [
            {"id": identity * 64, "scoreMicros": score}
            for identity, score in (
                ("a", 1_000_000),
                ("d", 1_000_000),
                ("c", 0),
                ("e", 0),
                ("b", -1_000_000),
            )
        ],
        "calls": 4,
        "inputTokens": 18,
        "elapsedMs": 12,
        "reservationIds": result["reservationIds"],
        "usageOrigin": "synthetic",
    }
    assert len(result["reservationIds"]) == 1
    assert all(
        snapshot["held"] == {"tokens": 1536, "calls": 4, "tool_ms": 60_000, "concurrency": 1}
        for snapshot in snapshots
    )
    assert [len(texts) for _, texts in provider.calls] == [1, 2, 2, 1]
    assert [kind for kind, _ in provider.calls] == ["query", "document", "document", "document"]
    assert tuple(text for _, texts in provider.calls[1:] for text in texts) == tuple(
        text for _, text in sorted(DOCUMENTS)
    )
    usage = service.budgets.inspect(app_env.context, episode)
    assert usage["used"] == {"calls": 4, "tokens": 18, "tool_ms": 12} and usage["held"] == {}
    with app_env.db.transaction(app_env.context) as conn:
        event = conn.execute(
            "SELECT details FROM evidence_events WHERE event_type='embedding_transient_ranking'"
        ).fetchone()["details"]
    assert event["scores_sha256"] == content_hash(
        sorted(result["scores"], key=lambda item: item["id"])
    )
    altered = [{**item, "scoreMicros": 0} for item in result["scores"]]
    assert event["scores_sha256"] != content_hash(sorted(altered, key=lambda item: item["id"]))
    assert "scores" not in event  # Only the exact output digest is retained by this owner.
    assert event["request_binding_sha256"] == "f" * 64
    assert_private_inputs_not_retained(app_env)
    reordered = service.rank_transient(
        app_env.context, episode, space, QUERY, tuple(reversed(DOCUMENTS))
    )
    assert (reordered["documentsSha256"], reordered["scores"]) == (
        result["documentsSha256"],
        result["scores"],
    )


def test_combined_budget_denial_never_loads_or_creates_corpus(app_env):
    service, provider, episode, space = setup_service(app_env, token_limit=1500)
    with pytest.raises(BudgetUnavailableError):
        rank(app_env, service, episode, space)
    assert provider.preparations == provider.calls == []
    assert service.budgets.inspect(app_env.context, episode)["held"] == {}
    assert_private_inputs_not_retained(app_env)


def test_scope_repository_named_view_and_profile_fail_before_dispatch(app_env):
    service, provider, episode, space = setup_service(app_env)
    with pytest.raises(ScopeDeniedError):
        service.rank_transient(
            app_env.context,
            episode,
            space,
            QUERY,
            DOCUMENTS,
            repository_id=app_env.other_repository_id,
        )
    with pytest.raises(ScopeDeniedError):
        rank(app_env, service, episode, space, embedding_name="missing")
    with pytest.raises(ScopeDeniedError):
        service.rank_transient(app_env.other_context, episode, space, QUERY, DOCUMENTS)
    provider.profile = replace(provider.profile, generation="other-space")
    with pytest.raises(InvalidInputError, match="incompatible"):
        rank(app_env, service, episode, space)
    assert provider.preparations == provider.calls == []
    assert service.budgets.inspect(app_env.context, episode)["held"] == {}


def test_cancellation_before_admission_and_after_result_never_returns_scores(app_env):
    service, provider, episode, space = setup_service(app_env)
    with pytest.raises(OperationCancelledError):
        rank(app_env, service, episode, space, cancelled=lambda: True)
    assert provider.preparations == provider.calls == []
    should_cancel = False

    def cancel_after_query():
        nonlocal should_cancel
        should_cancel = True

    provider.after_encode = cancel_after_query
    with pytest.raises(OperationCancelledError):
        rank(app_env, service, episode, space, cancelled=lambda: should_cancel)
    assert len(provider.calls) == 1
    assert service.budgets.inspect(app_env.context, episode)["used"]["calls"] == 1
    assert service.budgets.inspect(app_env.context, episode)["held"] == {}
    assert_private_inputs_not_retained(app_env)


def test_task_pause_during_query_charges_known_work_and_blocks_document_dispatch(app_env):
    service, provider, episode, space = setup_service(app_env)
    tasks = TaskService(app_env.db)

    def pause():
        current = tasks.inspect(app_env.context, episode)
        tasks.control(
            app_env.context,
            episode,
            current["update_token"],
            "pause",
            "user pause",
            idempotency_key="pause",
        )

    provider.after_encode = pause
    with pytest.raises(StateMismatchError, match="fenced"):
        rank(app_env, service, episode, space)
    assert len(provider.calls) == 1
    assert service.budgets.inspect(app_env.context, episode)["used"]["calls"] == 1
    with pytest.raises(StateMismatchError):
        rank(app_env, service, episode, space)
    assert len(provider.calls) == 1
    assert_private_inputs_not_retained(app_env)


def test_provider_profile_drift_after_dispatch_is_charged_and_fenced(app_env):
    service, provider, episode, space = setup_service(app_env)
    provider.after_encode = lambda: setattr(
        provider, "profile", replace(provider.profile, generation="drift")
    )
    with pytest.raises(InvalidInputError, match="changed"):
        rank(app_env, service, episode, space)
    assert len(provider.calls) == 1
    assert service.budgets.inspect(app_env.context, episode)["used"]["calls"] == 1
    assert service.budgets.inspect(app_env.context, episode)["held"] == {}
    assert_private_inputs_not_retained(app_env)


def test_uncertain_second_batch_holds_combined_reservation_without_private_payload(app_env):
    service, provider, episode, space = setup_service(app_env)

    def fail_second():
        if len(provider.calls) == 2:
            raise RuntimeError("synthetic provider lost its response")

    provider.after_encode = fail_second
    with pytest.raises(RuntimeError):
        rank(app_env, service, episode, space)
    usage = service.budgets.inspect(app_env.context, episode)
    assert usage["used"] == {}
    assert usage["held"] == {"tokens": 1536, "calls": 4, "tool_ms": 60_000, "concurrency": 1}
    with app_env.db.transaction(app_env.context) as conn:
        event = conn.execute(
            "SELECT details FROM evidence_events WHERE event_type='embedding_transient_ranking'"
        ).fetchone()["details"]
        assert event["outcome"] == "indeterminate" and event["known_calls"] == 1
        assert event["usage_origin"] == "synthetic"
        assert "scores_sha256" not in event
        assert "request_binding_sha256" not in event
    assert_private_inputs_not_retained(app_env)


@pytest.mark.parametrize("invalid_result", ["wrong_batch_size", "excess_input_tokens", "untyped"])
def test_rejected_typed_result_still_charges_confirmed_usage(app_env, invalid_result):
    class InvalidResultProvider(SyntheticTransientProvider):
        def encode(self, texts, kind):
            result = super().encode(texts, kind)
            if invalid_result == "wrong_batch_size":
                return replace(result, vectors=())
            if invalid_result == "untyped":
                return object()
            return replace(result, input_tokens=self.profile.max_input_tokens + 1)

    service, provider, episode, space = setup_service(app_env, InvalidResultProvider())
    with pytest.raises(InvalidInputError) as failure:
        rank(app_env, service, episode, space)
    usage = service.budgets.inspect(app_env.context, episode)
    assert failure.value.embedding_usage_indeterminate is (invalid_result == "untyped")
    if invalid_result == "untyped":
        assert usage["held"]["calls"] == 4 and usage["used"] == {}
    else:
        assert usage["held"] == {}
        assert usage["used"] == {
            "calls": 1,
            "tokens": 3 if invalid_result == "wrong_batch_size" else 257,
            "tool_ms": 3,
        }
    assert len(provider.calls) == 1
    assert_private_inputs_not_retained(app_env)


def test_supervised_batches_share_one_deadline_and_do_not_tokenize_in_parent(app_env, monkeypatch):
    import potbot.memory.embeddings as module

    service, provider, episode, space = setup_service(app_env, SupervisedTransientFixture())
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    provider.after_encode = lambda: clock.__setitem__(0, clock[0] + 0.1)
    result = rank(app_env, service, episode, space, runtime_budget_ms=1000)
    assert result["usageOrigin"] == "synthetic" and result["calls"] == 4
    assert provider.preparations == []
    assert 0 < provider.timeouts[-1] < provider.timeouts[0] <= 1000
    assert provider.timeouts == sorted(provider.timeouts, reverse=True)
    assert_private_inputs_not_retained(app_env)


def test_result_after_total_deadline_is_charged_and_rejected(app_env, monkeypatch):
    import potbot.memory.embeddings as module

    service, provider, episode, space = setup_service(app_env, SupervisedTransientFixture())
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    provider.after_encode = lambda: clock.__setitem__(0, 102.0)
    with pytest.raises(OperationTimeoutError):
        rank(app_env, service, episode, space, runtime_budget_ms=1000)
    assert len(provider.calls) == 1
    assert service.budgets.inspect(app_env.context, episode)["used"]["calls"] == 1
    assert service.budgets.inspect(app_env.context, episode)["held"] == {}
    assert_private_inputs_not_retained(app_env)


def test_missing_supervised_model_releases_known_unused_capacity_without_download(
    app_env, tmp_path
):
    provider = SupervisedQwenEncoder(tmp_path / "missing-model")
    service, _, episode, space = setup_service(app_env, provider)
    with pytest.raises(SupervisedEncodingFailure) as failure:
        rank(app_env, service, episode, space)
    assert not failure.value.dispatched and failure.value.result.processes_stopped
    usage = service.budgets.inspect(app_env.context, episode)
    assert usage["used"]["calls"] == usage["used"]["tokens"] == 0 and usage["held"] == {}
    assert_private_inputs_not_retained(app_env)


@pytest.mark.sandbox
def test_real_supervised_deadline_holds_uncertain_usage_and_no_private_artifact(app_env, tmp_path):
    from tests.integration.test_supervised_embeddings import BlockedStageSupervisor

    model = tmp_path / "controlled-model"
    model.mkdir()
    supervisor = BlockedStageSupervisor("transient ranking blocked worker")
    provider = SupervisedQwenEncoder(model, supervisor=supervisor)
    service, _, episode, space = setup_service(app_env, provider)
    with pytest.raises(SupervisedEncodingFailure):
        rank(app_env, service, episode, space, runtime_budget_ms=350)
    assert supervisor.last_result.outcome == "timeout", supervisor.last_result
    assert supervisor.last_result.processes_stopped
    usage = BudgetLedger(app_env.db).inspect(app_env.context, episode)
    assert usage["held"]["calls"] == 3 and usage["used"] == {}
    assert_private_inputs_not_retained(app_env)
