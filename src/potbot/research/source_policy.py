"""Exact external-source catalogue and transport preflight for Phase 7.

A catalogue is *configuration*, not network authority. A trusted controller
must separately verify reservation, disclosure and egress-policy identities
against durable records before passing a grant to this module. The returned
permit is consumed only by the existing bounded HTTP transport through a
qualified proxy/network namespace. No source code or downloaded text is run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.research.contracts import (
    ObservationStatus,
    ResearchAction,
    ResearchCategory,
    ResearchObservation,
    ResearchOperation,
)
from potbot.research.http_transport import FetchPermit, FetchResult, FetchStatus
from potbot.tools.connect_policy import checked_host

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InvalidInputError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate policy field")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class ResearchSource:
    """One exact operator-selected source, never a model-composed URL."""

    source_id: str
    category: ResearchCategory
    exact_url: str
    declared_revision: str
    max_response_bytes: int
    egress_policy_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not _SOURCE_ID.fullmatch(self.source_id):
            raise InvalidInputError("source ID must be a bounded literal identifier")
        if not isinstance(self.category, ResearchCategory) or self.category not in {
            ResearchCategory.DOCUMENTATION,
            ResearchCategory.EXAMPLE_CODE,
            ResearchCategory.MCP,
        }:
            raise InvalidInputError("source catalogue supports external source categories only")
        if not isinstance(self.exact_url, str) or not self.exact_url or len(self.exact_url) > 1024:
            raise InvalidInputError("external source URL must be bounded")
        if (
            not isinstance(self.declared_revision, str)
            or not self.declared_revision.strip()
            or len(self.declared_revision) > 200
            or any(ord(char) < 32 for char in self.declared_revision)
        ):
            raise InvalidInputError("source revision must be explicit and bounded")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 262144:
            raise InvalidInputError("source response allowance is invalid")
        _digest(self.egress_policy_digest, "egress policy")
        # Reuse the transport's authoritative HTTPS/hostname/redirect rules.
        # UUID(int=0) values are validation sentinels, never issued permits.
        FetchPermit(
            exact_url=self.exact_url,
            reservation_id=UUID(int=0),
            action_id=UUID(int=0),
            disclosure_decision_id=UUID(int=0),
            max_bytes=self.max_response_bytes,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "category": self.category.value,
            "exact_url": self.exact_url,
            "declared_revision": self.declared_revision,
            "max_response_bytes": self.max_response_bytes,
            "egress_policy_digest": self.egress_policy_digest,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash({"schema_version": 1, **self.to_dict()})


@dataclass(frozen=True, slots=True)
class ResearchFetchGrant:
    """Identities from an independently authenticated controller admission.

    Constructing this value is NOT sufficient authorization; callers must read
    and verify the corresponding reservation, disclosure decision and deployed
    egress policy from the controller's own records before invoking transport.
    """

    action_id: UUID
    action_identity: str
    catalog_digest: str
    source_id: str
    reservation_id: UUID
    disclosure_decision_id: UUID
    egress_policy_digest: str

    def __post_init__(self) -> None:
        for name in ("action_id", "reservation_id", "disclosure_decision_id"):
            if not isinstance(getattr(self, name), UUID):
                raise InvalidInputError(f"{name} requires a UUID")
        for name in ("action_identity", "catalog_digest", "egress_policy_digest"):
            _digest(getattr(self, name), name)
        if not isinstance(self.source_id, str) or not _SOURCE_ID.fullmatch(self.source_id):
            raise InvalidInputError("grant source ID must be a literal source identifier")


class ResearchSourceCatalog:
    """Immutable, content-addressed set of operator-selected exact URLs."""

    def __init__(self, sources: Iterable[ResearchSource]):
        selected = tuple(sources)
        if (
            not selected
            or len(selected) > 128
            or any(not isinstance(source, ResearchSource) for source in selected)
        ):
            raise InvalidInputError("source catalogue requires 1 to 128 typed sources")
        by_id = {source.source_id: source for source in selected}
        if len(by_id) != len(selected):
            raise InvalidInputError("source catalogue contains duplicate source identities")
        self._sources = by_id
        self.fingerprint = content_hash(
            {
                "schema_version": 1,
                "sources": [by_id[name].to_dict() for name in sorted(by_id)],
            }
        )

    def preflight(self, action: ResearchAction, grant: ResearchFetchGrant) -> FetchPermit:
        """Bind source/versions/limits; never verify grants or perform egress here."""
        if not isinstance(action, ResearchAction) or not isinstance(grant, ResearchFetchGrant):
            raise InvalidInputError("external-source preflight requires typed inputs")
        if grant.action_id != action.action_id or grant.action_identity != action.identity:
            raise ScopeDeniedError("source grant does not bind the exact admitted action")
        if grant.catalog_digest != self.fingerprint:
            raise RevisionConflictError("source grant was issued for another catalogue")
        source = self._sources.get(grant.source_id)
        if source is None:
            raise ScopeDeniedError("external source is not present in the pinned catalogue")
        query = action.query
        if (
            query.category != source.category
            or query.operation != ResearchOperation.QUERY_SOURCE
            or query.locator != source.source_id
        ):
            raise ScopeDeniedError("research action cannot select another source or category")
        if query.target_version != source.declared_revision:
            raise RevisionConflictError("source revision differs from the admitted research query")
        if grant.egress_policy_digest != source.egress_policy_digest:
            raise RevisionConflictError("source grant uses another deployed egress policy")
        if query.max_source_bytes > source.max_response_bytes:
            raise InvalidInputError("research query exceeds this source's byte allowance")
        return FetchPermit(
            exact_url=source.exact_url,
            action_id=action.action_id,
            reservation_id=grant.reservation_id,
            disclosure_decision_id=grant.disclosure_decision_id,
            max_bytes=query.max_source_bytes,
        )

    def observe(
        self,
        action: ResearchAction,
        grant: ResearchFetchGrant,
        result: FetchResult,
        *,
        retrieved_at: datetime,
    ) -> ResearchObservation:
        """Project transport bytes without claiming source reliability or licence."""
        permit = self.preflight(action, grant)
        if not isinstance(result, FetchResult):
            raise InvalidInputError("observation requires a typed transport outcome")
        if result.action_id != permit.action_id or result.request_url != permit.exact_url:
            raise ScopeDeniedError("transport result does not match the pinned research action")
        source = self._sources[grant.source_id]
        digest = result.sha256
        excerpt: str | None = None
        status = ObservationStatus.UNAVAILABLE
        complete = False
        if result.status == FetchStatus.FETCHED:
            if result.data is None or len(result.data) > permit.max_bytes:
                raise InvalidInputError("transport exceeded the research source's byte allowance")
            complete = True
            try:
                decoded = result.data.decode("utf-8")
            except UnicodeDecodeError:
                status = ObservationStatus.NON_TEXT
            else:
                if not decoded:
                    status = ObservationStatus.NO_MATCH
                elif "\x00" in decoded or any(
                    ord(char) < 32 and char not in "\t\n" for char in decoded
                ):
                    status = ObservationStatus.NON_TEXT
                else:
                    status = ObservationStatus.INSPECTED
                    excerpt = decoded
        elif result.status == FetchStatus.RESPONSE_TOO_LARGE:
            status = ObservationStatus.OVER_LIMIT
        return ResearchObservation(
            action_id=action.action_id,
            status=status,
            requested_locator=source.source_id,
            resolved_locator=source.exact_url,
            source_revision=source.fingerprint,
            retrieved_at=retrieved_at,
            content_digest=digest,
            excerpt=excerpt,
            line_start=None,
            line_end=None,
            scan_complete=complete,
        )


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidInputError(f"Research {name} is outside its supported bound")
    return value


@dataclass(frozen=True, slots=True)
class ResearchProviderRule:
    """One explicit supported search provider, with no credential or URL override."""

    provider: str
    cost_microunits_per_call: int
    minimum_interval_ms: int = 1000

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or self.provider not in {"brave", "tavily"}:
            raise InvalidInputError("Research provider is unsupported")
        _bounded_int(self.cost_microunits_per_call, "per-call cost ceiling", 0, 10**12)
        _bounded_int(self.minimum_interval_ms, "provider interval", 0, 3600000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "cost_microunits_per_call": self.cost_microunits_per_call,
            "minimum_interval_ms": self.minimum_interval_ms,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResearchProviderRule:
        if not isinstance(value, dict) or set(value) != {
            "provider",
            "cost_microunits_per_call",
            "minimum_interval_ms",
        }:
            raise InvalidInputError("Research provider rule fields are invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ResearchFetchHostRule:
    """Operator-reviewed terms for one exact public host; robots still apply."""

    host: str
    terms_approved: bool
    minimum_interval_ms: int = 1000

    def __post_init__(self) -> None:
        checked_host(self.host)
        if type(self.terms_approved) is not bool:
            raise InvalidInputError("Research source terms approval must be explicit")
        _bounded_int(self.minimum_interval_ms, "fetch interval", 0, 3600000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "terms_approved": self.terms_approved,
            "minimum_interval_ms": self.minimum_interval_ms,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResearchFetchHostRule:
        if not isinstance(value, dict) or set(value) != {
            "host",
            "terms_approved",
            "minimum_interval_ms",
        }:
            raise InvalidInputError("Research fetch-host rule fields are invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ExternalResearchPolicy:
    """External mode of the existing research source configuration owner.

    Configuration alone does not authorize a query. The controller binds this
    exact fingerprint and query to an explicit disclosure decision, reservation
    and current research lease before each request. The configured provider
    order is the comparison order; continuing after a failure additionally
    requires that status in ``fallback_statuses``. No ambiguous request retries.
    """

    allow_query_disclosure: bool = False
    providers: tuple[ResearchProviderRule, ...] = ()
    fetch_hosts: tuple[ResearchFetchHostRule, ...] = ()
    fallback_statuses: tuple[str, ...] = ()
    max_results: int = 8
    max_documents: int = 8
    max_requests: int = 24
    max_source_bytes: int = 262144
    timeout_seconds: int = 15

    def __post_init__(self) -> None:
        if type(self.allow_query_disclosure) is not bool:
            raise InvalidInputError("Research query disclosure permission must be explicit")
        if (
            type(self.providers) is not tuple
            or len(self.providers) > 2
            or any(type(item) is not ResearchProviderRule for item in self.providers)
            or len({item.provider for item in self.providers}) != len(self.providers)
            or type(self.fetch_hosts) is not tuple
            or len(self.fetch_hosts) > 32
            or any(type(item) is not ResearchFetchHostRule for item in self.fetch_hosts)
            or len({item.host for item in self.fetch_hosts}) != len(self.fetch_hosts)
        ):
            raise InvalidInputError("Research source policy requires distinct bounded typed rules")
        allowed = {"rate_limited", "quota_exhausted", "unavailable"}
        if (
            type(self.fallback_statuses) is not tuple
            or len(self.fallback_statuses) > len(allowed)
            or any(
                not isinstance(status, str) or status not in allowed
                for status in self.fallback_statuses
            )
            or len(set(self.fallback_statuses)) != len(self.fallback_statuses)
        ):
            raise InvalidInputError("Research fallback requires explicit non-ambiguous statuses")
        _bounded_int(self.max_results, "result limit", 1, 20)
        _bounded_int(self.max_documents, "document limit", 0, 8)
        _bounded_int(self.max_requests, "request limit", 1, 32)
        _bounded_int(self.max_source_bytes, "source byte limit", 1, 262144)
        _bounded_int(self.timeout_seconds, "deadline", 1, 30)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "potbot-external-research-policy/v1",
            "allow_query_disclosure": self.allow_query_disclosure,
            "providers": [rule.to_dict() for rule in self.providers],
            "fetch_hosts": [rule.to_dict() for rule in self.fetch_hosts],
            "fallback_statuses": list(self.fallback_statuses),
            "max_results": self.max_results,
            "max_documents": self.max_documents,
            "max_requests": self.max_requests,
            "max_source_bytes": self.max_source_bytes,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> ExternalResearchPolicy:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "allow_query_disclosure",
                "providers",
                "fetch_hosts",
                "fallback_statuses",
                "max_results",
                "max_documents",
                "max_requests",
                "max_source_bytes",
                "timeout_seconds",
            }
            or value["schema"] != "potbot-external-research-policy/v1"
            or not isinstance(value["providers"], list)
            or len(value["providers"]) > 2
            or not isinstance(value["fetch_hosts"], list)
            or len(value["fetch_hosts"]) > 32
            or not isinstance(value["fallback_statuses"], list)
        ):
            raise InvalidInputError("Research source policy fields are invalid")
        return cls(
            allow_query_disclosure=value["allow_query_disclosure"],
            providers=tuple(ResearchProviderRule.from_dict(row) for row in value["providers"]),
            fetch_hosts=tuple(ResearchFetchHostRule.from_dict(row) for row in value["fetch_hosts"]),
            fallback_statuses=tuple(value["fallback_statuses"]),
            max_results=value["max_results"],
            max_documents=value["max_documents"],
            max_requests=value["max_requests"],
            max_source_bytes=value["max_source_bytes"],
            timeout_seconds=value["timeout_seconds"],
        )

    @classmethod
    def read(cls, path: Path) -> ExternalResearchPolicy:
        try:
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError
            return cls.from_dict(json.loads(raw, object_pairs_hook=_unique_object))
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise InvalidInputError("The research source policy is unreadable or invalid") from None

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    @property
    def digest(self) -> str:
        return self.fingerprint

    def require_provider(self, provider: str) -> ResearchProviderRule:
        rule = next((row for row in self.providers if row.provider == provider), None)
        if not self.allow_query_disclosure or rule is None:
            raise ScopeDeniedError("Research query disclosure to this provider is not enabled")
        return rule

    def require_fetch_url(self, url: str) -> ResearchFetchHostRule:
        # Local repository/static-source declarations do not require the
        # optional provider transport dependencies merely to import this owner.
        from potbot.tools.pinned_https import canonical_https_host

        host = canonical_https_host(url, allow_query=True)
        rule = next((row for row in self.fetch_hosts if row.host == host), None)
        if rule is None or not rule.terms_approved:
            raise ScopeDeniedError("Research fetch host or source terms are not approved")
        return rule
