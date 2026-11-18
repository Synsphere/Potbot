"""Exact selected-task checks and launches on the existing operation queue."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.api.ui_discovery import _sha, _uuid
from potbot.api.ui_operation_workbench import _integer
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.operation_workbench import parse_request
from potbot.domain.result_assertions import validate_result_oracle
from potbot.domain.serialization import canonical_json

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

MAX_BODY_BYTES = 131_072
MAX_RESPONSE_BYTES = 1_500_000
ORACLE_SCHEMA = "potbot-task-result-oracle/v1"
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}


async def _body(request: Request) -> dict[str, Any]:
    if (
        len(request.headers.getlist("content-type")) != 1
        or request.headers["content-type"].split(";", 1)[0] != "application/json"
    ):
        raise InvalidInputError("Selected task controls require one JSON content type")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise InvalidInputError("Selected task input exceeds its byte allowance")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def constant(_value: str) -> None:
        raise ValueError

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        canonical_json(value).encode("utf-8", "strict")
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidInputError("Selected task input must be unambiguous finite JSON") from None
    if not isinstance(value, dict) or value.get("confirm") is not True:
        raise InvalidInputError("Selected task controls require explicit confirmation")
    return value


def register_task_execution_routes(
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
        from potbot.controller.task_execution import TaskExecutionService

        service = TaskExecutionService(database)

    def actor(request: Request, *, mutation: bool = False) -> WorkspaceContext:
        if len(request.headers.getlist("authorization")) != 1 or any(
            len(request.headers.getlist(name)) > 1 for name in ("origin", "x-potbot-setup-token")
        ):
            raise ScopeDeniedError("Selected task authorization is missing or ambiguous")
        scope = authenticate(request.headers.get("authorization"))
        if scope.workspace_id != workspace_id:
            raise ScopeDeniedError("Selected task belongs to another workspace")
        origins = request.headers.getlist("origin")
        if (origins and origins[0] not in allowed_origins) or (mutation and len(origins) != 1):
            raise ScopeDeniedError("Selected task controls require an exact allowed origin")
        return scope

    def query(request: Request, allowed: set[str] | None = None) -> dict[str, str]:
        pairs = request.query_params.multi_items()
        values = dict(pairs)
        if (
            len(request.scope.get("query_string", b"")) > 4096
            or len(pairs) != len(values)
            or set(values) - (allowed or set())
        ):
            raise InvalidInputError("Selected task query is invalid or ambiguous")
        return values

    def reply(value: dict[str, Any], *, queued: bool = False) -> JSONResponse:
        if len(canonical_json(value).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise InvalidInputError("Selected task response exceeds its byte allowance")
        return JSONResponse(value, headers=_HEADERS, status_code=202 if queued else 200)

    async def command(
        request: Request, *, setup: bool = True
    ) -> tuple[WorkspaceContext, dict[str, Any], str, str | None]:
        scope = actor(request, mutation=True)
        query(request)
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or not _KEY.fullmatch(keys[0]):
            raise InvalidInputError("Selected task commands require one stable key")
        request.state.idempotency_key = keys[0]
        token = None
        if setup:
            if authorize_setup is None:
                raise CapabilityUnavailableError("Selected task execution requires operator setup")
            if len(request.headers.getlist("x-potbot-setup-token")) != 1:
                raise ScopeDeniedError("Selected task execution requires one operator capability")
            token = request.headers["x-potbot-setup-token"]
            authorize_setup(token)
        return scope, await _body(request), keys[0], token

    base = "/v1/ui/task-execution/selections/{selection_id}"

    @app.get(base + "/context")
    async def context(selection_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request)
        return reply(await run_in_threadpool(service.context, scope, _uuid(selection_id)))

    @app.post(base + "/checks")
    async def check(selection_id: str, request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        if set(value) != {"selectionSha256", "request", "acceptance", "confirm"}:
            raise InvalidInputError(
                "A selected task check requires its exact request and assertions"
            )
        spec = parse_request(value["request"]).to_dict()
        acceptance = validate_result_oracle(value["acceptance"], schema=ORACLE_SCHEMA)
        return reply(
            await run_in_threadpool(
                service.check,
                scope,
                _uuid(selection_id),
                _sha(value["selectionSha256"]),
                spec,
                acceptance,
                key,
                setup_token=token,
            ),
            queued=True,
        )

    @app.post(base + "/runs")
    async def launch(selection_id: str, request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        if set(value) != {"checkJobId", "bindingSha256", "confirm"}:
            raise InvalidInputError("A selected task run requires its exact retained check binding")
        return reply(
            await run_in_threadpool(
                service.launch,
                scope,
                _uuid(selection_id),
                _uuid(value["checkJobId"]),
                _sha(value["bindingSha256"]),
                key,
                setup_token=token,
            ),
            queued=True,
        )

    @app.get(base + "/jobs")
    async def jobs(selection_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"limit", "after"})
        after = values.get("after")
        if after is not None and (
            not after
            or len(after) > 512
            or any(ord(char) < 33 or ord(char) > 126 for char in after)
        ):
            raise InvalidInputError("Selected task history cursor is invalid")
        return reply(
            await run_in_threadpool(
                service.jobs,
                scope,
                _uuid(selection_id),
                limit=_integer(values.get("limit", "25"), minimum=1, maximum=25),
                after=after,
            )
        )

    @app.get(base + "/jobs/{job_id}")
    async def inspect(selection_id: str, job_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request)
        return reply(
            await run_in_threadpool(service.inspect, scope, _uuid(selection_id), _uuid(job_id))
        )

    @app.get(base + "/jobs/{job_id}/events")
    async def events(selection_id: str, job_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"after", "limit"})
        return reply(
            await run_in_threadpool(
                service.events,
                scope,
                _uuid(selection_id),
                _uuid(job_id),
                after=_integer(values.get("after", "0"), maximum=2**53 - 1),
                limit=_integer(values.get("limit", "100"), minimum=1, maximum=100),
            )
        )

    @app.get(base + "/jobs/{job_id}/result")
    async def result(selection_id: str, job_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request)
        return reply(
            await run_in_threadpool(service.result, scope, _uuid(selection_id), _uuid(job_id))
        )

    @app.get(base + "/jobs/{job_id}/artifact")
    async def artifact(selection_id: str, job_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"offset", "length"})
        return reply(
            await run_in_threadpool(
                service.artifact,
                scope,
                _uuid(selection_id),
                _uuid(job_id),
                offset=_integer(values.get("offset", "0"), maximum=2**53 - 1),
                length=_integer(values.get("length", "262144"), minimum=1, maximum=262144),
            )
        )

    @app.post(base + "/jobs/{job_id}/cancel")
    async def cancel(selection_id: str, job_id: str, request: Request) -> JSONResponse:
        scope, value, key, _token = await command(request, setup=False)
        if set(value) != {"confirm"}:
            raise InvalidInputError("Selected task cancellation accepts only confirmation")
        return reply(
            await run_in_threadpool(service.cancel, scope, _uuid(selection_id), _uuid(job_id), key)
        )
