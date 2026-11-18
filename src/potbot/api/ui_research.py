"""Fixed operator HTTP surface for workspace research and cited evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.api.ui_discovery import _sha, _uuid
from potbot.api.ui_operation_workbench import _integer
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.research_evidence import citation_spec, claim_specs
from potbot.domain.serialization import canonical_json
from potbot.research.contracts import ResearchQuery

if TYPE_CHECKING:
    from fastapi import FastAPI

    from potbot.persistence.database import Database

MAX_BODY_BYTES = 131_072
MAX_RESPONSE_BYTES = 1_500_000
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Origin",
}


async def _body(request: Request, *, confirm: bool = True) -> dict[str, Any]:
    if (
        len(request.headers.getlist("content-type")) != 1
        or request.headers["content-type"].split(";", 1)[0] != "application/json"
    ):
        raise InvalidInputError("Research controls require one JSON content type")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise InvalidInputError("Research request exceeds its byte allowance")

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
        value = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant
        )
        canonical_json(value).encode("utf-8", "strict")
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidInputError("Research input must be unambiguous finite JSON") from None
    if not isinstance(value, dict) or (confirm and value.get("confirm") is not True):
        raise InvalidInputError("Research controls require an exact confirmed object")
    return value


def register_research_routes(
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
        from potbot.controller.research_workbench import ResearchWorkbench

        service = ResearchWorkbench(database)

    def actor(request: Request, *, mutation: bool = False) -> WorkspaceContext:
        if len(request.headers.getlist("authorization")) != 1 or any(
            len(request.headers.getlist(name)) > 1
            for name in ("origin", "x-potbot-setup-token", "x-potbot-expected-principal")
        ):
            raise ScopeDeniedError("Research authorization is missing or ambiguous")
        scope = authenticate(request.headers.get("authorization"))
        if scope.workspace_id != workspace_id:
            raise ScopeDeniedError("Research belongs to another workspace")
        expected = request.headers.getlist("x-potbot-expected-principal")
        if (mutation and len(expected) != 1) or (
            expected and expected[0] != str(scope.principal_id)
        ):
            # The BFF's configured bearer can rotate while a tab still retains
            # an original command. Fence its reviewed principal before effects,
            # rather than detecting the different scope only in the response.
            raise HTTPException(
                status_code=403,
                detail="The authenticated research scope changed; reopen it before another command.",
                headers=_HEADERS,
            )
        origins = request.headers.getlist("origin")
        if (origins and origins[0] not in allowed_origins) or (mutation and len(origins) != 1):
            raise ScopeDeniedError("Research controls require an exact allowed origin")
        return scope

    def query(request: Request, allowed: set[str] | None = None) -> dict[str, str]:
        pairs = request.query_params.multi_items()
        values = dict(pairs)
        if (
            len(request.scope.get("query_string", b"")) > 4096
            or len(pairs) != len(values)
            or set(values) - (allowed or set())
        ):
            raise InvalidInputError("Research query is invalid or ambiguous")
        return values

    def reply(value: dict[str, Any], *, queued: bool = False) -> JSONResponse:
        if len(canonical_json(value).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise InvalidInputError("Research response exceeds its byte allowance")
        return JSONResponse(value, headers=_HEADERS, status_code=202 if queued else 200)

    async def command(
        request: Request, *, setup: bool = True
    ) -> tuple[WorkspaceContext, dict[str, Any], str, str | None]:
        scope = actor(request, mutation=True)
        query(request)
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or not _KEY.fullmatch(keys[0]):
            raise InvalidInputError("Research commands require one stable key")
        request.state.idempotency_key = keys[0]
        token = None
        if setup:
            if authorize_setup is None:
                raise CapabilityUnavailableError("Research writes require operator setup")
            if len(request.headers.getlist("x-potbot-setup-token")) != 1:
                raise ScopeDeniedError("Research writes require one operator capability")
            token = request.headers["x-potbot-setup-token"]
            authorize_setup(token)
        return scope, await _body(request), keys[0], token

    base = "/v1/ui/research"

    @app.get(base + "/context")
    async def context(request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"episodeId", "repositoryStateId", "needId"})
        if not {"episodeId", "repositoryStateId"}.issubset(values):
            raise InvalidInputError("Research context requires a task and repository state")
        return reply(
            await run_in_threadpool(
                service.context,
                scope,
                _uuid(values["episodeId"]),
                _uuid(values["repositoryStateId"]),
                _uuid(values["needId"]) if "needId" in values else None,
            )
        )

    @app.post(base + "/needs")
    async def create_need(request: Request) -> JSONResponse:
        from potbot.controller.research_workbench import parse_need_spec

        scope, value, key, token = await command(request)
        if set(value) != {"episodeId", "repositoryStateId", "spec", "confirm"}:
            raise InvalidInputError("Research need creation fields are invalid")
        return reply(
            await run_in_threadpool(
                service.create_need,
                scope,
                _uuid(value["episodeId"]),
                _uuid(value["repositoryStateId"]),
                parse_need_spec(value["spec"]).to_dict(),
                key,
                setup_token=token,
            )
        )

    @app.post(base + "/actions")
    async def admit(request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        if set(value) != {
            "episodeId",
            "repositoryStateId",
            "needId",
            "contextSha256",
            "query",
            "confirm",
        }:
            raise InvalidInputError("Research admission fields are invalid")
        return reply(
            await run_in_threadpool(
                service.admit,
                scope,
                _uuid(value["episodeId"]),
                _uuid(value["repositoryStateId"]),
                _uuid(value["needId"]),
                _sha(value["contextSha256"]),
                ResearchQuery.from_dict(value["query"]).to_dict(),
                key,
                setup_token=token,
            )
        )

    @app.post(base + "/runs")
    async def queue_run(request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        if (
            set(value) != {"actionId", "policySha256", "disclosureConfirmed", "confirm"}
            or value["disclosureConfirmed"] is not True
        ):
            raise InvalidInputError(
                "Research run requires the reviewed action, policy and disclosure"
            )
        return reply(
            await run_in_threadpool(
                service.queue,
                scope,
                _uuid(value["actionId"]),
                _sha(value["policySha256"]),
                key,
                setup_token=token,
                disclosure_confirmed=True,
            ),
            queued=True,
        )

    @app.get(base + "/runs")
    async def runs(request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"episodeId", "limit", "after"})
        after = values.get("after")
        if after is not None and (
            not after
            or len(after) > 512
            or any(ord(char) < 33 or ord(char) > 126 for char in after)
        ):
            raise InvalidInputError("Research history cursor is invalid")
        return reply(
            await run_in_threadpool(
                service.list_runs,
                scope,
                episode_id=_uuid(values["episodeId"]) if "episodeId" in values else None,
                limit=_integer(values.get("limit", "25"), minimum=1, maximum=25),
                after=after,
            )
        )

    @app.get(base + "/runs/{run_id}")
    async def inspect(run_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request)
        return reply(await run_in_threadpool(service.inspect, scope, _uuid(run_id)))

    @app.get(base + "/runs/{run_id}/events")
    async def events(run_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        values = query(request, {"after", "limit"})
        return reply(
            await run_in_threadpool(
                service.events,
                scope,
                _uuid(run_id),
                after=_integer(values.get("after", "0"), maximum=2**53 - 1),
                limit=_integer(values.get("limit", "100"), minimum=1, maximum=100),
            )
        )

    @app.get(base + "/runs/{run_id}/result")
    async def result(run_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request)
        return reply(await run_in_threadpool(service.result, scope, _uuid(run_id)))

    @app.get(base + "/runs/{run_id}/sources/{source_id}")
    async def source(run_id: str, source_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request)
        return reply(await run_in_threadpool(service.source, scope, _uuid(run_id), _sha(source_id)))

    @app.post(base + "/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request) -> JSONResponse:
        scope, value, key, _ = await command(request, setup=False)
        if set(value) != {"confirm"}:
            raise InvalidInputError("Research cancellation has unexpected fields")
        return reply(await run_in_threadpool(service.cancel, scope, _uuid(run_id), key))

    @app.post(base + "/citations")
    async def citation(request: Request) -> JSONResponse:
        scope = actor(request, mutation=True)
        query(request)
        value = citation_spec(await _body(request, confirm=False))
        return reply(await run_in_threadpool(service.citation, scope, value))

    @app.post(base + "/comparisons")
    async def compare(request: Request) -> JSONResponse:
        scope, value, key, token = await command(request)
        if set(value) != {"episodeId", "needId", "contextSha256", "claims", "confirm"}:
            raise InvalidInputError("Research comparison fields are invalid")
        return reply(
            await run_in_threadpool(
                service.compare,
                scope,
                _uuid(value["episodeId"]),
                _uuid(value["needId"]),
                _sha(value["contextSha256"]),
                claim_specs(value["claims"]),
                key,
                setup_token=token,
            )
        )

    @app.get(base + "/comparisons/{evidence_id}")
    async def comparison(evidence_id: str, request: Request) -> JSONResponse:
        scope = actor(request)
        query(request)
        return reply(await run_in_threadpool(service.comparison, scope, _uuid(evidence_id)))
