"""Native live-control routes using the existing workspace/setup boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.controller.live_configuration import LiveConfiguration
from potbot.controller.node_controls import NodeControls
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError
from potbot.persistence.database import Database


async def body(request: Request, maximum: int) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InvalidInputError("A JSON control review is required")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum:
            raise InvalidInputError("The control review exceeds its byte allowance")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError

    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidInputError("An unambiguous, bounded JSON review is required") from None


def register_live_control_routes(
    app: FastAPI,
    database: Database,
    actor: Callable[..., WorkspaceContext],
    headers: dict[str, str],
    *,
    setup_available: bool,
) -> None:
    configuration = LiveConfiguration(database)
    nodes = NodeControls(database)

    @app.get("/v1/ui/settings/live")
    async def live_settings(request: Request) -> JSONResponse:
        return JSONResponse(
            await run_in_threadpool(
                configuration.read, actor(request), setup_available=setup_available
            ),
            headers=headers,
        )

    @app.put("/v1/ui/settings/live")
    async def save_settings(request: Request) -> JSONResponse:
        context = actor(request, mutate=True)
        review = await body(request, 32_768)
        return JSONResponse(
            await run_in_threadpool(configuration.save, context, review), headers=headers
        )

    @app.get("/v1/ui/swarm/control")
    async def node_controls(request: Request) -> JSONResponse:
        return JSONResponse(
            await run_in_threadpool(nodes.read, actor(request), setup_available=setup_available),
            headers=headers,
        )

    @app.post("/v1/ui/swarm/control", status_code=202)
    async def control_node(request: Request) -> JSONResponse:
        context = actor(request, mutate=True)
        review = await body(request, 4096)
        # actor has authenticated the separate setup capability. Its MAC is
        # verified again by the native supervisor; the key never enters SQL.
        result = await run_in_threadpool(
            nodes.request, context, review, request.headers["x-potbot-setup-token"]
        )
        return JSONResponse(result, status_code=202, headers=headers)
