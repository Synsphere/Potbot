"""Read-only operation catalogue over the existing workspace authorities.

Requests cannot dispatch, probe, grant, install or settle work. The service
projects bounded records; the normal execution path rechecks every permission.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}


class OperationReader(Protocol):
    """No write or execution method is exposed to this adapter."""

    def list(
        self,
        context: WorkspaceContext,
        *,
        query: str = "",
        kind: str | None = None,
        state: str | None = None,
        after: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]: ...

    def inspect(self, context: WorkspaceContext, operation_id: str) -> dict[str, Any]: ...


def _query(request: Request, allowed: frozenset[str]) -> dict[str, str]:
    pairs = request.query_params.multi_items()
    if len(pairs) != len({key for key, _ in pairs}) or any(key not in allowed for key, _ in pairs):
        raise InvalidInputError("Unexpected or duplicate operation catalogue query")
    return dict(pairs)


def register_operation_routes(
    app: FastAPI,
    database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...],
    workspace_id: UUID,
    *,
    reader: OperationReader | None = None,
) -> None:
    if reader is None:
        from potbot.controller.operation_catalogue import OperationCatalogue

        service: OperationReader = OperationCatalogue(database)
    else:
        service = reader

    def authorized(request: Request) -> WorkspaceContext:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Unknown browser origin")
        actor = authenticate(request.headers.get("authorization"))
        if actor.workspace_id != workspace_id:
            raise ScopeDeniedError("Catalogue is outside the configured workspace")
        return actor

    @app.get("/v1/ui/operations")
    async def operation_list(request: Request) -> JSONResponse:
        actor = authorized(request)
        query = _query(request, frozenset({"q", "kind", "state", "after", "limit"}))
        raw_limit = query.get("limit", "25")
        if (
            not raw_limit.isascii()
            or not raw_limit.isdecimal()
            or len(raw_limit) > 3
            or not 1 <= int(raw_limit) <= 100
        ):
            raise InvalidInputError("Catalogue page size must be between 1 and 100")
        if len(query.get("q", "")) > 160 or len(query.get("after", "")) > 4096:
            raise InvalidInputError("Catalogue query exceeds its bound")
        result = await run_in_threadpool(
            service.list,
            actor,
            query=query.get("q", ""),
            kind=query.get("kind"),
            state=query.get("state"),
            after=query.get("after"),
            limit=int(raw_limit),
        )
        return JSONResponse(result, headers=_HEADERS)

    @app.get("/v1/ui/operations/{operation_id}")
    async def operation_detail(request: Request, operation_id: str) -> JSONResponse:
        actor = authorized(request)
        _query(request, frozenset())
        if re.fullmatch(r"[a-f0-9]{64}", operation_id) is None:
            raise InvalidInputError("Catalogue operation identity is invalid")
        result = await run_in_threadpool(service.inspect, actor, operation_id)
        return JSONResponse(result, headers=_HEADERS)
