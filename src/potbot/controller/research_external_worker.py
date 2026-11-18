"""One bounded native external research attempt on ResearchRunStore's queue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from potbot.controller.research_external import ResearchRateWait, refresh_external_wait
from potbot.controller.research_store import ResearchStore
from potbot.controller.research_worker import ResearchLease, ResearchRunStore
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.research.contracts import ResearchAction
from potbot.research.external_contracts import ExternalRequest, ExternalResponse
from potbot.research.external_transport import (
    ResearchCredentials,
    TransportFactory,
    perform_external_request,
)
from potbot.research.providers import (
    fetch_request,
    merge_search_results,
    metadata_request,
    normalize_search_results,
    robots_permits,
    robots_request,
    search_request,
)
from potbot.research.source_policy import ExternalResearchPolicy


class ExternalResearchWorker:
    """Native-only orchestrator; constructor seams are never browser fields."""

    def __init__(
        self,
        store: ResearchRunStore,
        policy: ExternalResearchPolicy,
        *,
        credentials: ResearchCredentials | None = None,
        transport_factory: TransportFactory | None = None,
        usage_origin: str = "measured",
        policy_provider: Callable[[], ExternalResearchPolicy] | None = None,
    ) -> None:
        if not isinstance(policy, ExternalResearchPolicy) or usage_origin not in {
            "measured",
            "synthetic",
        }:
            raise InvalidInputError(
                "External research worker requires a trusted policy and explicit usage origin"
            )
        if transport_factory is not None and usage_origin != "synthetic":
            raise InvalidInputError(
                "Injected research transports must explicitly mark synthetic usage"
            )
        self.store, self.policy = store, policy
        self.credentials, self.transport_factory = credentials, transport_factory
        self.usage_origin = usage_origin
        if policy_provider is not None and not callable(policy_provider):
            raise InvalidInputError("Native research policy provider must be a trusted callable")
        self.policy_provider = policy_provider

    def _deployment_policy(self) -> ExternalResearchPolicy:
        value = self.policy_provider() if self.policy_provider is not None else self.policy
        if not isinstance(value, ExternalResearchPolicy):
            raise ScopeDeniedError("Native research deployment policy is unavailable")
        return value

    def _require_policy(self) -> None:
        if self._deployment_policy().fingerprint != self.policy.fingerprint:
            raise RevisionConflictError(
                "Native research deployment policy changed during execution"
            )

    def run_once(self, context: WorkspaceContext, worker_id: str) -> dict[str, Any] | None:
        return asyncio.run(self.arun_once(context, worker_id))

    async def _send(
        self, context: WorkspaceContext, lease: ResearchLease, request: ExternalRequest
    ) -> ExternalResponse:
        admitted = False

        async def before_send(prepared: ExternalRequest) -> None:
            nonlocal admitted
            if admitted or prepared.fingerprint != request.fingerprint:
                raise ScopeDeniedError(
                    "Research transport tried to change or repeat its prepared request"
                )
            while True:
                try:
                    self._require_policy()
                    self.store.before_send_external(context, lease, self.policy, prepared)
                    admitted = True
                    return
                except ResearchRateWait as wait:
                    # A short await keeps cancellation and current task/need
                    # authority observable; no DB lock survives the interval.
                    refresh_external_wait(self.store, context, lease, self.policy)
                    await asyncio.sleep(min(wait.milliseconds / 1000, 1.0))

        response = await perform_external_request(
            request,
            before_send=before_send,
            credentials=self.credentials,
            transport_factory=self.transport_factory,
        )
        if not admitted:
            raise ScopeDeniedError("Research transport returned without controller admission")
        self.store.record_external_response(
            context, lease, request, response, usage_origin=self.usage_origin
        )
        return response

    def _stop(self, context: WorkspaceContext, lease: ResearchLease, reason: str) -> dict[str, Any]:
        try:
            return self.store.fail_external(context, lease, reason)
        except RevisionConflictError:
            self.store.quarantine_external(context)
            return self.store.inspect_external(context, lease.run_id)

    async def arun_once(self, context: WorkspaceContext, worker_id: str) -> dict[str, Any] | None:
        self.store.quarantine_external(context)
        self.policy = self._deployment_policy()
        lease = self.store.claim_external(context, worker_id, self.policy)
        if lease is None:
            return None
        try:
            action = ResearchAction.from_dict(
                ResearchStore(self.store.database).inspect(context, lease.action_id)
            )
            batches = []
            calls = 0
            for rule in self.policy.providers:
                response = await self._send(
                    context,
                    lease,
                    search_request(self.policy, rule.provider, action.query.search_text),
                )
                calls += 1
                if response.status != "fetched":
                    if response.status in {"timed_out", "indeterminate"}:
                        return self._stop(context, lease, "usage_indeterminate")
                    if response.status not in self.policy.fallback_statuses:
                        return self._stop(context, lease, "request_failed")
                    continue
                assert response.data is not None
                if rule.provider == "pubmed":
                    from potbot.research.specialist import verify_pubmed_summary

                    child = metadata_request(self.policy, response.data)
                    if child is not None:
                        search_raw = response.data
                        response = await self._send(context, lease, child)
                        calls += 1
                        if response.status != "fetched":
                            if response.status in {"timed_out", "indeterminate"}:
                                return self._stop(context, lease, "usage_indeterminate")
                            if response.status not in self.policy.fallback_statuses:
                                return self._stop(context, lease, "request_failed")
                            continue
                        assert response.data is not None
                        verify_pubmed_summary(response.data, search_raw, self.policy.max_results)
                assert response.data is not None
                batches.append(
                    normalize_search_results(
                        rule.provider,
                        response.data,
                        retrieved_at=response.retrieved_at,
                        max_results=self.policy.max_results,
                    )
                )
            merged = merge_search_results(batches)
            robots: dict[str, ExternalResponse] = {}
            for result in merged[: self.policy.max_documents]:
                url = result["canonicalUrl"]
                try:
                    self.policy.require_fetch_url(url)
                    request = robots_request(self.policy, url)
                except (ScopeDeniedError, InvalidInputError):
                    continue
                if request.fingerprint not in robots:
                    if calls >= self.policy.max_requests:
                        break
                    response = await self._send(context, lease, request)
                    calls += 1
                    robots[request.fingerprint] = response
                    if response.status in {"timed_out", "indeterminate"}:
                        return self._stop(context, lease, "usage_indeterminate")
                response = robots[request.fingerprint]
                if response.status != "fetched":
                    continue
                assert response.data is not None
                if not robots_permits(response.data, url):
                    continue
                if calls >= self.policy.max_requests:
                    break
                response = await self._send(context, lease, fetch_request(self.policy, url))
                calls += 1
                if response.status in {"timed_out", "indeterminate"}:
                    return self._stop(context, lease, "usage_indeterminate")
            self._require_policy()
            return self.store.complete_external(
                context, lease, self.policy, revalidate=self._require_policy
            )
        except asyncio.CancelledError:
            self._stop(context, lease, "research_cancelled")
            raise
        except OperationCancelledError:
            return self._stop(context, lease, "research_cancelled")
        except CapabilityUnavailableError:
            return self._stop(context, lease, "provider_credential_unavailable")
        except (RevisionConflictError, ScopeDeniedError):
            return self._stop(context, lease, "authority_changed")
        except Exception:
            # A pending dispatch is detected by the owner and retains the
            # reservation as indeterminate. Unknown failures never trigger retry.
            return self._stop(context, lease, "research_failed")
