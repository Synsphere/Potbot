"""Authenticated retained MCP metadata and explicit operator decisions."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.api.ui_discovery import _body, _sha, _uuid
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def register_mcp_discovery_routes(
    app: FastAPI,
    database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...],
    workspace_id: UUID,
    authorize_setup: Callable[[str | None], None] | None,
    *,
    service: Any = None,
) -> None:
    if service is None:
        from potbot.controller.mcp_discovery import McpDiscoveryService

        service = McpDiscoveryService(database)

    def actor(request: Request, *, mutate: bool = False) -> WorkspaceContext:
        if len(request.headers.getlist("authorization")) != 1:
            raise ScopeDeniedError("MCP discovery requires one authenticated scope")
        context = authenticate(request.headers.get("authorization"))
        if context.workspace_id != workspace_id:
            raise ScopeDeniedError("MCP discovery belongs to another workspace")
        origins = request.headers.getlist("origin")
        if len(origins) > 1 or (origins and origins[0] not in allowed_origins):
            raise ScopeDeniedError("MCP discovery origin is not approved")
        if mutate:
            if authorize_setup is None:
                raise CapabilityUnavailableError("MCP discovery controls require operator setup")
            if len(origins) != 1:
                raise ScopeDeniedError("MCP controls require an exact origin")
            if len(request.headers.getlist("x-potbot-setup-token")) != 1:
                raise InvalidInputError("MCP controls require a single setup capability")
            authorize_setup(request.headers.get("x-potbot-setup-token"))
        return context

    def query(request: Request, allowed: set[str]) -> dict[str, str]:
        values = dict(request.query_params)
        if (
            len(request.scope.get("query_string", b"")) > 4096
            or len(values) != len(request.query_params.multi_items())
            or set(values) - allowed
        ):
            raise InvalidInputError("MCP discovery query fields are invalid or ambiguous")
        return values

    def page(values: dict[str, str]) -> dict[str, Any]:
        raw = values.get("limit", "25")
        if (
            not raw.isascii()
            or not raw.isdigit()
            or not 1 <= int(raw) <= 50
            or str(int(raw)) != raw
        ):
            raise InvalidInputError("MCP discovery page limit must be one to fifty")
        after = values.get("after")
        if after is not None:
            _uuid(after)
        return {"limit": int(raw), "after": after}

    async def command(request: Request) -> tuple[WorkspaceContext, int, str, str, str]:
        context = actor(request, mutate=True)
        query(request, set())
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or not _KEY.fullmatch(keys[0]):
            raise InvalidInputError("MCP controls require one stable command key")
        request.state.idempotency_key = keys[0]
        value = await _body(request)
        if (
            set(value) != {"revision", "digest", "confirm"}
            or type(value["revision"]) is not int
            or not 1 <= value["revision"] < 2**53 - 1
        ):
            raise InvalidInputError("MCP controls require the exact reviewed revision and digest")
        return (
            context,
            value["revision"],
            _sha(value["digest"]),
            keys[0],
            str(request.headers["x-potbot-setup-token"]),
        )

    def reply(value: dict[str, Any]) -> JSONResponse:
        return JSONResponse(value, headers=_HEADERS)

    @app.get("/v1/ui/mcp-discovery/bindings")
    async def bindings(request: Request) -> JSONResponse:
        context = actor(request)
        values = query(request, {"limit", "after"})
        return reply(await run_in_threadpool(service.bindings, context, **page(values)))

    @app.get("/v1/ui/mcp-discovery/bindings/{identity}")
    async def binding(identity: str, request: Request) -> JSONResponse:
        context = actor(request)
        query(request, set())
        return reply(await run_in_threadpool(service.binding, context, _uuid(identity)))

    @app.get("/v1/ui/mcp-discovery/bindings/{identity}/entries")
    async def entries(identity: str, request: Request) -> JSONResponse:
        context = actor(request)
        values = query(request, {"kind", "q", "limit", "after"})
        kind, search = values.get("kind"), values.get("q", "")
        if kind is not None and kind not in {"tool", "resource", "resource_template", "prompt"}:
            raise InvalidInputError("Unknown MCP entry kind")
        if len(search) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in search):
            raise InvalidInputError("MCP entry search exceeds its bounded literal contract")
        return reply(
            await run_in_threadpool(
                service.entries, context, _uuid(identity), kind=kind, query=search, **page(values)
            )
        )

    @app.get("/v1/ui/mcp-discovery/bindings/{identity}/entries/{entry_id}/reference")
    async def reference(identity: str, entry_id: str, request: Request) -> JSONResponse:
        context = actor(request)
        query(request, set())
        return reply(
            await run_in_threadpool(service.reference, context, _uuid(identity), _uuid(entry_id))
        )

    @app.get("/v1/ui/mcp-discovery/attempts")
    async def attempts(request: Request) -> JSONResponse:
        context = actor(request)
        values = query(request, {"bindingId", "limit", "after"})
        binding_id = _uuid(values["bindingId"]) if "bindingId" in values else None
        return reply(
            await run_in_threadpool(
                service.attempts, context, binding_id=binding_id, **page(values)
            )
        )

    @app.get("/v1/ui/mcp-discovery/attempts/{identity}")
    async def attempt(identity: str, request: Request) -> JSONResponse:
        context = actor(request)
        query(request, set())
        return reply(await run_in_threadpool(service.attempt, context, _uuid(identity)))

    @app.post("/v1/ui/mcp-discovery/bindings/{identity}/{action}")
    async def control(identity: str, action: str, request: Request) -> JSONResponse:
        context, revision, digest, key, token = await command(request)
        if action == "reconcile":
            value = await run_in_threadpool(
                service.request, context, _uuid(identity), revision, digest, key, setup_token=token
            )
        elif action in {"enable", "disable", "revoke"}:
            value = await run_in_threadpool(
                service.control,
                context,
                _uuid(identity),
                action,
                revision,
                digest,
                key,
                setup_token=token,
            )
        else:
            raise InvalidInputError("Unsupported MCP discovery control")
        return reply(value)

    @app.post("/v1/ui/mcp-discovery/attempts/{identity}/cancel")
    async def cancel(identity: str, request: Request) -> JSONResponse:
        context, revision, digest, key, token = await command(request)
        return reply(
            await run_in_threadpool(
                service.cancel, context, _uuid(identity), revision, digest, key, setup_token=token
            )
        )
