"""Private task discovery review; native semantic refinement remains explicit."""

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
from potbot.domain.serialization import canonical_json

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MAX_RESPONSE_BYTES = 1_500_000


def register_task_discovery_routes(
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
        from potbot.controller.task_discovery import TaskDiscoveryService

        service = TaskDiscoveryService(database)

    def actor(request: Request, *, mutate: bool = False) -> WorkspaceContext:
        if len(request.headers.getlist("authorization")) != 1:
            raise ScopeDeniedError("Task discovery requires one authenticated scope")
        context = authenticate(request.headers.get("authorization"))
        if context.workspace_id != workspace_id:
            raise ScopeDeniedError("Task discovery belongs to another workspace")
        origins = request.headers.getlist("origin")
        if len(origins) > 1 or (origins and origins[0] not in allowed_origins):
            raise ScopeDeniedError("Task discovery origin is not approved")
        if mutate:
            if authorize_setup is None:
                raise CapabilityUnavailableError("Task discovery controls require operator setup")
            if len(origins) != 1:
                raise ScopeDeniedError("Task discovery controls require an exact origin")
            if len(request.headers.getlist("x-potbot-setup-token")) != 1:
                raise InvalidInputError("Task discovery requires one setup capability")
            authorize_setup(request.headers.get("x-potbot-setup-token"))
        return context

    def query(request: Request, allowed: set[str]) -> dict[str, str]:
        values = dict(request.query_params)
        if (
            len(request.scope.get("query_string", b"")) > 4096
            or len(values) != len(request.query_params.multi_items())
            or set(values) - allowed
        ):
            raise InvalidInputError("Task discovery query fields are invalid or ambiguous")
        return values

    def reply(value: dict[str, Any]) -> JSONResponse:
        if len(canonical_json(value).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise InvalidInputError("Task discovery response exceeds its byte allowance")
        return JSONResponse(value, headers=_HEADERS)

    async def command(request: Request) -> tuple[WorkspaceContext, dict[str, Any], str, str]:
        context = actor(request, mutate=True)
        query(request, set())
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or not _KEY.fullmatch(keys[0]):
            raise InvalidInputError("Task discovery requires one stable command key")
        request.state.idempotency_key = keys[0]
        if len(request.headers.getlist("content-type")) != 1:
            raise InvalidInputError("Task discovery controls require one JSON content type")
        return (
            context,
            await _body(request),
            keys[0],
            str(request.headers["x-potbot-setup-token"]),
        )

    def reviewed(value: dict[str, Any], *, select: bool = False) -> tuple[int, str]:
        fields = {"revision", "digest", "confirm"} | ({"candidateId"} if select else set())
        if (
            set(value) != fields
            or type(value["revision"]) is not int
            or not 1 <= value["revision"] < 2**53 - 1
        ):
            raise InvalidInputError(
                "Task discovery requires the exact reviewed revision and digest"
            )
        return value["revision"], _sha(value["digest"])

    @app.get("/v1/ui/task-discovery/context")
    async def context(request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"episodeId", "stateId"})
        if set(values) != {"episodeId", "stateId"}:
            raise InvalidInputError("Task discovery requires the exact task and repository state")
        return reply(
            await run_in_threadpool(
                service.context, scope, _uuid(values["episodeId"]), _uuid(values["stateId"])
            )
        )

    @app.get("/v1/ui/task-discovery/searches")
    async def searches(request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"episodeId", "limit", "after"})
        raw = values.get("limit", "25")
        if (
            not raw.isascii()
            or not raw.isdigit()
            or not 1 <= int(raw) <= 25
            or str(int(raw)) != raw
        ):
            raise InvalidInputError("Task discovery page limit must be one to twenty-five")
        after = values.get("after")
        if after is not None:
            _uuid(after)
        return reply(
            await run_in_threadpool(
                service.searches,
                scope,
                episode_id=_uuid(values["episodeId"]) if "episodeId" in values else None,
                limit=int(raw),
                after=after,
            )
        )

    @app.get("/v1/ui/task-discovery/searches/{identity}")
    async def search(identity: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request, set())
        return reply(await run_in_threadpool(service.search, scope, _uuid(identity)))

    @app.get("/v1/ui/task-discovery/results/{identity}")
    async def result(identity: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request, set())
        return reply(await run_in_threadpool(service.result, scope, _uuid(identity)))

    @app.get("/v1/ui/task-discovery/selections/{identity}")
    async def selection(identity: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request, set())
        return reply(await run_in_threadpool(service.selection, scope, _uuid(identity)))

    @app.get("/v1/ui/task-discovery/searches/{identity}/candidates/{candidate_id}")
    async def inspect_candidate(identity: str, candidate_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request, set())
        return reply(
            await run_in_threadpool(
                service.inspect_candidate, scope, _uuid(identity), _sha(candidate_id)
            )
        )

    @app.post("/v1/ui/task-discovery/searches")
    async def create(request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        from potbot.controller.task_discovery import TaskDiscoveryService

        TaskDiscoveryService._request(value)
        return reply(await run_in_threadpool(service.create, scope, value, key, setup_token=token))

    @app.post("/v1/ui/task-discovery/searches/{identity}/cancel")
    async def cancel(identity: str, request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        revision, digest = reviewed(value)
        return reply(
            await run_in_threadpool(
                service.cancel, scope, _uuid(identity), revision, digest, key, setup_token=token
            )
        )

    @app.post("/v1/ui/task-discovery/searches/{identity}/select")
    async def select(identity: str, request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        revision, digest = reviewed(value, select=True)
        return reply(
            await run_in_threadpool(
                service.select,
                scope,
                _uuid(identity),
                revision,
                digest,
                _sha(value["candidateId"]),
                key,
                setup_token=token,
            )
        )
