"""Authenticated operation checks and runs through the existing durable queue.

The HTTP adapter never starts a worker or probes an installation. A separate
operator capability authorizes checks and launches; owned reads and cancellation
retain the workspace principal's ordinary access boundary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

MAX_BODY_BYTES = 65_536
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


class Workbench(Protocol):
    def describe(self, context: WorkspaceContext, operation_id: str) -> dict[str, Any]: ...

    def check(
        self,
        context: WorkspaceContext,
        operation_id: str,
        request: dict[str, Any],
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]: ...

    def launch(
        self,
        context: WorkspaceContext,
        operation_id: str,
        check_job_id: UUID,
        request: dict[str, Any],
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]: ...

    def inspect(
        self, context: WorkspaceContext, operation_id: str, job_id: UUID
    ) -> dict[str, Any]: ...

    def events(
        self,
        context: WorkspaceContext,
        operation_id: str,
        job_id: UUID,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]: ...

    def cancel(
        self, context: WorkspaceContext, operation_id: str, job_id: UUID, key: str
    ) -> dict[str, Any]: ...

    def result(
        self, context: WorkspaceContext, operation_id: str, job_id: UUID
    ) -> dict[str, Any]: ...

    def artifact(
        self,
        context: WorkspaceContext,
        operation_id: str,
        job_id: UUID,
        *,
        offset: int = 0,
        length: int = 262144,
    ) -> dict[str, Any]: ...


def _uuid(value: object) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("A canonical job identity is required") from None


def _query(request: Request, allowed: frozenset[str] = frozenset()) -> dict[str, str]:
    pairs = request.query_params.multi_items()
    if len(pairs) != len({key for key, _ in pairs}) or any(key not in allowed for key, _ in pairs):
        raise InvalidInputError("Unexpected or duplicate workbench query")
    return dict(pairs)


def _integer(value: str, *, maximum: int, minimum: int = 0) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]{0,15}", value):
        raise InvalidInputError("Invalid workbench page bound")
    number = int(value)
    if not minimum <= number <= maximum:
        raise InvalidInputError("Workbench page bound is outside its allowance")
    return number


def _idempotency(request: Request) -> str:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1 or _KEY.fullmatch(values[0]) is None:
        raise InvalidInputError("A bounded Idempotency-Key header is required")
    request.state.idempotency_key = values[0]
    return values[0]


async def _body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InvalidInputError("Operation workbench requests require JSON")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise InvalidInputError("Operation workbench request exceeds its byte allowance")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, value in items:
            if name in values:
                raise ValueError("duplicate key")
            values[name] = value
        return values

    def constant(_value: str) -> None:
        raise ValueError("nonfinite constant")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidInputError("Invalid operation workbench document") from None
    if not isinstance(value, dict):
        raise InvalidInputError("Operation workbench input must be an object")
    return value


def register_operation_workbench_routes(
    app: FastAPI,
    database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...],
    workspace_id: UUID,
    authorize_setup: Callable[[str | None], None] | None,
    *,
    service: Workbench | None = None,
) -> None:
    if service is None:
        from potbot.controller.operation_workbench import OperationWorkbench

        workbench: Workbench = OperationWorkbench(database)
    else:
        workbench = service

    def actor(request: Request, operation_id: str, *, mutation: bool = False) -> WorkspaceContext:
        if any(
            len(request.headers.getlist(name)) > 1
            for name in ("authorization", "origin", "x-potbot-setup-token")
        ):
            raise ScopeDeniedError("Ambiguous operation workbench authorization")
        origin = request.headers.get("origin")
        if (origin is not None and origin not in allowed_origins) or (mutation and origin is None):
            raise ScopeDeniedError("Operation workbench requires an allowed browser origin")
        context = authenticate(request.headers.get("authorization"))
        if context.workspace_id != workspace_id:
            raise ScopeDeniedError("Operation workbench is outside the configured workspace")
        if _SHA.fullmatch(operation_id) is None:
            raise InvalidInputError("Invalid catalogue operation identity")
        return context

    def setup(request: Request) -> str:
        if authorize_setup is None:
            raise CapabilityUnavailableError("Operator setup is unavailable on this listener")
        value = request.headers.get("x-potbot-setup-token")
        authorize_setup(value)
        if not isinstance(value, str) or not value:
            raise ScopeDeniedError("Operator setup permission required")
        return value

    def response(value: dict[str, Any], *, queued: bool = False) -> JSONResponse:
        return JSONResponse(value, status_code=202 if queued else 200, headers=_HEADERS)

    @app.get("/v1/ui/operations/{operation_id}/contract")
    async def describe(request: Request, operation_id: str) -> JSONResponse:
        context = actor(request, operation_id)
        _query(request)
        return response(await run_in_threadpool(workbench.describe, context, operation_id))

    @app.post("/v1/ui/operations/{operation_id}/checks")
    async def check(request: Request, operation_id: str) -> JSONResponse:
        context = actor(request, operation_id, mutation=True)
        _query(request)
        token, key = setup(request), _idempotency(request)
        value = await _body(request)
        result = await run_in_threadpool(
            workbench.check, context, operation_id, value, key, setup_token=token
        )
        return response(result, queued=True)

    @app.post("/v1/ui/operations/{operation_id}/runs")
    async def launch(request: Request, operation_id: str) -> JSONResponse:
        context = actor(request, operation_id, mutation=True)
        _query(request)
        token, key = setup(request), _idempotency(request)
        value = await _body(request)
        if (
            set(value) != {"checkJobId", "request", "confirm"}
            or value["confirm"] is not True
            or not isinstance(value["request"], dict)
        ):
            raise InvalidInputError(
                "A launch requires its checked request and explicit confirmation"
            )
        result = await run_in_threadpool(
            workbench.launch,
            context,
            operation_id,
            _uuid(value["checkJobId"]),
            value["request"],
            key,
            setup_token=token,
        )
        return response(result, queued=True)

    @app.get("/v1/ui/operations/{operation_id}/jobs/{job_id}")
    async def inspect(request: Request, operation_id: str, job_id: str) -> JSONResponse:
        context = actor(request, operation_id)
        _query(request)
        return response(
            await run_in_threadpool(workbench.inspect, context, operation_id, _uuid(job_id))
        )

    @app.get("/v1/ui/operations/{operation_id}/jobs/{job_id}/events")
    async def events(request: Request, operation_id: str, job_id: str) -> JSONResponse:
        context = actor(request, operation_id)
        query = _query(request, frozenset({"after", "limit"}))
        after = _integer(query.get("after", "0"), maximum=2**53 - 1)
        limit = _integer(query.get("limit", "100"), minimum=1, maximum=100)
        return response(
            await run_in_threadpool(
                workbench.events, context, operation_id, _uuid(job_id), after=after, limit=limit
            )
        )

    @app.get("/v1/ui/operations/{operation_id}/jobs/{job_id}/result")
    async def result(request: Request, operation_id: str, job_id: str) -> JSONResponse:
        context = actor(request, operation_id)
        _query(request)
        return response(
            await run_in_threadpool(workbench.result, context, operation_id, _uuid(job_id))
        )

    @app.post("/v1/ui/operations/{operation_id}/jobs/{job_id}/cancel")
    async def cancel(request: Request, operation_id: str, job_id: str) -> JSONResponse:
        context = actor(request, operation_id, mutation=True)
        _query(request)
        key = _idempotency(request)
        value = await _body(request)
        if value != {"confirm": True} or type(value.get("confirm")) is not bool:
            raise InvalidInputError("Cancellation requires explicit confirmation")
        return response(
            await run_in_threadpool(workbench.cancel, context, operation_id, _uuid(job_id), key)
        )

    @app.get("/v1/ui/operations/{operation_id}/jobs/{job_id}/artifact")
    async def artifact(request: Request, operation_id: str, job_id: str) -> JSONResponse:
        context = actor(request, operation_id)
        query = _query(request, frozenset({"offset", "length"}))
        offset = _integer(query.get("offset", "0"), maximum=2**53 - 1)
        length = _integer(query.get("length", "262144"), minimum=1, maximum=262144)
        return response(
            await run_in_threadpool(
                workbench.artifact,
                context,
                operation_id,
                _uuid(job_id),
                offset=offset,
                length=length,
            )
        )
