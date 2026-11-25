"""Scoped settings/inventory reads and native-operator revocation intents."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.controller.settings import SettingsService
from potbot.controller.swarm_requests import SCHEMA
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.persistence.database import Database
from potbot.tools.local_inventory import InventoryProfile

_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}


def register_settings_routes(
    app: FastAPI,
    database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...],
    workspace_id: UUID,
    authorize_setup: Callable[[str | None], None] | None,
    inventory: InventoryProfile | None = None,
    *,
    service: SettingsService | None = None,
) -> None:
    owner = service or SettingsService(database, inventory)

    def actor(request: Request, *, mutate: bool = False) -> WorkspaceContext:
        if request.query_params:
            raise InvalidInputError("Settings endpoints do not accept caller URLs or query fields")
        if len(request.headers.getlist("authorization")) != 1:
            raise ScopeDeniedError("One authenticated workspace scope is required")
        context = authenticate(request.headers.get("authorization"))
        if context.workspace_id != workspace_id:
            raise ScopeDeniedError("Settings target is outside the authenticated workspace")
        origins = request.headers.getlist("origin")
        if len(origins) > 1 or origins and origins[0] not in allowed_origins:
            raise ScopeDeniedError("Settings origin is not approved")
        if mutate:
            if authorize_setup is None:
                raise CapabilityUnavailableError("Native operator setup is required")
            if len(origins) != 1 or len(request.headers.getlist("x-potbot-setup-token")) != 1:
                raise ScopeDeniedError("Explicit operator setup and mutation origin are required")
            authorize_setup(request.headers.get("x-potbot-setup-token"))
        return context

    @app.get("/v1/ui/settings/catalogue")
    async def settings_catalogue(request: Request) -> JSONResponse:
        return JSONResponse(
            await run_in_threadpool(owner.catalogue, actor(request)), headers=_HEADERS
        )

    @app.get("/v1/ui/settings/effective")
    async def effective_settings(request: Request) -> JSONResponse:
        return JSONResponse(
            await run_in_threadpool(owner.effective, actor(request)), headers=_HEADERS
        )

    @app.get("/v1/ui/inventory/models")
    async def installed_models(request: Request) -> JSONResponse:
        return JSONResponse(
            await run_in_threadpool(owner.inventory_view, actor(request), "models"),
            headers=_HEADERS,
        )

    @app.get("/v1/ui/inventory/tools")
    async def installed_tools(request: Request) -> JSONResponse:
        return JSONResponse(
            await run_in_threadpool(owner.inventory_view, actor(request), "tools"), headers=_HEADERS
        )

    @app.get("/v1/ui/swarm/nodes")
    async def swarm_nodes(request: Request) -> JSONResponse:
        return JSONResponse(
            await run_in_threadpool(
                owner.swarm, actor(request), request_available=authorize_setup is not None
            ),
            headers=_HEADERS,
        )

    @app.post("/v1/ui/swarm/revocations", status_code=202)
    async def revoke_request(request: Request) -> JSONResponse:
        context = actor(request, mutate=True)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise InvalidInputError("A JSON revocation review is required")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise InvalidInputError("Revocation review exceeds its byte allowance")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        try:
            value = json.loads(raw, object_pairs_hook=unique)
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "requestId",
                    "grantId",
                    "expectedRevision",
                    "expectedGeneration",
                    "grantSha256",
                    "confirm",
                }
                or value["confirm"] is not True
            ):
                raise ValueError
            identity = UUID(value["requestId"])
            if str(identity) != value["requestId"] or identity.int == 0:
                raise ValueError
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            raise InvalidInputError("An exact confirmed revocation review is required") from None
        result = await run_in_threadpool(
            owner.request,
            context,
            identity,
            {
                "schema": SCHEMA,
                **{
                    key: value[key]
                    for key in ("grantId", "expectedRevision", "expectedGeneration", "grantSha256")
                },
            },
        )
        return JSONResponse(result, status_code=202, headers=_HEADERS)

    from potbot.api.live_controls import register_live_control_routes

    register_live_control_routes(
        app, database, actor, _HEADERS, setup_available=authorize_setup is not None
    )
