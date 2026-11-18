"""Retained qualification DTO reads and exact browser decision boundaries."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_mcp_qualification import register_mcp_qualification_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError

BASE = "/v1/ui/mcp-qualification"


@pytest.fixture
def api():
    context, foreign = WorkspaceContext(uuid4(), uuid4()), WorkspaceContext(uuid4(), uuid4())
    calls = []
    token = "private-qualification-setup-canary-" + "x" * 40
    allowed = {"suites", "suite", "attempts", "attempt", "report", "request", "control", "cancel"}

    class Service:
        def __getattr__(self, name):
            assert name in allowed, "The browser must not address any native execution owner"

            def invoke(actor, *args, **kwargs):
                assert actor == context
                calls.append((name, args, kwargs))
                return {
                    "schema": "potbot-mcp-qualification/v1",
                    "workspaceId": str(actor.workspace_id),
                    "principalId": str(actor.principal_id),
                    "items": [],
                    "nextCursor": None,
                }

            return invoke

    def authenticate(value):
        if value == "Bearer ordinary":
            return context
        if value == "Bearer foreign":
            return foreign
        raise ScopeDeniedError("private authentication failure canary")

    def authorize(value):
        if value != token:
            raise ScopeDeniedError("private setup failure canary")

    def client(*, setup=True):
        app = FastAPI()

        @app.exception_handler(DomainError)
        async def failure(request, error):
            response = error_response(
                error,
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=hasattr(request.state, "idempotency_key"),
                ),
            )
            return JSONResponse(response.body, status_code=response.status)

        register_mcp_qualification_routes(
            app,
            None,
            authenticate,
            ("https://operator.example",),
            context.workspace_id,
            authorize if setup else None,
            service=Service(),
        )
        return TestClient(app)

    return SimpleNamespace(
        context=context,
        calls=calls,
        client=client,
        token=token,
        headers={
            "Authorization": "Bearer ordinary",
            "Origin": "https://operator.example",
            "X-Potbot-Setup-Token": token,
            "Idempotency-Key": "reviewed-qualification-command",
        },
    )


@pytest.mark.parametrize(
    "path,method",
    [
        ("suites", "suites"),
        ("suites/{id}", "suite"),
        ("attempts", "attempts"),
        ("attempts/{id}", "attempt"),
        ("reports/{id}", "report"),
    ],
)
def test_retained_reads_need_no_setup_and_route_to_one_read_owner(api, path, method):
    identity = uuid4()
    response = api.client(setup=False).get(
        f"{BASE}/{path.format(id=identity)}", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["vary"] == "Authorization, Origin"
    assert [call[0] for call in api.calls] == [method]
    assert response.json()["schema"] == "potbot-mcp-qualification/v1"
    assert api.token not in response.text


@pytest.mark.parametrize(
    "collection,filter_name,owner_name",
    [("suites", "bindingId", "binding_id"), ("attempts", "suiteId", "suite_id")],
)
def test_exact_scope_filter_and_cursor_reach_only_retained_read_owner(
    api, collection, filter_name, owner_name
):
    identity, cursor = uuid4(), uuid4()
    response = api.client().get(
        f"{BASE}/{collection}",
        params={filter_name: str(identity), "limit": "12", "after": str(cursor)},
        headers={"Authorization": "Bearer ordinary"},
    )
    assert response.status_code == 200
    assert api.calls == [
        (collection, (), {owner_name: identity, "limit": 12, "after": str(cursor)})
    ]


@pytest.mark.parametrize(
    "action,owner",
    [
        ("qualify", "request"),
        ("enable", "control"),
        ("disable", "control"),
        ("revoke", "control"),
        ("cancel", "cancel"),
    ],
)
def test_browser_decisions_require_exact_revision_digest_and_command_identity(api, action, owner):
    identity = uuid4()
    collection = "attempts" if action == "cancel" else "suites"
    response = api.client().post(
        f"{BASE}/{collection}/{identity}/{action}",
        json={"revision": 3, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 200
    assert len(api.calls) == 1
    name, args, options = api.calls[0]
    assert name == owner and args[0] == identity
    assert args[-3:] == (3, "a" * 64, "reviewed-qualification-command")
    assert options == {"setup_token": api.token}
    assert api.token not in response.text


@pytest.mark.parametrize(
    "name,value,status",
    [
        ("Authorization", None, 404),
        ("Authorization", "Bearer foreign", 404),
        ("Authorization", "Bearer unknown", 404),
        ("Origin", None, 404),
        ("Origin", "null", 404),
        ("Origin", "https://attacker.example", 404),
        ("X-Potbot-Setup-Token", None, 400),
        ("X-Potbot-Setup-Token", "ordinary", 404),
        ("Idempotency-Key", None, 400),
        ("Idempotency-Key", "", 400),
        ("Idempotency-Key", "space is ambiguous", 400),
        ("Idempotency-Key", "x" * 129, 400),
    ],
)
def test_mutations_require_independent_matching_authorities(api, name, value, status):
    headers = dict(api.headers)
    if value is None:
        headers.pop(name)
    else:
        headers[name] = value
    response = api.client().post(
        f"{BASE}/suites/{uuid4()}/qualify",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=headers,
    )
    assert response.status_code == status and not api.calls
    assert "canary" not in response.text and api.token not in response.text


@pytest.mark.parametrize(
    "header", ["Authorization", "Origin", "X-Potbot-Setup-Token", "Idempotency-Key"]
)
def test_duplicate_authority_headers_never_reach_an_owner(api, header):
    response = api.client().post(
        f"{BASE}/suites/{uuid4()}/qualify",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=[*api.headers.items(), (header, api.headers[header])],
    )
    assert response.status_code in {400, 404} and not api.calls


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"revision": True, "digest": "a" * 64, "confirm": True},
        {"revision": 0, "digest": "a" * 64, "confirm": True},
        {"revision": -1, "digest": "a" * 64, "confirm": True},
        {"revision": 2**53, "digest": "a" * 64, "confirm": True},
        {"revision": 1.0, "digest": "a" * 64, "confirm": True},
        {"revision": 1, "digest": "A" * 64, "confirm": True},
        {"revision": 1, "digest": None, "confirm": True},
        {"revision": 1, "digest": "a" * 64, "confirm": False},
        {"revision": 1, "digest": "a" * 64, "confirm": 1},
        {"revision": 1, "digest": "a" * 64, "confirm": True, "profile": "/operator/private.json"},
        {"revision": 1, "digest": "a" * 64, "confirm": True, "passed": True},
    ],
)
def test_commands_are_exact_decisions_without_private_files_or_uploaded_success(api, body):
    response = api.client().post(
        f"{BASE}/attempts/{uuid4()}/cancel", json=body, headers=api.headers
    )
    assert response.status_code == 400 and not api.calls
    assert "/operator/private.json" not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        b'{"revision":1,"revision":2,"digest":"' + b"a" * 64 + b'","confirm":true}',
        b'{"revision":NaN,"digest":"' + b"a" * 64 + b'","confirm":true}',
        b" " * 8193,
        b"[1]",
        b"\xff",
    ],
)
def test_ambiguous_or_unbounded_json_cannot_become_a_command(api, raw):
    response = api.client().post(
        f"{BASE}/suites/{uuid4()}/qualify",
        content=raw,
        headers={**api.headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "limit=01",
        "limit=-1",
        "limit=true",
        "limit=1&limit=2",
        "after=not-a-uuid",
        "bindingId=not-a-uuid",
        "suiteId=wrong-collection",
        "profile=/operator/private.json",
        "q=" + "x" * 4097,
    ],
)
def test_queries_are_bounded_unambiguous_and_cannot_select_native_configuration(api, query):
    response = api.client().get(
        f"{BASE}/suites?{query}", headers={"Authorization": "Bearer ordinary"}
    )
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize("collection", ["suites", "attempts", "reports"])
def test_detail_routes_reject_extra_query_or_noncanonical_identity(api, collection):
    for target in (f"{uuid4()}?limit=1", str(uuid4()).upper(), "not-a-uuid"):
        response = api.client().get(
            f"{BASE}/{collection}/{target}", headers={"Authorization": "Bearer ordinary"}
        )
        assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "collection,action",
    [
        ("suites", "admit"),
        ("suites", "run"),
        ("suites", "recover"),
        ("suites", "install"),
        ("attempts", "run"),
        ("attempts", "recover"),
        ("reports", "complete"),
    ],
)
def test_native_dispatch_and_receipt_recovery_are_not_browser_operations(api, collection, action):
    response = api.client().post(
        f"{BASE}/{collection}/{uuid4()}/{action}",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code in {400, 404} and not api.calls


def test_query_fields_cannot_modify_a_valid_command(api):
    response = api.client().post(
        f"{BASE}/suites/{uuid4()}/qualify?profile=/operator/private.json",
        json={"revision": 1, "digest": "a" * 64, "confirm": True},
        headers=api.headers,
    )
    assert response.status_code == 400 and not api.calls


def test_non_json_controls_and_disabled_setup_never_reach_owner(api):
    body = {"revision": 1, "digest": "a" * 64, "confirm": True}
    response = api.client(setup=False).post(
        f"{BASE}/suites/{uuid4()}/qualify", json=body, headers=api.headers
    )
    assert response.status_code == 503 and not api.calls
    response = api.client().post(f"{BASE}/suites/{uuid4()}/qualify", data=body, headers=api.headers)
    assert response.status_code == 400 and not api.calls


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer foreign"},
        {"Authorization": "Bearer ordinary", "Origin": "https://attacker.example"},
        [("Authorization", "Bearer ordinary"), ("Authorization", "Bearer ordinary")],
    ],
)
def test_retained_evidence_never_crosses_workspace_or_ambiguous_authentication(api, headers):
    response = api.client().get(f"{BASE}/suites", headers=headers)
    assert response.status_code == 404 and not api.calls
    assert "canary" not in response.text
