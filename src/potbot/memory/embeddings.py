"""Budgeted local embedding ingestion and exact, scoped pgvector retrieval.

This is the initial standard embedding channel. Its sources are explicitly
registered evidence text; learned memory admission and hybrid ranking are later
phases. A source head is checked again under lock before a vector is published.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.domain.auth import WorkspaceContext
from potbot.domain.embeddings import (
    DEFAULT_EMBEDDING_NAME,
    SOURCE_TEXT_INPUT_FORMAT,
    validate_embedding_name,
)
from potbot.domain.errors import (
    InvalidInputError,
    OperationCancelledError,
    OperationTimeoutError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.memory.embedding_profile import (
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingProvider,
    SupervisedEmbeddingProvider,
    validate_vector,
)
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database
from potbot.persistence.embedding_sources import register_source_in_transaction
from potbot.persistence.evidence import EvidenceStore


def _transient_inputs(
    query: str, candidates: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    """Bound transient input before either database admission or model loading."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise InvalidInputError("transient ranking query must contain 1..500 characters")
    if not isinstance(candidates, tuple) or not 1 <= len(candidates) <= 16:
        raise InvalidInputError("transient ranking requires 1..16 candidate documents")
    identities: set[str] = set()
    total_bytes = 0
    try:
        total_bytes += len(query.encode("utf-8"))
        for candidate in candidates:
            if not isinstance(candidate, tuple) or len(candidate) != 2:
                raise InvalidInputError("transient candidates must be identity/text pairs")
            identity, text = candidate
            if (
                not isinstance(identity, str)
                or re.fullmatch(r"[0-9a-f]{64}", identity) is None
                or identity in identities
            ):
                raise InvalidInputError("transient candidate identities must be unique SHA256s")
            identities.add(identity)
            if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                raise InvalidInputError("transient document must contain 1..4096 characters")
            total_bytes += len(text.encode("utf-8"))
    except UnicodeError as error:
        raise InvalidInputError("transient input must be valid UTF-8 text") from error
    if total_bytes > 65_536:
        raise InvalidInputError("transient ranking inputs exceed the 64 KiB byte limit")
    return tuple(sorted(candidates))


def _cosine_micros(query: tuple[float, ...], document: tuple[float, ...]) -> int:
    """Return signed, normalized cosine without platform-dependent float output."""
    query = validate_vector(query)
    document = validate_vector(document)
    numerator = math.fsum(a * b for a, b in zip(query, document, strict=True))
    denominator = math.sqrt(math.fsum(a * a for a in query)) * math.sqrt(
        math.fsum(b * b for b in document)
    )
    return round(max(-1.0, min(1.0, numerator / denominator)) * 1_000_000)


