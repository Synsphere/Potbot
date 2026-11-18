"""Live MCP metadata collection through real protocol and supervised fixture owners."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import CapabilityContext, ToolDefinition, ToolRegistry
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.mcp_discovery import McpDiscoveryCatalog, bounded_metadata, catalog_item
from potbot.tools.mcp_http import McpHttpTransport
from potbot.tools.mcp_protocol import MODERN, TOOLS_LISTEN, McpClient, McpProtocolError
from potbot.tools.mcp_sandbox import ReadOnlyMcpGrant, SandboxedMcpClient
from potbot.workspace_sandbox import WorkspaceSandbox

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RawCatalogPeer = runpy.run_path(str(FIXTURES / "mcp_live_catalog.py"))["RawCatalogPeer"]
URL = "https://mcp.fixture.example/mcp"


def collect(*, variant="normal", era="modern"):
    peer = RawCatalogPeer(variant=variant, era=era)
    client = McpClient(peer.exchange)
    peer.client = client
    catalog = asyncio.run(client.discover_catalog())
    return catalog, peer, client


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_collects_all_advertised_interfaces_without_invoking_any(era):
    catalog, peer, client = collect(era=era)
    value = catalog.to_dict()
    assert value["protocolVersion"] == (MODERN if era == "modern" else "2025-11-25")
    assert value["serverInfo"] == {"name": "fixture-live-catalog", "version": "1"}
    assert set(value["collections"].values()) == {"observed"}
    assert value["notificationSupport"] == {"tools": True, "resources": True, "prompts": True}
    assert value["cacheTtlMs"] == 60_000 and len(value["items"]) == 5
    assert all(item["invocable"] is False for item in value["items"])
    assert set(item["kind"] for item in value["items"]) == {
        "tool",
        "resource",
        "resource_template",
        "prompt",
    }
    assert set(call["method"] for call in peer.calls) <= {
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "prompts/list",
    }
    assert len([call for call in peer.calls if call["method"] == "tools/list"]) == 2
    assert client.catalog is None and client.discovery_catalog == catalog
    assert McpDiscoveryCatalog.from_dict(value).fingerprint == catalog.fingerprint
    value["items"][0]["definition"]["name"] = "modified-return-value"
    assert catalog.to_dict()["items"][0]["definition"]["name"] != "modified-return-value"


def test_absent_capabilities_are_distinct_from_observed_empty_collections():
    absent, peer, _ = collect(variant="capabilities_absent")
    assert len(peer.calls) == 1
    assert set(absent.to_dict()["collections"].values()) == {"not_advertised"}
    assert absent.to_dict()["items"] == []
    empty, peer, _ = collect(variant="empty_collections")
    assert len(peer.calls) == 5
    assert set(empty.to_dict()["collections"].values()) == {"observed"}
    assert empty.to_dict()["items"] == []
    assert absent.fingerprint != empty.fingerprint


def test_order_does_not_change_identity_but_schema_and_removal_do():
    original, _, _ = collect()
    reordered, _, _ = collect(variant="reverse_order")
    changed, _, _ = collect(variant="changed_schema")
    removed, _, _ = collect(variant="removed_tool")
    assert original.fingerprint == reordered.fingerprint
    assert len({original.fingerprint, changed.fingerprint, removed.fingerprint}) == 3
    search = next(item for item in original.to_dict()["items"] if item["identity"] == "search")
    assert search["definition"]["annotations"] == {"readOnlyHint": True}
    assert search["invocable"] is False


@pytest.mark.parametrize(
    "variant", ["cursor_loop", "duplicate_identity", "missing_collection", "notification"]
)
def test_incomplete_or_changed_collection_cannot_replace_a_retained_catalog(variant):
    peer = RawCatalogPeer()
    client = McpClient(peer.exchange)
    peer.client = client

    async def scenario():
        assert await client.discover_catalog()
        peer.variant = variant
        with pytest.raises((McpProtocolError, StateMismatchError)):
            await client.discover_catalog()
        assert client.discovery_catalog is None and client.catalog is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema="unrecognized"),
        lambda value: value.update(protocolVersion=[]),
        lambda value: value.update(protocolVersion="2099-01-01"),
        lambda value: value.update(serverInfo={"name": "missing-version"}),
        lambda value: value.update(capabilities={"tools": True}),
        lambda value: value.update(capabilities={"tools": {"listChanged": 1}}),
        lambda value: value["collections"].update(tools="partial"),
        lambda value: value["notificationSupport"].update(tools=1),
        lambda value: value.update(cacheTtlMs=True),
        lambda value: value.update(cacheTtlMs=-1),
        lambda value: value.update(cacheTtlMs=2**53),
        lambda value: value["items"][0].update(kind=[]),
        lambda value: value["items"][0].update(invocable=True),
        lambda value: value["items"][0].update(definitionSha256="a" * 64),
        lambda value: value["items"].append(copy.deepcopy(value["items"][0])),
        lambda value: value["items"][0]["definition"].update(name="unbound-change"),
        lambda value: value.update(extra="not-in-contract"),
    ],
)
def test_retained_catalog_parser_rejects_inconsistent_or_unbounded_claims(mutate):
    value = collect()[0].to_dict()
    mutate(value)
    with pytest.raises(McpProtocolError):
        McpDiscoveryCatalog.from_dict(value)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("resource", {"name": "x", "uri": "relative/path"}),
        ("resource", {"name": "x", "uri": "fixture://x", "size": True}),
        ("resource", {"name": "x", "uri": "fixture://x\n"}),
        ("resource", {"name": "x\n", "uri": "fixture://x"}),
        ("resource_template", {"name": "x", "uriTemplate": "fixture://{x}", "mimeType": []}),
        ("prompt", {"name": "x", "arguments": [{"name": "a", "required": 1}]}),
        ("prompt", {"name": "x", "arguments": [{"name": "a"}, {"name": "a"}]}),
        ("prompt", {"name": "x", "arguments": [{"name": "a"}] * 129}),
        ("prompt", {"name": "x", "description": "a" * 4097}),
        ("prompt", {"name": "x\t"}),
        ("prompt", {"name": "x", "arguments": [{"name": "a\n"}]}),
        ("prompt", {"name": "x", "description": "\ud800"}),
        (
            "tool",
            {
                "name": "x",
                "inputSchema": {"type": "object", "$ref": "https://untrusted.invalid/schema"},
            },
        ),
        ("tool", {"name": "x", "inputSchema": {"type": "array"}}),
        ("tool", {"name": "x", "inputSchema": {"type": "object"}, "_meta": "a" * 131_072}),
    ],
)
def test_metadata_field_and_schema_boundaries(kind, value):
    with pytest.raises(McpProtocolError):
        catalog_item(kind, value)


def test_metadata_structural_bounds_cover_detached_arrays_and_multiline_descriptions():
    description = "First line\n\tIndented second line"
    assert (
        catalog_item("prompt", {"name": "prompt", "description": description})["description"]
        == description
    )
    nested = "leaf"
    for _ in range(34):
        nested = (nested,)
    for value in (nested, [None] * 8193, {"a" * 513: "key"}, {"value": float("nan")}):
        with pytest.raises(McpProtocolError):
            bounded_metadata(value)


@pytest.mark.parametrize(
    "fault",
    ["count", "bytes", "pages", "cursor_type", "cursor_empty", "cursor_long", "interactive"],
)
def test_live_collection_allowances_are_enforced_before_publication(fault):
    peer = RawCatalogPeer()

    async def exchange(request, expected):
        reply = await peer.exchange(request, expected)
        if request["method"] == "tools/list":
            page = reply["result"]
            if fault == "count":
                page["tools"] = [
                    {"name": f"tool-{n}", "inputSchema": {"type": "object"}} for n in range(257)
                ]
                page.pop("nextCursor", None)
            elif fault == "bytes":
                page["tools"] = [
                    {"name": f"tool-{n}", "inputSchema": {"type": "object"}, "_meta": "a" * 90_000}
                    for n in range(24)
                ]
                page.pop("nextCursor", None)
            elif fault == "pages":
                page["tools"] = []
                page["nextCursor"] = str(request["id"])
            elif fault.startswith("cursor_"):
                page["nextCursor"] = {
                    "cursor_type": False,
                    "cursor_empty": "",
                    "cursor_long": "a" * 2049,
                }[fault]
            else:
                page["resultType"] = "input_required"
        return reply

    client = McpClient(exchange)
    with pytest.raises(McpProtocolError):
        asyncio.run(client.discover_catalog())
    assert client.discovery_catalog is None


def test_http_collects_metadata_with_existing_fixed_transport_headers():
    peer = RawCatalogPeer()
    seen = []

    async def respond(request):
        body = json.loads(request.content)
        seen.append(request)
        assert request.headers["MCP-Protocol-Version"] == MODERN
        assert request.headers["Mcp-Method"] == body["method"]
        assert request.headers["Authorization"] == "Bearer fixture-only-secret"
        return httpx.Response(200, json=await peer.exchange(body, True))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            transport = McpHttpTransport(
                URL,
                http,
                network_approval=lambda url: None,
                bearer_supplier=lambda: "fixture-only-secret",
            )
            client = McpClient(transport.exchange, http_transport=transport)
            result = await client.discover_catalog()
            assert len(result.to_dict()["items"]) == 5

    asyncio.run(scenario())
    assert len(seen) == 6 and all(request.url == URL for request in seen)


@pytest.mark.parametrize("after_reply", [False, True])
def test_http_in_band_change_before_or_after_reply_invalidates_collection(after_reply):
    peer = RawCatalogPeer()

    async def respond(request):
        body = json.loads(request.content)
        reply = await peer.exchange(body, True)
        if body["method"] == "prompts/list":
            changed = {"jsonrpc": "2.0", "method": "notifications/resources/list_changed"}
            frames = [reply, changed] if after_reply else [changed, reply]
            wire = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=wire)
        return httpx.Response(200, json=reply)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            transport = McpHttpTransport(URL, http, network_approval=lambda url: None)
            client = McpClient(transport.exchange, http_transport=transport)
            with pytest.raises(StateMismatchError):
                await client.discover_catalog()
            assert client.discovery_catalog is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", [None, "partial_ack", "wrong_identity", "unrequested", "graceful"]
)
def test_modern_watch_observes_declared_collections_and_correlated_completion(fault):
    peer = RawCatalogPeer()
    observed = []

    async def respond(request):
        body = json.loads(request.content)
        if body["method"] != TOOLS_LISTEN:
            return httpx.Response(200, json=await peer.exchange(body, True))
        meta = {"io.modelcontextprotocol/subscriptionId": body["id"]}
        filters = body["params"]["notifications"]
        assert filters == {
            "toolsListChanged": True,
            "resourcesListChanged": True,
            "promptsListChanged": True,
        }
        acknowledged = dict(filters)
        if fault == "partial_ack":
            acknowledged.pop("promptsListChanged")
        frames = [
            {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": meta, "notifications": acknowledged},
            }
        ]
        if fault == "graceful":
            frames.append(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"resultType": "complete", "_meta": meta},
                }
            )
        else:
            if fault == "wrong_identity":
                meta = {"io.modelcontextprotocol/subscriptionId": body["id"] + 1}
            frames.append(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/updated"
                    if fault == "unrequested"
                    else "notifications/prompts/list_changed",
                    "params": {"_meta": meta},
                }
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            transport = McpHttpTransport(URL, http, network_approval=lambda url: None)

            async def exchange(request, reply):
                if request["method"] == TOOLS_LISTEN:
                    return await transport.listen_catalog(request, window_ms=250)
                return await transport.exchange(request, reply)

            client = McpClient(exchange, http_transport=transport)
            await client.discover_catalog()
            if fault in {"partial_ack", "wrong_identity", "unrequested"}:
                with pytest.raises(McpProtocolError):
                    await client.watch_catalog()
            else:
                observed.extend(await client.watch_catalog())
            assert client.discovery_catalog is None

    asyncio.run(scenario())
    if fault is None:
        assert observed == ["prompts"]
    elif fault == "graceful":
        assert observed == []


class UnitFixtureBackend:
    def prepare(self, *, argv, status_fd, **_):
        return SimpleNamespace(
            argv=argv, pass_fds=(status_fd,), identity="unit-fixture-NOT-isolation"
        )


def sandbox_session(tmp_path, *, variant="normal", authorize=lambda: None):
    root = tmp_path / "registered"
    candidate = root / "candidates" / "discovery"
    candidate.mkdir(parents=True)
    definition = ToolDefinition(
        name="mcp-fixture",
        capability="local-mcp",
        argv_prefix=(
            sys.executable,
            "-u",
            str(FIXTURES / "mcp_live_stdio_server.py"),
            "--variant",
            variant,
        ),
        mutates=False,
        accepts_stdin=True,
        timeout_ms=5000,
    )
    return SandboxedMcpClient(
        supervisor=InteractiveProcessSupervisor(UnitFixtureBackend()),
        sandbox=WorkspaceSandbox(root),
        candidate_root=candidate,
        server_kind=definition.name,
        registry=ToolRegistry((definition,)),
        capabilities=CapabilityContext(()),
        task_contract={},
        executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        permitted=(),
        approve_call=lambda *_: pytest.fail("Metadata cannot request a call grant"),
        discovery_only=True,
        authorize_discovery=authorize,
    )


def test_metadata_only_supervised_lifetime_has_no_task_grant_or_invocation_path(tmp_path):
    admissions = []
    session = sandbox_session(tmp_path, authorize=lambda: admissions.append("current"))

    async def scenario():
        async with session:
            catalog = await session.discover_catalog()
            assert len(catalog.to_dict()["items"]) == 5
            for method in ("tools/call", "resources/read", "prompts/get"):
                with pytest.raises(ScopeDeniedError):
                    await session._exchange({"jsonrpc": "2.0", "id": 200, "method": method}, True)
            with pytest.raises(ScopeDeniedError):
                await session.call("search", {"query": "cannot execute"})
            assert session.completion is None
        assert session.completion is not None and session.completion.processes_stopped
        assert await session.close() == session.completion
        assert len(admissions) >= 16

    asyncio.run(scenario())


def test_stdio_notification_and_revoked_admission_close_the_supervised_lifetime(tmp_path):
    session = sandbox_session(tmp_path, variant="notification")

    async def scenario():
        async with session:
            with pytest.raises(StateMismatchError):
                await session.discover_catalog()
        assert session.completion is not None and session.completion.processes_stopped

    asyncio.run(scenario())


def test_discovery_authority_is_rechecked_before_every_exchange(tmp_path):
    admitted = True

    def authorize():
        if not admitted:
            raise ScopeDeniedError("fixture admission revoked")

    session = sandbox_session(tmp_path, authorize=authorize)

    async def scenario():
        nonlocal admitted
        async with session:
            admitted = False
            with pytest.raises(ScopeDeniedError):
                await session.discover_catalog()
        assert session.completion is not None and session.completion.processes_stopped

    asyncio.run(scenario())


def test_discovery_mode_rejects_call_grants_and_nonthrowing_authority(tmp_path):
    session = sandbox_session(tmp_path)
    session._authorize_discovery = lambda: True
    with pytest.raises(ScopeDeniedError):
        asyncio.run(session.__aenter__())
    assert session._process is None
    session._grants = {"search": ReadOnlyMcpGrant("search")}
    with pytest.raises(ScopeDeniedError):
        asyncio.run(session.call("search", {}))


def test_actual_stop_receipt_survives_a_lease_finalization_failure(tmp_path):
    session = sandbox_session(tmp_path)

    def fail_finalization(result):
        assert result.processes_stopped
        raise StateMismatchError("fixture lease finalization unavailable")

    async def scenario():
        await session.__aenter__()
        session._finish_lease = fail_finalization
        with pytest.raises(StateMismatchError):
            await session.close()
        assert session.completion is not None and session.completion.processes_stopped
        assert await session.close() == session.completion

    asyncio.run(scenario())


def test_subscription_window_requires_explicit_bound():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("No network"))
        ) as http:
            transport = McpHttpTransport(URL, http, network_approval=lambda url: None)
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": TOOLS_LISTEN,
                "params": {
                    "notifications": {"toolsListChanged": True},
                    "_meta": {"io.modelcontextprotocol/protocolVersion": MODERN},
                },
            }
            for value in (0, 249, 5001, True):
                with pytest.raises(InvalidInputError):
                    await transport.listen_catalog(request, window_ms=value)

    asyncio.run(scenario())
