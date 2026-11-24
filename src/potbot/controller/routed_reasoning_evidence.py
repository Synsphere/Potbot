"""Resolve one explicit selection of retained T05 citations for a routed plan."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from potbot.controller.research_evidence import ResearchEvidence
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.research_evidence import citation_spec, identity, timestamp
from potbot.domain.serialization import content_hash
from potbot.research.contracts import ResearchScope
from potbot.semantics.releases import read_task_pin

_PACKET_KIND = "potbot-routed-reasoning-citations/v1"
_MAX_CITATIONS = 4
_MAX_QUOTE_BYTES = 1024


def citation_selection(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "citations",
        "repository_state_id",
        "allow_hosted_disclosure",
    }:
        raise InvalidInputError("Evidence selection requires citations, repository and disclosure")
    identity(value["repository_state_id"])
    requested = value["citations"]
    if (
        type(value["allow_hosted_disclosure"]) is not bool
        or not isinstance(requested, list)
        or not 1 <= len(requested) <= _MAX_CITATIONS
    ):
        raise InvalidInputError("Select one to four citations and an explicit disclosure choice")
    citations = [citation_spec(spec) for spec in requested]
    if len({content_hash(spec) for spec in citations}) != len(citations) or any(
        spec["endByte"] - spec["startByte"] > _MAX_QUOTE_BYTES for spec in citations
    ):
        raise InvalidInputError("Select distinct citations of at most 1024 UTF-8 bytes each")
    return {**value, "citations": citations}


def is_citation_packet(value: object) -> bool:
    return isinstance(value, dict) and value.get("kind") == _PACKET_KIND


def matches_frozen_plan(
    retained: dict[str, Any],
    retained_digest: str,
    requested: dict[str, Any],
    selection: dict[str, Any] | None,
) -> bool:
    evidence = retained["evidence"]
    packet = evidence[-1] if evidence and is_citation_packet(evidence[-1]) else None
    if selection is None:
        # Legacy runs had no reserved packet. Their original plan still retries exactly.
        return content_hash(requested) == retained_digest
    return (
        packet is not None
        and packet.get("selection_sha256") == content_hash(selection)
        and content_hash({**retained, "evidence": evidence[:-1]}) == content_hash(requested)
        and content_hash(retained) == retained_digest
    )


def freeze_citations(
    conn: Any,
    actor: WorkspaceContext,
    episode_id: UUID,
    selection: dict[str, Any],
    research: ResearchEvidence,
) -> dict[str, Any]:
    repository_state_id = identity(selection["repository_state_id"])
    # Read the selected repository snapshot under the same task lock that keeps
    # its contract live until the new run and its frozen evidence commit.
    task = conn.execute(
        "SELECT e.active_contract_id FROM episodes e JOIN repository_states r "
        "ON r.workspace_id=e.workspace_id AND r.repository_id=e.repository_id "
        "WHERE e.workspace_id=%s AND e.id=%s AND e.status='running' "
        "AND e.active_contract_id IS NOT NULL AND r.id=%s FOR SHARE OF e",
        (actor.workspace_id, episode_id, repository_state_id),
    ).fetchone()
    if task is None:
        raise ScopeDeniedError("Citation snapshot does not belong to the running task")
    pin = read_task_pin(conn, actor, episode_id)
    assert pin is not None
    cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    items = []
    for spec in selection["citations"]:
        citation = research.citation(actor, spec, cache=cache)
        scope = ResearchScope.from_dict(citation["researchScope"])
        if (
            scope.workspace_id != actor.workspace_id
            or scope.task_id != episode_id
            or scope.contract_revision_id != task["active_contract_id"]
            or scope.repository_state_id != repository_state_id
            or scope.semantic_release_digest != pin["content"]["release_digest"]
        ):
            raise ScopeDeniedError("Citation belongs to a different task, snapshot or semantic pin")
        need = conn.execute(
            "SELECT s.current_revision_id,s.status FROM information_needs n "
            "JOIN need_status s ON s.workspace_id=n.workspace_id AND s.need_id=n.id "
            "WHERE n.workspace_id=%s AND n.episode_id=%s AND n.id=%s FOR SHARE OF s",
            (actor.workspace_id, episode_id, scope.need_id),
        ).fetchone()
        if (
            need is None
            or need["status"] != "open"
            or need["current_revision_id"] != scope.need_revision_id
        ):
            raise StateMismatchError("Citation needs its original open information-need revision")
        freshness = citation["freshnessSeconds"]
        published = citation["publishedAt"]
        if freshness and (
            published is None
            or not 0 <= (datetime.now(UTC) - timestamp(published)).total_seconds() <= freshness
        ):
            raise StateMismatchError("Citation lacks fresh source-reported publication evidence")
        items.append(
            {
                "trust": "untrusted_literal_excerpt",
                "quote": citation["quote"],
                "source_sha256": citation["rawSha256"],
                "quote_sha256": citation["quoteSha256"],
                "citation_sha256": citation["sha256"],
                "research_receipt_sha256": citation["receiptSha256"],
            }
        )
    return {
        "kind": _PACKET_KIND,
        "selection_sha256": content_hash(selection),
        "items": items,
    }