class EmbeddingService:
    def __init__(
        self, database: Database, artifacts: ArtifactStore, provider: EmbeddingProvider
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.provider = provider
        self.budgets = BudgetLedger(database)

    def register_space(
        self,
        context: WorkspaceContext,
        profile: EmbeddingProfile | None = None,
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
    ) -> dict[str, Any]:
        """Register an immutable profile and a named raw-text view of that space.

        Names select representations; the profile fingerprint still defines
        coordinate compatibility. Several names can bind the same exact space,
        and a name can retain several explicitly selected space generations.
        """
        from psycopg.types.json import Jsonb

        validate_embedding_name(embedding_name)
        profile = profile or self.provider.profile
        with self.database.transaction(context) as conn:
            conn.execute(
                "INSERT INTO embedding_spaces(workspace_id,id,content_hash,content,dimensions,metric) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,content_hash) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    profile.fingerprint,
                    Jsonb(profile.to_dict()),
                    profile.dimensions,
                    profile.metric,
                ),
            )
            row = conn.execute(
                "SELECT * FROM embedding_spaces WHERE workspace_id=%s AND content_hash=%s",
                (context.workspace_id, profile.fingerprint),
            ).fetchone()
            conn.execute(
                "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id,input_format) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,embedding_name,space_id) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    embedding_name,
                    row["id"],
                    SOURCE_TEXT_INPUT_FORMAT,
                ),
            )
            view = conn.execute(
                "SELECT id,input_format FROM embedding_views WHERE workspace_id=%s AND embedding_name=%s AND space_id=%s",
                (context.workspace_id, embedding_name, row["id"]),
            ).fetchone()
            return {
                **dict(row),
                "embedding_name": embedding_name,
                "view_id": view["id"],
                "input_format": view["input_format"],
            }

    def register_source(
        self,
        context: WorkspaceContext,
        repository_id: UUID,
        source_key: str,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return register_source_in_transaction(
                conn, context, self.artifacts, repository_id, source_key, text, metadata=metadata
            )

    def retire_source(
        self, context: WorkspaceContext, repository_id: UUID, source_key: str
    ) -> None:
        with self.database.transaction(context) as conn:
            result = conn.execute(
                "UPDATE embedding_source_heads SET source_id=NULL,active=false,generation=generation+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND repository_id=%s AND source_key=%s RETURNING id",
                (context.workspace_id, repository_id, source_key),
            ).fetchone()
            if result is None:
                raise ScopeDeniedError("record is unavailable")

    def _space(
        self, conn: Any, context: WorkspaceContext, space_id: UUID, profile: EmbeddingProfile
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM embedding_spaces WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, space_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("record is unavailable")
        if (
            row["content_hash"] != profile.fingerprint
            or content_hash(row["content"]) != row["content_hash"]
        ):
            raise InvalidInputError("provider and stored embedding spaces are incompatible")
        return dict(row)

    def _view(
        self,
        conn: Any,
        context: WorkspaceContext,
        space_id: UUID,
        embedding_name: str,
        profile: EmbeddingProfile,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        space = self._space(conn, context, space_id, profile)
        row = conn.execute(
            "SELECT * FROM embedding_views WHERE workspace_id=%s AND embedding_name=%s AND space_id=%s",
            (context.workspace_id, embedding_name, space_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("named embedding view is unavailable")
        if row["input_format"] != SOURCE_TEXT_INPUT_FORMAT:
            raise InvalidInputError("unsupported named embedding input format")
        return space, dict(row)

    def _describe_vector(
        self,
        vector: Any,
        source: Any,
        space: dict[str, Any],
        view: dict[str, Any],
        profile: EmbeddingProfile,
    ) -> dict[str, Any]:
        prepared = profile.prepare(source["content"], "document")
        return {
            **dict(vector),
            "artifact_id": source["artifact_id"],
            "embedding_name": view["embedding_name"],
            "view_id": view["id"],
            "input_format": view["input_format"],
            "profile_hash": space["content_hash"],
            "encoding_input_hash": hashlib.sha256(prepared.encode("utf-8")).hexdigest(),
            "dimensions": space["dimensions"],
            "metric": space["metric"],
            "generation": space["content"]["generation"],
        }

    def _assert_provider(self, provider: EmbeddingProvider, profile: EmbeddingProfile) -> None:
        if self.provider is not provider or provider.profile.fingerprint != profile.fingerprint:
            raise InvalidInputError("embedding provider or profile changed during the operation")

    @staticmethod
    def _episode_repository(
        conn: Any, context: WorkspaceContext, episode_id: UUID, repository_id: UUID
    ) -> None:
        if (
            conn.execute(
                "SELECT id FROM episodes WHERE workspace_id=%s AND id=%s AND repository_id=%s",
                (context.workspace_id, episode_id, repository_id),
            ).fetchone()
            is None
        ):
            raise ScopeDeniedError("record is unavailable")

    def _encode(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        text: str,
        kind: Literal["query", "document"],
        runtime_budget_ms: int,
        *,
        provider: EmbeddingProvider,
        profile: EmbeddingProfile,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[EmbeddingBatch, UUID]:
        if type(runtime_budget_ms) is not int or not 1 <= runtime_budget_ms <= 3_600_000:
            raise InvalidInputError("embedding runtime reservation must be bounded")
        # Reserve the configured maximum before model/tokenizer loading. This
        # deliberately conservative initial adapter never spends preparation
        # compute before admission; confirmed unused tokens are then released.
        self._assert_provider(provider, profile)
        profile.prepare(text, kind)
        reservation = self.budgets.reserve(
            context,
            episode_id,
            {
                "tokens": profile.max_input_tokens,
                "calls": 1,
                "tool_ms": runtime_budget_ms,
                "concurrency": 1,
            },
            f"embedding-{uuid4()}",
        )
        reservation_id = UUID(str(reservation["id"]))
        started = time.monotonic()
        encoding_started = False
        supervised = isinstance(provider, SupervisedEmbeddingProvider)
        try:
            if isinstance(provider, SupervisedEmbeddingProvider):
                encoding_started = True

                def should_cancel() -> bool:
                    if cancelled is not None and cancelled():
                        return True
                    with self.database.transaction(context) as conn:
                        row = conn.execute(
                            "SELECT status FROM episodes WHERE workspace_id=%s AND id=%s",
                            (context.workspace_id, episode_id),
                        ).fetchone()
                        return row is None or row["status"] != "running"

                result = provider.encode_supervised(
                    (text,), kind, timeout_ms=runtime_budget_ms, cancelled=should_cancel
                )
            else:
                # Synthetic/test providers retain their explicit accounting-only
                # contract. Production local Qwen always selects the supervised path.
                tokens = provider.count_tokens((text,), kind)
                self._assert_provider(provider, profile)
                if type(tokens) is not int or not 0 < tokens <= profile.max_input_tokens:
                    raise InvalidInputError("provider input exceeds the registered token bound")
                encoding_started = True
                result = provider.encode((text,), kind)
            if not isinstance(result, EmbeddingBatch):
                raise InvalidInputError("embedding provider returned an untyped result")
            if not 0 < result.input_tokens <= profile.max_input_tokens:
                raise InvalidInputError("embedding provider returned invalid input usage")
        except BaseException as error:
            process_result = getattr(error, "result", None)
            stop_uncertain = process_result is not None and not process_result.processes_stopped
            if getattr(error, "dispatched", encoding_started) or stop_uncertain:
                # Missing usage after dispatch is never assumed to be free.
                # A missing stop proof also prevents releasing the reservation,
                # even if startup acknowledgement was lost before dispatch.
                self.budgets.mark_indeterminate(
                    context, reservation_id, "embedding provider did not return confirmed usage"
                )
            else:
                self.budgets.settle(
                    context,
                    reservation_id,
                    {"tokens": 0, "calls": 0, "tool_ms": int((time.monotonic() - started) * 1000)},
                    origin="measured",
                )
            if supervised:
                EvidenceStore(self.database).append(
                    context,
                    "embedding_worker_outcome",
                    episode_id=episode_id,
                    producer_id="potbot.local_embedding_supervisor",
                    details={
                        "reservation_id": str(reservation_id),
                        "profile_hash": profile.fingerprint,
                        "input_kind": kind,
                        "input_hash": hashlib.sha256(
                            profile.prepare(text, kind).encode()
                        ).hexdigest(),
                        "outcome": getattr(process_result, "outcome", "inconclusive"),
                        "error_code": str(getattr(error, "code", "internal_error")),
                        "dispatched": bool(getattr(error, "dispatched", encoding_started)),
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "isolation_identity": getattr(process_result, "isolation_identity", None),
                        "processes_stopped": getattr(process_result, "processes_stopped", False),
                    },
                )
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.budgets.settle(
            context,
            reservation_id,
            {
                "tokens": result.input_tokens,
                "calls": 1,
                "tool_ms": elapsed_ms if result.usage_origin == "measured" else result.elapsed_ms,
            },
            origin=result.usage_origin,
        )
        if supervised:
            EvidenceStore(self.database).append(
                context,
                "embedding_worker_outcome",
                episode_id=episode_id,
                producer_id="potbot.local_embedding_supervisor",
                details={
                    "reservation_id": str(reservation_id),
                    "profile_hash": profile.fingerprint,
                    "input_kind": kind,
                    "input_hash": hashlib.sha256(profile.prepare(text, kind).encode()).hexdigest(),
                    "outcome": "completed",
                    "dispatched": True,
                    "elapsed_ms": elapsed_ms,
                    "publication": "pending_source_and_task_fence",
                },
            )
        # Confirmed compute is charged even when a mutable provider changed its
        # configuration during dispatch. Such a vector never enters the old space.
        self._assert_provider(provider, profile)
        if len(result.vectors) != 1:
            raise InvalidInputError("embedding provider returned the wrong batch size")
        validate_vector(result.vectors[0])
        return result, reservation_id

    @staticmethod
    def _publication_allowed(conn: Any, context: WorkspaceContext, episode_id: UUID) -> None:
        row = conn.execute(
            "SELECT status FROM episodes WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (context.workspace_id, episode_id),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise StateMismatchError("embedding operation was fenced before publication")

    @staticmethod
    def _transient_task_binding(
        conn: Any,
        context: WorkspaceContext,
        episode_id: UUID,
        repository_id: UUID | None,
    ) -> UUID:
        row = conn.execute(
            "SELECT repository_id,status FROM episodes WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (context.workspace_id, episode_id),
        ).fetchone()
        if row is None or (repository_id is not None and row["repository_id"] != repository_id):
            raise ScopeDeniedError("transient ranking task repository is unavailable")
        if row["status"] != "running":
            raise StateMismatchError("transient ranking was fenced because the task is not running")
        return UUID(str(row["repository_id"]))

    def rank_transient(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        space_id: UUID,
        query: str,
        candidates: tuple[tuple[str, str], ...],
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
        runtime_budget_ms: int = 60_000,
        repository_id: UUID | None = None,
        request_binding_sha256: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Rank an already authorized shortlist without publishing private text.

        Query and document vectors exist only in this call's memory. The existing
        named space, provider, task budget and supervised worker remain the only
        owners. Retained accounting/evidence contain hashes and usage, never
        texts, candidate vectors, sources, artifacts or a second search index.

        A single reservation covers all texts before preparation can load a
        tokenizer. Calls respect the exact registered profile's batch limit:
        one query call and at most sixteen document calls under one deadline.
        Unsupervised fixture providers must label their synthetic usage.
        """
        validate_embedding_name(embedding_name)
        documents = _transient_inputs(query, candidates)
        if (
            not isinstance(episode_id, UUID)
            or not isinstance(space_id, UUID)
            or (repository_id is not None and not isinstance(repository_id, UUID))
        ):
            raise InvalidInputError("transient ranking requires UUID task, space and repository")
        if type(runtime_budget_ms) is not int or not 1 <= runtime_budget_ms <= 120_000:
            raise InvalidInputError("transient ranking runtime must be 1..120000 milliseconds")
        if request_binding_sha256 is not None and (
            not isinstance(request_binding_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_binding_sha256) is None
        ):
            raise InvalidInputError("transient ranking request binding must be lowercase SHA256")
        provider = self.provider
        profile = provider.profile
        self._assert_provider(provider, profile)
        profile.prepare(query, "query")
        for _, text in documents:
            profile.prepare(text, "document")
        if cancelled is not None and cancelled():
            raise OperationCancelledError("transient ranking was cancelled before admission")
        batches: list[tuple[Literal["query", "document"], tuple[str, ...]]] = [("query", (query,))]
        for offset in range(0, len(documents), profile.max_batch_size):
            batches.append(
                (
                    "document",
                    tuple(text for _, text in documents[offset : offset + profile.max_batch_size]),
                )
            )
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        documents_hash = content_hash([{"id": key, "text": text} for key, text in documents])
        with self.database.transaction(context) as conn:
            self._view(conn, context, space_id, embedding_name, profile)
            repository_id = self._transient_task_binding(conn, context, episode_id, repository_id)
            self._assert_provider(provider, profile)
            reservation = self.budgets.reserve_in_transaction(
                conn,
                context,
                episode_id,
                {
                    "tokens": (len(documents) + 1) * profile.max_input_tokens,
                    "calls": len(batches),
                    "tool_ms": runtime_budget_ms,
                    "concurrency": 1,
                },
                f"embedding-transient-{uuid4()}",
            )
        reservation_id = UUID(str(reservation["id"]))
        started = time.monotonic()
        deadline = started + runtime_budget_ms / 1000
        supervised = isinstance(provider, SupervisedEmbeddingProvider)
        calls = input_tokens = reported_ms = 0
        usage_origin: str | None = None
        dispatched = False
        vectors: list[tuple[float, ...]] = []

        def elapsed() -> int:
            return max(0, int((time.monotonic() - started) * 1000))

        def fence() -> int:
            self._assert_provider(provider, profile)
            if cancelled is not None and cancelled():
                raise OperationCancelledError("transient ranking was cancelled before return")
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining < 1:
                raise OperationTimeoutError("transient ranking exceeded its total runtime")
            with self.database.transaction(context) as conn:
                self._transient_task_binding(conn, context, episode_id, repository_id)
                self._view(conn, context, space_id, embedding_name, profile)
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining < 1:
                raise OperationTimeoutError("transient ranking exceeded its total runtime")
            return remaining

        def should_cancel() -> bool:
            if cancelled is not None and cancelled():
                return True
            if (
                time.monotonic() >= deadline
                or self.provider is not provider
                or provider.profile.fingerprint != profile.fingerprint
            ):
                return True
            with self.database.transaction(context) as conn:
                row = conn.execute(
                    "SELECT status,repository_id FROM episodes WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, episode_id),
                ).fetchone()
                return (
                    row is None
                    or row["status"] != "running"
                    or row["repository_id"] != repository_id
                )

        def settle_known() -> int:
            accounted_ms = elapsed() if usage_origin in {None, "measured"} else reported_ms
            self.budgets.settle(
                context,
                reservation_id,
                {"tokens": input_tokens, "calls": calls, "tool_ms": accounted_ms},
                origin=usage_origin or "measured",
            )
            return accounted_ms

        def record(
            outcome: str,
            error: BaseException | None = None,
            *,
            scores_sha256: str | None = None,
        ) -> None:
            process_result = getattr(error, "result", None)
            EvidenceStore(self.database).append(
                context,
                "embedding_transient_ranking",
                episode_id=episode_id,
                producer_id="potbot.local_embedding_supervisor"
                if supervised
                else "potbot.embedding",
                details={
                    "schema": "potbot-transient-ranking-evidence/v1",
                    "reservation_id": str(reservation_id),
                    "space_id": str(space_id),
                    "embedding_name": embedding_name,
                    "profile_hash": profile.fingerprint,
                    "query_hash": query_hash,
                    "documents_hash": documents_hash,
                    "document_count": len(documents),
                    "outcome": outcome,
                    "known_calls": calls,
                    "known_input_tokens": input_tokens,
                    "usage_origin": usage_origin,
                    "elapsed_ms": elapsed(),
                    "supervised": supervised,
                    "error_code": str(getattr(error, "code", "internal_error")) if error else None,
                    "worker_outcome": getattr(process_result, "outcome", None),
                    "dispatched": bool(calls or getattr(error, "dispatched", dispatched)),
                    "processes_stopped": getattr(process_result, "processes_stopped", None),
                    **({"scores_sha256": scores_sha256} if scores_sha256 is not None else {}),
                    **(
                        {"request_binding_sha256": request_binding_sha256}
                        if request_binding_sha256 is not None
                        else {}
                    ),
                },
            )

        try:
            for kind, texts in batches:
                remaining = fence()
                if remaining < 1:
                    raise OperationTimeoutError("transient ranking exceeded its total runtime")
                if isinstance(provider, SupervisedEmbeddingProvider):
                    dispatched = True
                    result = provider.encode_supervised(
                        texts, kind, timeout_ms=remaining, cancelled=should_cancel
                    )
                else:
                    # This path supports explicit synthetic providers in offline
                    # acceptance. Production selection uses the supervised owner.
                    tokens = provider.count_tokens(texts, kind)
                    self._assert_provider(provider, profile)
                    if (
                        type(tokens) is not int
                        or not 0 < tokens <= len(texts) * profile.max_input_tokens
                    ):
                        raise InvalidInputError("provider input exceeds the registered token bound")
                    fence()
                    dispatched = True
                    result = provider.encode(texts, kind)
                if not isinstance(result, EmbeddingBatch):
                    raise InvalidInputError("embedding provider returned an untyped result")
                if usage_origin is not None and result.usage_origin != usage_origin:
                    raise InvalidInputError("transient batches returned inconsistent usage origins")
                usage_origin = result.usage_origin
                input_tokens += result.input_tokens
                reported_ms += result.elapsed_ms
                calls += 1
                dispatched = (
                    False  # A typed result confirms usage even if its vectors are rejected.
                )
                if not 0 < result.input_tokens <= len(texts) * profile.max_input_tokens:
                    raise InvalidInputError("embedding provider returned invalid input usage")
                if len(result.vectors) != len(texts):
                    raise InvalidInputError("embedding provider returned the wrong batch size")
                vectors.extend(validate_vector(vector) for vector in result.vectors)
                fence()
            query_vector, *document_vectors = vectors
            scored_documents = sorted(
                (
                    (_cosine_micros(query_vector, vector), identity)
                    for (identity, _), vector in zip(documents, document_vectors, strict=True)
                ),
                key=lambda row: (-row[0], row[1]),
            )
            scores = [
                {"id": identity, "scoreMicros": score} for score, identity in scored_documents
            ]
            fence()
        except BaseException as error:
            process_result = getattr(error, "result", None)
            stop_uncertain = process_result is not None and not process_result.processes_stopped
            if getattr(error, "dispatched", dispatched) or stop_uncertain:
                self.budgets.mark_indeterminate(
                    context, reservation_id, "transient encoding did not return confirmed usage"
                )
                error.embedding_usage_indeterminate = True  # type: ignore[attr-defined]
                record("indeterminate", error)
            else:
                settle_known()
                error.embedding_usage_indeterminate = False  # type: ignore[attr-defined]
                record("rejected", error)
            raise
        accounted_ms = settle_known()
        try:
            # Settlement can exhaust a task. Neither a confirmed vector nor
            # successful accounting permits a late result after revocation.
            fence()
        except BaseException as error:
            error.embedding_usage_indeterminate = False  # type: ignore[attr-defined]
            record("fenced", error)
            raise
        record("completed", scores_sha256=content_hash(sorted(scores, key=lambda item: item["id"])))
        return {
            "schema": "potbot-transient-ranking/v1",
            "spaceId": str(space_id),
            "embeddingName": embedding_name,
            "profileSha256": profile.fingerprint,
            "querySha256": query_hash,
            "documentsSha256": documents_hash,
            "scores": scores,
            "calls": calls,
            "inputTokens": input_tokens,
            "elapsedMs": accounted_ms,
            "reservationIds": [str(reservation_id)],
            "usageOrigin": usage_origin,
        }

    def index_source(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        source_id: UUID,
        space_id: UUID,
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
        runtime_budget_ms: int = 60_000,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        validate_embedding_name(embedding_name)
        provider = self.provider
        profile = provider.profile
        with self.database.transaction(context) as conn:
            space, view = self._view(conn, context, space_id, embedding_name, profile)
            source = conn.execute(
                "SELECT s.*,h.generation FROM embedding_sources s JOIN embedding_source_heads h ON (h.workspace_id,h.source_id)=(s.workspace_id,s.id) WHERE s.workspace_id=%s AND s.id=%s AND h.active",
                (context.workspace_id, source_id),
            ).fetchone()
            if source is None:
                raise ScopeDeniedError("record is unavailable")
            self._episode_repository(conn, context, episode_id, source["repository_id"])
            existing = conn.execute(
                "SELECT id,source_id,space_id,embedding_name,content_hash,usage_origin FROM embedding_vectors WHERE workspace_id=%s AND source_id=%s AND embedding_name=%s AND space_id=%s",
                (context.workspace_id, source_id, embedding_name, space_id),
            ).fetchone()
            if existing is not None:
                return self._describe_vector(existing, source, space, view, profile)
        result, reservation_id = self._encode(
            context,
            episode_id,
            source["content"],
            "document",
            runtime_budget_ms,
            provider=provider,
            profile=profile,
            cancelled=cancelled,
        )
        with self.database.transaction(context) as conn:
            self._publication_allowed(conn, context, episode_id)
            self._assert_provider(provider, profile)
            head = conn.execute(
                "SELECT * FROM embedding_source_heads WHERE workspace_id=%s AND repository_id=%s AND source_key=%s FOR UPDATE",
                (context.workspace_id, source["repository_id"], source["source_key"]),
            ).fetchone()
            if (
                head is None
                or not head["active"]
                or head["source_id"] != source_id
                or head["generation"] != source["generation"]
            ):
                raise RevisionConflictError("source changed or was retired while encoding")
            conn.execute(
                "INSERT INTO embedding_vectors(workspace_id,id,source_id,space_id,embedding_name,embedding,content_hash,reservation_id,usage_origin) VALUES (%s,%s,%s,%s,%s,%s::public.vector,%s,%s,%s) ON CONFLICT (workspace_id,source_id,embedding_name,space_id) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    source_id,
                    space_id,
                    embedding_name,
                    json.dumps(result.vectors[0]),
                    source["content_hash"],
                    reservation_id,
                    result.usage_origin,
                ),
            )
            row = conn.execute(
                "SELECT id,source_id,space_id,embedding_name,content_hash,usage_origin FROM embedding_vectors WHERE workspace_id=%s AND source_id=%s AND embedding_name=%s AND space_id=%s",
                (context.workspace_id, source_id, embedding_name, space_id),
            ).fetchone()
            return self._describe_vector(row, source, space, view, profile)

    def search(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        repository_id: UUID,
        space_id: UUID,
        query: str,
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
        limit: int = 5,
        runtime_budget_ms: int = 60_000,
        source_ids: tuple[UUID, ...] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        validate_embedding_name(embedding_name)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("retrieval limit must be between 1 and 100")
        if source_ids is not None and (
            not isinstance(source_ids, tuple)
            or len(source_ids) > 100_000
            or any(not isinstance(identity, UUID) for identity in source_ids)
            or len(set(source_ids)) != len(source_ids)
        ):
            raise InvalidInputError("source filter requires at most 100000 unique UUIDs")
        provider = self.provider
        profile = provider.profile
        with self.database.transaction(context) as conn:
            space, view = self._view(conn, context, space_id, embedding_name, profile)
            self._episode_repository(conn, context, episode_id, repository_id)
            if (
                conn.execute(
                    "SELECT id FROM repositories WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, repository_id),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
        if source_ids == ():
            return []
        result, _ = self._encode(
            context,
            episode_id,
            query,
            "query",
            runtime_budget_ms,
            provider=provider,
            profile=profile,
            cancelled=cancelled,
        )
        with self.database.transaction(context) as conn:
            self._publication_allowed(conn, context, episode_id)
            self._assert_provider(provider, profile)
            # No approximate index is used in this first integration. Rejoining
            # current heads prevents retired or superseded sources reappearing.
            rows = conn.execute(
                "SELECT s.id,s.source_key,s.artifact_id,s.content,s.metadata,s.content_hash AS source_content_hash,v.id AS vector_id,v.space_id,v.embedding_name,v.usage_origin,v.embedding OPERATOR(public.<=>) %s::public.vector AS distance FROM embedding_vectors v JOIN embedding_sources s ON (s.workspace_id,s.id)=(v.workspace_id,v.source_id) JOIN embedding_source_heads h ON (h.workspace_id,h.source_id)=(s.workspace_id,s.id) WHERE v.workspace_id=%s AND v.space_id=%s AND v.embedding_name=%s AND s.repository_id=%s AND h.active AND v.content_hash=s.content_hash AND (%s::uuid[] IS NULL OR s.id=ANY(%s::uuid[])) ORDER BY distance,s.id LIMIT %s",
                (
                    json.dumps(result.vectors[0]),
                    context.workspace_id,
                    space_id,
                    embedding_name,
                    repository_id,
                    None if source_ids is None else list(source_ids),
                    None if source_ids is None else list(source_ids),
                    limit,
                ),
            ).fetchall()
            return [self._describe_vector(row, row, space, view, profile) for row in rows]
