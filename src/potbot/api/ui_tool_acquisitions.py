"""Public review and durable requests for the operator-owned tool lifecycle.

These routes have no Git, process, source path, runtime manifest or executable
capability. Only the separate native host worker consumes authorized requests.
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

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_ACTIONS = frozenset({"rollback", "reconcile", "repin", "postflight", "cancel"})
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def _uuid(value: Any) -> UUID:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return UUID(value)
    except (TypeError, ValueError):
        raise InvalidInputError("Tool acquisition requires a canonical UUID") from None


def _number(value: Any, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise InvalidInputError("Tool acquisition bound must be an exact integer")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("Tool acquisition requires exact reviewed SHA-256 evidence")
    return value


async def _body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InvalidInputError("Tool acquisition requests require JSON")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise InvalidInputError("Tool acquisition request exceeds its byte allowance")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if name in result:
                raise ValueError("duplicate field")
            result[name] = value
        return result

    def nonfinite(_: str) -> None:
        raise ValueError("nonfinite value")

    try:
        value = json.loads(raw.decode(), object_pairs_hook=unique, parse_constant=nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidInputError("Tool acquisition request is invalid JSON") from None
    if not isinstance(value, dict) or value.get("confirm") is not True:
        raise InvalidInputError("Tool acquisition requires an explicitly confirmed object")
    return value


def register_tool_acquisition_routes(
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
        from potbot.tools.tool_acquisition import ToolAcquisitionService

        service = ToolAcquisitionService(database)

    def actor(request: Request, *, mutate: bool = False) -> WorkspaceContext:
        identity = authenticate(request.headers.get("authorization"))
        if identity.workspace_id != workspace_id:
            raise ScopeDeniedError("Tool acquisition belongs to another workspace")
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Tool acquisition origin is not approved")
        if mutate:
            if authorize_setup is None:
                raise CapabilityUnavailableError("Tool acquisition controls require operator setup")
            if origin is None:
                raise ScopeDeniedError("Tool acquisition controls require an exact origin")
            authorize_setup(request.headers.get("x-potbot-setup-token"))
        return identity

    def query(request: Request, allowed: frozenset[str] = frozenset()) -> dict[str, str]:
        pairs = request.query_params.multi_items()
        if len({name for name, _ in pairs}) != len(pairs) or any(
            name not in allowed for name, _ in pairs
        ):
            raise InvalidInputError("Unexpected or duplicate tool acquisition query")
        return dict(pairs)

    def key(request: Request) -> str:
        values = request.headers.getlist("idempotency-key")
        if len(values) != 1 or not _KEY.fullmatch(values[0]):
            raise InvalidInputError("A bounded Idempotency-Key is required")
        request.state.idempotency_key = values[0]
        return values[0]

    def page(request: Request) -> tuple[int, str | None]:
        values = query(request, frozenset({"limit", "after"}))
        raw = values.get("limit", "25")
        if not re.fullmatch(r"[1-9]|[1-4][0-9]|50", raw):
            raise InvalidInputError("Tool acquisition page size must be between 1 and 50")
        after = values.get("after")
        if after is not None and (not after or len(after) > 256):
            raise InvalidInputError("Invalid tool acquisition cursor")
        return int(raw), after

    def response(value: dict[str, Any]) -> JSONResponse:
        return JSONResponse(value, headers=_HEADERS)

    @app.get("/v1/ui/tools/acquisitions/recipes")
    async def recipes(request: Request) -> JSONResponse:
        identity = actor(request)
        limit, after = page(request)
        return response(
            await run_in_threadpool(service.recipes, identity, limit=limit, after=after)
        )

    @app.get("/v1/ui/tools/acquisitions")
    async def listing(request: Request) -> JSONResponse:
        identity = actor(request)
        limit, after = page(request)
        return response(await run_in_threadpool(service.list, identity, limit=limit, after=after))

    @app.post("/v1/ui/tools/acquisitions")
    async def create(request: Request) -> JSONResponse:
        identity = actor(request, mutate=True)
        query(request)
        request_key = key(request)
        value = await _body(request)
        if set(value) != {"recipeId", "commit", "confirm"}:
            raise InvalidInputError("Tool source request fields are invalid")
        if not isinstance(value["commit"], str) or not _COMMIT.fullmatch(value["commit"]):
            raise InvalidInputError("Tool source requires an exact commit")
        return response(
            await run_in_threadpool(
                service.request,
                identity,
                _uuid(value["recipeId"]),
                value["commit"],
                request_key,
                setup_token=request.headers.get("x-potbot-setup-token"),
            )
        )

    @app.get("/v1/ui/tools/acquisitions/{operation_id}/events")
    async def events(request: Request, operation_id: str) -> JSONResponse:
        identity = actor(request)
        values = query(request, frozenset({"after", "limit"}))
        for raw in values.values():
            if not re.fullmatch(r"0|[1-9][0-9]{0,15}", raw):
                raise InvalidInputError("Tool event bounds must be exact nonnegative integers")
        after = _number(int(values.get("after", "0")), maximum=2**53 - 1)
        limit = _number(int(values.get("limit", "50")), maximum=50)
        if limit == 0:
            raise InvalidInputError("Tool event limit must be positive")
        return response(
            await run_in_threadpool(
                service.events,
                identity,
                _uuid(operation_id),
                after=after,
                limit=limit,
            )
        )

    @app.get("/v1/ui/tools/acquisitions/{operation_id}")
    async def detail(request: Request, operation_id: str) -> JSONResponse:
        identity = actor(request)
        query(request)
        return response(await run_in_threadpool(service.inspect, identity, _uuid(operation_id)))

    @app.post("/v1/ui/tools/acquisitions/{operation_id}/{action}")
    async def control(request: Request, operation_id: str, action: str) -> JSONResponse:
        identity = actor(request, mutate=True)
        query(request)
        request_key = key(request)
        operation = _uuid(operation_id)
        if action not in _ACTIONS | {"approve-build", "approve", "reject"}:
            raise InvalidInputError("Unknown tool acquisition action")
        value = await _body(request)
        evidence_field = (
            "reviewSha256"
            if action == "approve-build"
            else "evidenceSha256"
            if action in {"approve", "reject"}
            else "bindingSha256"
        )
        if set(value) != {"revision", evidence_field, "confirm"}:
            raise InvalidInputError("Tool acquisition control fields are invalid")
        revision = _number(value["revision"], maximum=2**53 - 1)
        evidence = _sha(value[evidence_field])
        token = request.headers.get("x-potbot-setup-token")
        if action == "approve-build":
            result = await run_in_threadpool(
                service.approve_build,
                identity,
                operation,
                revision,
                evidence,
                request_key,
                setup_token=token,
            )
        elif action in {"approve", "reject"}:
            result = await run_in_threadpool(
                service.decide,
                identity,
                operation,
                revision,
                evidence,
                action == "approve",
                request_key,
                setup_token=token,
            )
        else:
            result = await run_in_threadpool(
                service.action,
                identity,
                operation,
                action,
                revision,
                evidence,
                request_key,
                setup_token=token,
            )
        return response(result)
