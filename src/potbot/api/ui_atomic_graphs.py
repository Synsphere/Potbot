"""Private, bounded graph status and operator-controlled scheduler actions.

No HTTP handler runs a Cog, holds a worker lease, or creates a new plan.
An explicitly confirmed tick only submits previously checked jobs through the
existing, database-fenced operation workbench. A separate local scheduler worker
is required for continuously automatic progression.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

_UUID = re.compile(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}


def _uuid(value: str) -> UUID:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise InvalidInputError("Invalid atomic graph identifier")
    return UUID(value)


def register_atomic_graph_routes(
    app: FastAPI,
    database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...],
    workspace_id: UUID,
    authorize_setup: Callable[[str | None], None] | None,
    *,
    service: Any = None,
    pump: Any = None,
) -> None:
    if service is None:
        from potbot.controller.atomic_graph_runtime import AtomicGraphService

        service = AtomicGraphService(database)
    if pump is None:
        from potbot.controller.atomic_graph_pump import AtomicGraphPump

        pump = AtomicGraphPump(service)

    def actor(request: Request, *, mutation: bool = False) -> WorkspaceContext:
        if len(request.headers.getlist("authorization")) != 1 or any(
            len(request.headers.getlist(name)) > 1
            for name in (
                "origin",
                "x-potbot-setup-token",
                "x-potbot-expected-principal",
                "idempotency-key",
            )
        ):
            raise ScopeDeniedError("Ambiguous graph authorization")
        origin = request.headers.getlist("origin")
        if origin and origin[0] not in allowed_origins or mutation and len(origin) != 1:
            raise ScopeDeniedError("Graph controls require an approved browser origin")
        context = authenticate(request.headers.get("authorization"))
        if context.workspace_id != workspace_id:
            raise ScopeDeniedError("Graph is outside the configured workspace")
        expected = request.headers.get("x-potbot-expected-principal")
        if (mutation and expected is None) or (expected and expected != str(context.principal_id)):
            raise ScopeDeniedError("Graph owner changed; reopen this workspace")
        return context

    def query(request: Request, allowed: set[str]) -> dict[str, str]:
        items = request.query_params.multi_items()
        result = dict(items)
        if (
            len(request.scope.get("query_string", b"")) > 4096
            or len(items) != len(result)
            or set(result) - allowed
        ):
            raise InvalidInputError("Graph query is invalid or ambiguous")
        return result

    async def body(request: Request, *, keys: set[str]) -> dict[str, Any]:
        if (
            len(request.headers.getlist("content-type")) != 1
            or request.headers["content-type"].split(";", 1)[0] != "application/json"
        ):
            raise InvalidInputError("Graph mutation requires JSON")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise InvalidInputError("Graph mutation exceeds its byte limit")

        def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
            if len({key for key, _ in items}) != len(items):
                raise ValueError
            return dict(items)

        def reject_constant(_value: str) -> None:
            raise ValueError

        try:
            value = json.loads(
                raw.decode("utf-8", "strict"),
                object_pairs_hook=unique,
                parse_constant=reject_constant,
            )
            canonical_json(value).encode("utf-8", "strict")
        except (ValueError, UnicodeError, RecursionError):
            raise InvalidInputError("Graph mutation needs unambiguous finite JSON") from None
        if not isinstance(value, dict) or set(value) != keys or value.get("confirm") is not True:
            raise InvalidInputError("Graph mutation requires an explicit confirmed contract")
        return value

    def command(request: Request, *, require_setup: bool) -> str | None:
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or _KEY.fullmatch(keys[0]) is None:
            raise InvalidInputError("Graph mutations require one stable idempotency key")
        request.state.idempotency_key = keys[0]
        if not require_setup:
            return None
        if authorize_setup is None:
            raise CapabilityUnavailableError("Graph scheduler needs operator setup")
        token = request.headers.getlist("x-potbot-setup-token")
        if len(token) != 1 or not token[0]:
            raise ScopeDeniedError("Graph scheduler needs one operator capability")
        authorize_setup(token[0])
        return token[0]

    def reply(value: dict[str, Any], *, accepted: bool = False) -> JSONResponse:
        if len(canonical_json(value).encode("utf-8")) > 1_000_000:
            raise InvalidInputError("Graph response exceeds the allowed size")
        return JSONResponse(value, status_code=202 if accepted else 200, headers=_HEADERS)

    @app.get("/v1/ui/atomic-graphs")
    async def listing(request: Request) -> JSONResponse:
        context = actor(request)
        values = query(request, {"after", "limit", "activeOnly"})
        raw_limit = values.get("limit", "25")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit):
            raise InvalidInputError("Invalid graph page limit")
        state = values.get("activeOnly", "false")
        if state not in {"true", "false"}:
            raise InvalidInputError("Invalid graph state filter")
        return reply(
            await run_in_threadpool(
                pump.list,
                context,
                after=values.get("after"),
                limit=int(raw_limit),
                active_only=state == "true",
            )
        )

    @app.get("/v1/ui/atomic-graphs/{graph_id}")
    async def inspect(request: Request, graph_id: str) -> JSONResponse:
        context = actor(request)
        query(request, set())
        return reply(await run_in_threadpool(service.inspect, context, _uuid(graph_id)))

    @app.post("/v1/ui/atomic-graphs/{graph_id}/tick")
    async def tick(request: Request, graph_id: str) -> JSONResponse:
        context = actor(request, mutation=True)
        query(request, set())
        token = command(request, require_setup=True)
        value = await body(request, keys={"confirm", "limit"})
        if type(value["limit"]) is not int or not 1 <= value["limit"] <= 8:
            raise InvalidInputError("Graph tick exceeds the per-call launch bound")
        return reply(
            await run_in_threadpool(
                pump.tick_graph,
                context,
                _uuid(graph_id),
                setup_token=token,
                limit=value["limit"],
            ),
            accepted=True,
        )

    @app.post("/v1/ui/atomic-graphs/{graph_id}/interrupt")
    async def interrupt(request: Request, graph_id: str) -> JSONResponse:
        context = actor(request, mutation=True)
        query(request, set())
        command(request, require_setup=False)
        await body(request, keys={"confirm"})
        return reply(await run_in_threadpool(service.interrupt, context, _uuid(graph_id)))
