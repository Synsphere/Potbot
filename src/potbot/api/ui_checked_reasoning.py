"""Private operator controls for the existing durable reasoning queue."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.api.ui_operation_workbench import _body, _query
from potbot.controller.routed_reasoning import ROLES, RoutedReasoning
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.operation_workbench import canonical_uuid

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}
_RUN = "/v1/ui/checked-reasoning/{run_id}"


def register_checked_reasoning_routes(
    app: FastAPI,
    database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...],
    workspace_id: UUID,
    authorize_setup: Callable[[str | None], None],
) -> None:
    service = RoutedReasoning(database)

    def actor(request: Request, *, mutation: bool = False) -> WorkspaceContext:
        if len(request.headers.getlist("authorization")) != 1 or any(
            len(request.headers.getlist(name)) > 1
            for name in ("origin", "x-potbot-expected-principal", "x-potbot-setup-token")
        ):
            raise ScopeDeniedError("Ambiguous checked reasoning authorization")
        origin = request.headers.get("origin")
        if (origin is not None and origin not in allowed_origins) or (mutation and origin is None):
            raise ScopeDeniedError("Checked reasoning requires an approved operator origin")
        scope = authenticate(request.headers.get("authorization"))
        expected = request.headers.get("x-potbot-expected-principal")
        if scope.workspace_id != workspace_id or (
            (expected is not None or mutation) and expected != str(scope.principal_id)
        ):
            raise ScopeDeniedError("Checked reasoning owner changed; reopen the operator session")
        _query(request)
        return scope

    async def confirmed(request: Request, *fields: str) -> dict[str, Any]:
        if len(request.headers.getlist("content-type")) != 1:
            raise InvalidInputError("Checked reasoning requires one JSON content type")
        value = await _body(request)
        if set(value) != {"confirm", *fields} or value["confirm"] is not True:
            raise InvalidInputError("Checked reasoning requires the exact confirmed command")
        return value

    @app.get(_RUN)
    async def inspect(request: Request, run_id: str) -> JSONResponse:
        value = await run_in_threadpool(service.inspect, actor(request), canonical_uuid(run_id))
        return JSONResponse(value, headers=_HEADERS)

    @app.get(_RUN + "/result")
    async def result(request: Request, run_id: str) -> JSONResponse:
        value = await run_in_threadpool(service.result, actor(request), canonical_uuid(run_id))
        return JSONResponse(value, headers=_HEADERS)

    @app.post(_RUN + "/start")
    async def start(request: Request, run_id: str) -> JSONResponse:
        scope = actor(request, mutation=True)
        command = await confirmed(request, "plan")
        value = await run_in_threadpool(
            service.start, scope, canonical_uuid(run_id), command["plan"]
        )
        return JSONResponse(value, headers=_HEADERS)

    @app.post(_RUN + "/advance")
    async def advance(request: Request, run_id: str) -> JSONResponse:
        scope = actor(request, mutation=True)
        token = request.headers.get("x-potbot-setup-token")
        if not token:
            raise ScopeDeniedError("Checked reasoning advancement requires operator setup")
        await run_in_threadpool(authorize_setup, token)
        command = await confirmed(request, "expected_stage")
        stage = command["expected_stage"]
        if type(stage) is not int or not 0 <= stage < len(ROLES):
            raise InvalidInputError("A checked reasoning stage index is required")
        value = await run_in_threadpool(
            service.advance, scope, canonical_uuid(run_id), setup_token=token, expected_stage=stage
        )
        return JSONResponse(value, headers=_HEADERS)

    @app.post(_RUN + "/interrupt")
    async def interrupt(request: Request, run_id: str) -> JSONResponse:
        scope = actor(request, mutation=True)
        await confirmed(request)
        value = await run_in_threadpool(service.interrupt, scope, canonical_uuid(run_id))
        return JSONResponse(value, headers=_HEADERS)
