"""Authenticated discovery review; native probes remain explicit host commands."""

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
from potbot.domain.discovery import CAPABILITY_CLASSES, EFFECTS, KINDS, PLATFORMS
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.syn import parse_type_ref

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_FILTERS = frozenset(
    {
        "classes",
        "inputTypes",
        "outputTypes",
        "platform",
        "permittedEffects",
        "permittedScopes",
        "requireLocal",
    }
)


def _uuid(value: Any) -> UUID:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("Discovery requires a canonical UUID") from None


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("Discovery requires an exact reviewed SHA-256")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**53 - 1:
        raise InvalidInputError("Discovery requires a bounded exact revision")
    return value


def discovery_constraints(values: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in _FILTERS & values.keys():
        raw = values[name]
        if len(raw.encode()) > 4096:
            raise InvalidInputError("Discovery filter exceeds its byte allowance")
        if name == "requireLocal":
            if raw not in {"true", "false"}:
                raise InvalidInputError("The local-runtime filter must be true or false")
            result[name] = raw == "true"
        elif name == "platform":
            if raw not in PLATFORMS:
                raise InvalidInputError("Unknown discovery platform")
            result[name] = raw
        else:
            items = raw.split(",") if raw else []
            if (
                len(items) > 32
                or len(set(items)) != len(items)
                or any(
                    not item or len(item) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in item)
                    for item in items
                )
            ):
                raise InvalidInputError("Discovery filters require bounded unique literal values")
            if name == "classes" and not set(items) <= CAPABILITY_CLASSES:
                raise InvalidInputError("Unknown discovery capability class")
            if name == "permittedEffects" and not set(items) <= EFFECTS:
                raise InvalidInputError("Unknown discovery effect")
            if name in {"inputTypes", "outputTypes"}:
                for item in items:
                    parse_type_ref(item)
            result[name] = items
    return result


