"""Worker-owned operation eligibility and immediate linked-launch revalidation.

An eligibility observation does not issue a grant, reserve a budget, call a
model provider or dispatch a work process. Read-only health/installation probes
use the existing operator authorities. Execution still performs its normal
admission. Only operator-signed check and launch requests enter this path.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from potbot.controller.budgets import BudgetLedger
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    BudgetUnavailableError,
    CapabilityUnavailableError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.jobs import CHECK_JOB, COG_JOB, JobLease, JobSpec
from potbot.domain.operation_workbench import (
    CHECK_SCHEMA,
    parse_check_report,
    report_time,
    request_digest,
    verify_check_authorization,
    verify_launch_authorization,
)
from potbot.domain.serialization import content_hash
from potbot.tools.secrets import CaptureRedactor

if TYPE_CHECKING:
    from potbot.controller.job_runner import HostedJobRunner
    from potbot.controller.jobs import JobService
    from potbot.controller.native_cog_dispatch import NativeCogDispatcher
    from potbot.models.hosted_gateway import HostedInferenceGateway


class _StoppedCheck(Exception):
    """The report already contains a safe reason; do not expose source errors."""


class OperationChecks:
    def __init__(
        self,
        jobs: JobService,
        gateway: HostedInferenceGateway,
        native_cogs: NativeCogDispatcher | None,
        *,
        setup_token: Callable[[], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.jobs, self.gateway, self.native_cogs = jobs, gateway, native_cogs
        self.setup_token = setup_token
        self.clock = clock or (lambda: datetime.now(UTC))

    def _token(self) -> str | None:
        try:
            return self.setup_token() if self.setup_token is not None else None
        except Exception:
            return None  # A locked or unavailable native store never grants a check.

    @staticmethod
    def _owner(actor: WorkspaceContext, lease: JobLease) -> None:
        if (
            lease.workspace_id != actor.workspace_id
            or lease.owner_principal_id != actor.principal_id
        ):
            raise ScopeDeniedError("Operation work belongs to another principal.")

    def evaluate(self, actor: WorkspaceContext, lease: JobLease) -> dict[str, Any]:
        """Finish one explicit operation.check; source errors become safe codes."""
        self._owner(actor, lease)
        if lease.job_type != CHECK_JOB:
            raise ScopeDeniedError("This worker entry point accepts operation checks only.")
        now = self.clock()
        identity, request = verify_check_authorization(actor, lease.input, self._token(), now=now)
        if request.episode_id != lease.episode_id:
            raise ScopeDeniedError("The operation check differs from its queued task.")
        expiry = datetime.fromtimestamp(lease.input["authorization"]["expires_at"], UTC)
        return self._evaluate(actor, lease, identity, request, now, expiry)

    def revalidate(self, actor: WorkspaceContext, lease: JobLease) -> None:
        """Recheck a linked launch; the retained receipt is never an authority."""
        if lease.operation_check_id is None:
            return
        self._owner(actor, lease)
        prior = self.jobs.operation_check(actor, lease)
        if prior is None:
            raise ScopeDeniedError("The launch has no owned successful operation check.")
        now = self.clock()
        token = self._token()
        identity, request = verify_check_authorization(actor, prior["input"], token, now=now)
        verify_launch_authorization(actor, lease, prior["input"], token, now=now)
        recorded = parse_check_report(prior["result"]["operation_check"])
        if (
            not recorded["eligible"]
            or recorded["operationId"] != identity
            or report_time(recorded["checkedAt"]) > now
            or report_time(recorded["expiresAt"]) <= now
            or recorded["bindings"]["contractRevisionId"] != str(lease.contract_revision_id)
            or recorded["bindings"]["requestSha256"] != request_digest(request.to_dict())
        ):
            raise StateMismatchError("The operation check is stale; check the exact request again.")
        expiry = datetime.fromtimestamp(prior["input"]["authorization"]["expires_at"], UTC)
        current = self._evaluate(actor, lease, identity, request, now, expiry)
        if not current["eligible"] or current["bindings"] != recorded["bindings"]:
            raise StateMismatchError(
                "Current operation eligibility differs from the checked request."
            )

    def _evaluate(
        self,
        actor: WorkspaceContext,
        lease: JobLease,
        identity: str,
        request: JobSpec,
        now: datetime,
        authorization_expiry: datetime,
    ) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        bindings: dict[str, Any] = {
            "requestSha256": request_digest(request.to_dict()),
            "contractRevisionId": str(lease.contract_revision_id),
        }

        def check(code: str, action: Callable[[], Any]) -> Any:
            try:
                result = action()
            except (OperationCancelledError, RevisionConflictError):
                raise
            except CapabilityUnavailableError:
                checks.append(
                    {
                        "code": code,
                        "status": "unsupported"
                        if code in {"native_handler", "native_protocol"}
                        else "blocked",
                    }
                )
                raise _StoppedCheck from None
            except Exception:
                checks.append({"code": code, "status": "blocked"})
                raise _StoppedCheck from None
            checks.append({"code": code, "status": "passed"})
            return result

        def task() -> None:
            if self.jobs.cancelled(actor, lease):
                raise OperationCancelledError("Operation check lost its live task or job lease.")
            with self.jobs.database.transaction(actor) as connection:
                row = connection.execute(
                    "SELECT status,active_contract_id FROM episodes WHERE workspace_id=%s AND id=%s",
                    (actor.workspace_id, request.episode_id),
                ).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["active_contract_id"] != lease.contract_revision_id
            ):
                raise StateMismatchError("A current running task contract is required.")

        try:
            check("task_contract", task)
            if request.job_type == COG_JOB:

                def installed() -> NativeCogDispatcher:
                    if self.native_cogs is None:
                        raise CapabilityUnavailableError("The native Cog handler is not installed.")
                    return self.native_cogs

                native = check("native_handler", installed)
                target = replace(
                    lease,
                    job_type=request.job_type,
                    input=request.input,
                    selected_models=request.selected_models,
                )
                native_bindings, demand = native.eligibility(self.jobs, actor, target, check=check)
                bindings.update(native_bindings)
            elif request.job_type == "model.generate":
                bindings["policySha256"] = self.gateway.policy.fingerprint
                demand = self._hosted(request, check)
            else:
                raise CapabilityUnavailableError(
                    "No execution handler is installed for this operation."
                )

            def budget() -> None:
                available = BudgetLedger(self.jobs.database).inspect(actor, request.episode_id)[
                    "available"
                ]
                if any(
                    unit not in available or value > available[unit]
                    for unit, value in demand.items()
                ):
                    raise BudgetUnavailableError(
                        "The active task budget cannot cover this request."
                    )

            check("task_budget", budget)
            check("task_still_current", task)
        except _StoppedCheck:
            pass
        expires = min(now + timedelta(seconds=120), authorization_expiry)
        return parse_check_report(
            {
                "schema": CHECK_SCHEMA,
                "operationId": identity,
                "eligible": all(item["status"] == "passed" for item in checks),
                "checks": checks,
                "bindings": bindings,
                "checkedAt": now.isoformat(),
                "expiresAt": expires.isoformat(),
                "bindingSha256": content_hash(bindings),
            }
        )

    def _hosted(
        self, request: JobSpec, check: Callable[[str, Callable[[], Any]], Any]
    ) -> dict[str, int]:
        def policy() -> list[tuple[Any, int, Any]]:
            return [
                self.gateway.policy.request(selection, request.input)
                for selection in request.selected_models
            ]

        admitted = check("hosted_policy", policy)

        def credentials() -> None:
            for generation, _allowance, _rate in admitted:
                key = self.gateway.secret_resolver(generation.selection.credential_ref)
                redactor = CaptureRedactor(
                    tuple(dict.fromkeys((key, *self.gateway.protected_values)))
                )
                prompt = (generation.prompt + generation.system).encode()
                if any(pattern in prompt for pattern in redactor.patterns):
                    raise ScopeDeniedError("A protected value cannot be disclosed in a prompt.")

        check("provider_credentials", credentials)
        demand = {
            "calls": 0,
            "tokens": 0,
            "inference_ms": 0,
            "money_microunits": 0,
            "concurrency": 1,
        }
        for generation, allowance, rate in admitted:
            demand["calls"] += 1
            demand["tokens"] += allowance + generation.max_output_tokens
            demand["inference_ms"] += math.ceil(generation.timeout_seconds * 1000)
            demand["money_microunits"] += rate.charge(allowance, generation.max_output_tokens)
        return demand


def runner_checks(runner: HostedJobRunner) -> OperationChecks:
    """Construct from the actual configured worker, not HTTP-supplied adapters."""
    return OperationChecks(
        runner.jobs,
        runner.gateway,
        runner.native_cogs,
        setup_token=runner.operation_setup_token,
    )
