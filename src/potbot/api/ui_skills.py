"""Authenticated portable skill import, review and operator lifecycle HTTP adapter."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.context.rpc_contract import canonical_uuid
from potbot.context.skill_packages import MAX_ARCHIVE_BYTES, fetch_github_skill, parse_skill_zip
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

MAX_IMPORT_BODY_BYTES = 6 * 1024 * 1024
MAX_ACTION_BODY_BYTES = 65536
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _query(request: Request, allowed: frozenset[str] = frozenset()) -> dict[str, str]:
    pairs = request.query_params.multi_items()
    if len(pairs) != len({key for key, _ in pairs}) or any(key not in allowed for key, _ in pairs):
        raise InvalidInputError("Unexpected or duplicate skill query")
    return dict(pairs)


def _integer(value: str, *, maximum: int, minimum: int = 0) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]{0,15}", value):
        raise InvalidInputError("Invalid skill page or resource bound")
    result = int(value)
    if not minimum <= result <= maximum:
        raise InvalidInputError("Skill page or resource bound is outside its allowance")
    return result


def _revision(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**53:
        raise InvalidInputError("Skill control requires an exact current revision")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise InvalidInputError("Skill action requires an exact release or plan digest")
    return value


def _idempotency(request: Request) -> str:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1 or _KEY.fullmatch(values[0]) is None:
        raise InvalidInputError("A bounded Idempotency-Key header is required")
    request.state.idempotency_key = values[0]
    return values[0]


async def _body(request: Request, *, maximum: int = MAX_ACTION_BODY_BYTES) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InvalidInputError("Skill requests require JSON")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum:
            raise InvalidInputError("Skill request exceeds its byte allowance")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if name in result:
                raise ValueError("duplicate field")
            result[name] = value
        return result

    def constant(_: str) -> None:
        raise ValueError("non-finite JSON")

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidInputError("Invalid skill request document") from None
    if not isinstance(result, dict):
        raise InvalidInputError("Skill request must be a JSON object")
    return result


def register_skill_acquisition_routes(
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
        from potbot.context.skill_acquisition import SkillAcquisition

        service = SkillAcquisition(database)

    def actor(request: Request, *, mutation: bool = False) -> WorkspaceContext:
        if any(
            len(request.headers.getlist(name)) > 1
            for name in ("authorization", "origin", "x-potbot-setup-token")
        ):
            raise ScopeDeniedError("Ambiguous skill authorization")
        origin = request.headers.get("origin")
        if (origin is not None and origin not in allowed_origins) or (mutation and origin is None):
            raise ScopeDeniedError("Skill mutations require an allowed browser origin")
        context = authenticate(request.headers.get("authorization"))
        if context.workspace_id != workspace_id:
            raise ScopeDeniedError("Skill request is outside the configured workspace")
        return context

    def setup(request: Request) -> str:
        if authorize_setup is None:
            raise CapabilityUnavailableError("Operator setup is unavailable on this listener")
        token = request.headers.get("x-potbot-setup-token")
        authorize_setup(token)
        if not isinstance(token, str) or not token:
            raise ScopeDeniedError("Operator setup permission required")
        return token

    def response(value: dict[str, Any]) -> JSONResponse:
        return JSONResponse(value, headers=_HEADERS)

    def page(request: Request) -> tuple[int, str | None]:
        query = _query(request, frozenset({"limit", "after"}))
        limit = _integer(query.get("limit", "25"), maximum=50, minimum=1)
        after = query.get("after")
        if after is not None and (not after or len(after) > 512):
            raise InvalidInputError("Invalid skill page cursor")
        return limit, after

    @app.get("/v1/ui/skills")
    async def listing(request: Request) -> JSONResponse:
        context = actor(request)
        limit, after = page(request)
        return response(await run_in_threadpool(service.list, context, limit=limit, after=after))

    @app.get("/v1/ui/skills/packages")
    async def packages(request: Request) -> JSONResponse:
        context = actor(request)
        limit, after = page(request)
        return response(
            await run_in_threadpool(service.packages, context, limit=limit, after=after)
        )

    @app.post("/v1/ui/skills/imports/zip")
    async def import_zip(request: Request) -> JSONResponse:
        context = actor(request, mutation=True)
        _query(request)
        setup(request)
        key = _idempotency(request)
        body = await _body(request, maximum=MAX_IMPORT_BODY_BYTES)
        if set(body) != {"archiveBase64", "subdirectory"} or not all(
            isinstance(value, str) for value in body.values()
        ):
            raise InvalidInputError("ZIP import requires archiveBase64 and subdirectory")
        try:
            raw = base64.b64decode(body["archiveBase64"], validate=True)
        except (ValueError, binascii.Error):
            raise InvalidInputError("Invalid base64 ZIP archive") from None
        if not 1 <= len(raw) <= MAX_ARCHIVE_BYTES:
            raise InvalidInputError("Skill ZIP exceeds its compressed byte allowance")
        path = body["subdirectory"]
        from potbot.context.skill_packages import validate_resource_path

        if path:
            validate_resource_path(path)
        source = {"kind": "zip", "sha256": hashlib.sha256(raw).hexdigest(), "subdirectory": path}
        result = await run_in_threadpool(
            service.acquire,
            context,
            source,
            key,
            lambda: parse_skill_zip(raw, subdirectory=path),
        )
        return response(result)

    @app.post("/v1/ui/skills/imports/github")
    async def import_github(request: Request) -> JSONResponse:
        context = actor(request, mutation=True)
        _query(request)
        setup(request)
        key = _idempotency(request)
        body = await _body(request)
        if set(body) != {"repository", "commit", "path"} or not all(
            isinstance(value, str) for value in body.values()
        ):
            raise InvalidInputError("GitHub import requires repository, exact commit and path")
        source = {
            "kind": "github",
            "repository": body["repository"],
            "commit": body["commit"],
            "subdirectory": body["path"],
        }
        result = await run_in_threadpool(
            service.acquire,
            context,
            source,
            key,
            lambda: fetch_github_skill(body["repository"], body["commit"], body["path"]),
        )
        return response(result)

    @app.get("/v1/ui/skills/packages/{package_id}")
    async def package(request: Request, package_id: str) -> JSONResponse:
        context = actor(request)
        _query(request)
        return response(
            await run_in_threadpool(service.package, context, canonical_uuid(package_id))
        )

    @app.get("/v1/ui/skills/packages/{package_id}/plan")
    async def plan(request: Request, package_id: str) -> JSONResponse:
        context = actor(request)
        query = _query(request, frozenset({"skillId"}))
        target = canonical_uuid(query["skillId"]) if "skillId" in query else None
        return response(
            await run_in_threadpool(
                service.plan, context, canonical_uuid(package_id), skill_id=target
            )
        )

    @app.post("/v1/ui/skills/packages/{package_id}/install")
    async def install(request: Request, package_id: str) -> JSONResponse:
        context = actor(request, mutation=True)
        _query(request)
        token, key = setup(request), _idempotency(request)
        body = await _body(request)
        if (
            set(body) != {"planSha256", "expectedRevision", "skillId", "confirm"}
            or body["confirm"] is not True
        ):
            raise InvalidInputError("Installation requires an exact reviewed plan and confirmation")
        target = None if body["skillId"] is None else canonical_uuid(body["skillId"])
        result = await run_in_threadpool(
            service.install,
            context,
            canonical_uuid(package_id),
            _digest(body["planSha256"]),
            _revision(body["expectedRevision"]),
            key,
            skill_id=target,
            setup_token=token,
        )
        return response(result)

    def resource_query(request: Request, *, release: bool) -> tuple[str, int, int, str | None]:
        allowed = {"path", "offset", "length"} | ({"releaseSha256"} if release else set())
        query = _query(request, frozenset(allowed))
        if "path" not in query or (release and "releaseSha256" not in query):
            raise InvalidInputError("Resource read requires its exact package path and release")
        from potbot.context.skill_packages import validate_resource_path

        path = validate_resource_path(query["path"])
        offset = _integer(query.get("offset", "0"), maximum=1024 * 1024)
        length = _integer(query.get("length", "65536"), maximum=65536, minimum=1)
        return path, offset, length, _digest(query["releaseSha256"]) if release else None

    @app.get("/v1/ui/skills/packages/{package_id}/resources")
    async def package_resources(request: Request, package_id: str) -> JSONResponse:
        context = actor(request)
        path, offset, length, _ = resource_query(request, release=False)
        return response(
            await run_in_threadpool(
                service.package_resource,
                context,
                canonical_uuid(package_id),
                path,
                offset=offset,
                length=length,
            )
        )

    @app.get("/v1/ui/skills/{skill_id}")
    async def skill(request: Request, skill_id: str) -> JSONResponse:
        context = actor(request)
        _query(request)
        return response(await run_in_threadpool(service.inspect, context, canonical_uuid(skill_id)))

    @app.get("/v1/ui/skills/{skill_id}/history")
    async def history(request: Request, skill_id: str) -> JSONResponse:
        context = actor(request)
        limit, after = page(request)
        return response(
            await run_in_threadpool(
                service.history, context, canonical_uuid(skill_id), limit=limit, after=after
            )
        )

    @app.get("/v1/ui/skills/{skill_id}/resources")
    async def resources(request: Request, skill_id: str) -> JSONResponse:
        context = actor(request)
        path, offset, length, release = resource_query(request, release=True)
        return response(
            await run_in_threadpool(
                service.resource,
                context,
                canonical_uuid(skill_id),
                release,
                path,
                offset=offset,
                length=length,
            )
        )

    @app.post("/v1/ui/skills/{skill_id}/{action}")
    async def control(request: Request, skill_id: str, action: str) -> JSONResponse:
        context = actor(request, mutation=True)
        _query(request)
        token, key = setup(request), _idempotency(request)
        if action not in {"activate", "disable", "rollback", "revoke"}:
            raise InvalidInputError("Unsupported skill lifecycle action")
        body = await _body(request)
        if (
            set(body) != {"expectedRevision", "releaseSha256", "confirm"}
            or body["confirm"] is not True
        ):
            raise InvalidInputError("Skill control requires an exact revision and confirmation")
        release = _digest(body["releaseSha256"]) if action == "rollback" else None
        if action != "rollback" and body["releaseSha256"] is not None:
            raise InvalidInputError("Only rollback accepts a retained release selector")
        result = await run_in_threadpool(
            service.control,
            context,
            canonical_uuid(skill_id),
            action,
            _revision(body["expectedRevision"]),
            key,
            release_sha256=release,
            setup_token=token,
        )
        return response(result)