async def _body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InvalidInputError("Discovery controls require JSON")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8192:
            raise InvalidInputError("Discovery request exceeds its byte allowance")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def nonfinite(_: str) -> None:
        raise ValueError

    try:
        value = json.loads(data.decode(), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidInputError("Discovery request is not unambiguous JSON") from None
    if not isinstance(value, dict) or value.get("confirm") is not True:
        raise InvalidInputError("Discovery controls require an explicit confirmation")
    return value


def register_discovery_routes(
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
        from potbot.controller.capability_discovery import DiscoveryService

        service = DiscoveryService(database)

    def actor(request: Request, *, mutate: bool = False) -> WorkspaceContext:
        context = authenticate(request.headers.get("authorization"))
        if context.workspace_id != workspace_id:
            raise ScopeDeniedError("Discovery belongs to another workspace")
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Discovery origin is not approved")
        if mutate:
            if authorize_setup is None:
                raise CapabilityUnavailableError("Discovery controls require operator setup")
            if origin is None:
                raise ScopeDeniedError("Discovery controls require an exact origin")
            if len(request.headers.getlist("x-potbot-setup-token")) != 1:
                raise InvalidInputError("A single setup capability is required")
            authorize_setup(request.headers.get("x-potbot-setup-token"))
        return context

    def query(request: Request, allowed: frozenset[str] = frozenset()) -> dict[str, str]:
        pairs = request.query_params.multi_items()
        if (
            len(request.scope.get("query_string", b"")) > 8192
            or len(pairs) != len({key for key, _ in pairs})
            or any(key not in allowed for key, _ in pairs)
        ):
            raise InvalidInputError("Unexpected, duplicate or oversized discovery query")
        return dict(pairs)

    def page(values: dict[str, str]) -> tuple[int, str | None]:
        raw = values.get("limit", "25")
        if not re.fullmatch(r"[1-9]|[1-4][0-9]|50", raw):
            raise InvalidInputError("Discovery page size must be between one and fifty")
        after = values.get("after")
        if after is not None and (not after or len(after) > 512):
            raise InvalidInputError("Discovery cursor is invalid")
        return int(raw), after

    def key(request: Request) -> str:
        values = request.headers.getlist("idempotency-key")
        if len(values) != 1 or not _KEY.fullmatch(values[0]):
            raise InvalidInputError("Discovery requires a stable Idempotency-Key")
        request.state.idempotency_key = values[0]
        return values[0]

    def response(value: dict[str, Any]) -> JSONResponse:
        return JSONResponse(value, headers=_HEADERS)

    @app.get("/v1/ui/discovery/sources")
    async def sources(request: Request) -> JSONResponse:
        context = actor(request)
        limit, after = page(query(request, frozenset({"limit", "after"})))
        return response(await run_in_threadpool(service.sources, context, limit=limit, after=after))

    @app.get("/v1/ui/discovery/sources/{identity}")
    async def source(request: Request, identity: str) -> JSONResponse:
        context = actor(request)
        query(request)
        return response(await run_in_threadpool(service.source, context, _uuid(identity)))

    @app.get("/v1/ui/discovery/publishers")
    async def publishers(request: Request) -> JSONResponse:
        context = actor(request)
        values = query(request, frozenset({"limit", "after", "sourceId"}))
        limit, after = page(values)
        source_id = _uuid(values["sourceId"]) if "sourceId" in values else None
        return response(
            await run_in_threadpool(
                service.publishers, context, source_id=source_id, limit=limit, after=after
            )
        )

    @app.get("/v1/ui/discovery/candidates")
    async def candidates(request: Request) -> JSONResponse:
        context = actor(request)
        values = query(request, frozenset({"limit", "after", "q", "kind", "sourceId"}) | _FILTERS)
        limit, after = page(values)
        if len(values.get("q", "")) > 500 or ("kind" in values and values["kind"] not in KINDS):
            raise InvalidInputError("Discovery search is outside its supported bounds")
        constraints = discovery_constraints(values)
        return response(
            await run_in_threadpool(
                service.candidates,
                context,
                query=values.get("q", ""),
                kind=values.get("kind"),
                source_id=_uuid(values["sourceId"]) if "sourceId" in values else None,
                limit=limit,
                after=after,
                constraints=constraints or None,
            )
        )

    @app.get("/v1/ui/discovery/candidates/{identity}")
    async def candidate(request: Request, identity: str) -> JSONResponse:
        context = actor(request)
        query(request)
        return response(await run_in_threadpool(service.candidate, context, _uuid(identity)))

    @app.get("/v1/ui/discovery/checks")
    async def checks(request: Request) -> JSONResponse:
        context = actor(request)
        limit, after = page(query(request, frozenset({"limit", "after"})))
        return response(await run_in_threadpool(service.checks, context, limit=limit, after=after))

    @app.get("/v1/ui/discovery/checks/{identity}")
    async def check(request: Request, identity: str) -> JSONResponse:
        context = actor(request)
        query(request)
        return response(await run_in_threadpool(service.check, context, _uuid(identity)))

    @app.post("/v1/ui/discovery/candidates/{identity}/checks")
    async def create_check(request: Request, identity: str) -> JSONResponse:
        context = actor(request, mutate=True)
        query(request)
        request_key, value = key(request), await _body(request)
        if set(value) != {"snapshotId", "candidateSha256", "agentId", "confirm"}:
            raise InvalidInputError("Exact discovery review fields are required")
        return response(
            await run_in_threadpool(
                service.create_check,
                context,
                _uuid(identity),
                _uuid(value["snapshotId"]),
                _sha(value["candidateSha256"]),
                _uuid(value["agentId"]),
                request_key,
                setup_token=request.headers.get("x-potbot-setup-token"),
            )
        )

    @app.post("/v1/ui/discovery/{collection}/{identity}/{action}")
    async def control(
        request: Request, collection: str, identity: str, action: str
    ) -> JSONResponse:
        context = actor(request, mutate=True)
        query(request)
        request_key, value = key(request), await _body(request)
        allowed = {
            "sources": {"enable", "disable", "revoke"},
            "publishers": {"approve", "block", "revoke"},
            "checks": {"handoff", "cancel"},
        }
        if (
            collection not in allowed
            or action not in allowed[collection]
            or set(value) != {"revision", "digest", "confirm"}
        ):
            raise InvalidInputError("Unsupported discovery control fields")
        args = (
            _uuid(identity),
            action,
            _revision(value["revision"]),
            _sha(value["digest"]),
            request_key,
        )
        if collection == "checks":
            result = await run_in_threadpool(
                service.action,
                context,
                args[0],
                args[1],
                args[2],
                args[3],
                args[4],
                setup_token=request.headers.get("x-potbot-setup-token"),
            )
        else:
            kind = "source" if collection == "sources" else "publisher"
            result = await run_in_threadpool(
                service.control,
                context,
                kind,
                args[0],
                args[1],
                args[2],
                args[3],
                args[4],
                setup_token=request.headers.get("x-potbot-setup-token"),
            )
        return response(result)
