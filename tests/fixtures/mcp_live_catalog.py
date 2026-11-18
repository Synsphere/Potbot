"""Raw MCP protocol fixture for collector and durable-owner acceptance tests.

This returns peer frames, never signed or preconstructed Potbot observations.
It is not a qualified integration or a production server.
"""

from __future__ import annotations

import copy
from typing import Any


class RawCatalogPeer:
    def __init__(self, *, variant: str = "normal", era: str = "modern") -> None:
        self.variant, self.era = variant, era
        self.calls: list[dict[str, Any]] = []
        self.client: Any = None

    async def exchange(self, request: dict[str, Any], expect_reply: bool) -> dict[str, Any] | None:
        self.calls.append(copy.deepcopy(request))
        method = request["method"]
        if method == "notifications/initialized":
            assert not expect_reply
            return None
        assert expect_reply
        if method == "server/discover" and self.era == "legacy":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32601, "message": "fixture legacy peer"},
            }
        capabilities = {
            "tools": {"listChanged": True},
            "resources": {"listChanged": True, "subscribe": True},
            "prompts": {"listChanged": True},
        }
        if self.variant == "capabilities_absent":
            capabilities = {}
        if method in {"server/discover", "initialize"}:
            info = {"name": "fixture-live-catalog", "version": "1"}
            result = {"capabilities": capabilities, "ttlMs": 90_000}
            if method == "server/discover":
                result.update(
                    resultType="complete",
                    supportedVersions=["2026-07-28"],
                    _meta={"io.modelcontextprotocol/serverInfo": info},
                )
            else:
                result.update(protocolVersion="2025-11-25", serverInfo=info)
            return {"jsonrpc": "2.0", "id": request["id"], "result": result}
        collections = {
            "tools/list": "tools",
            "resources/list": "resources",
            "resources/templates/list": "resourceTemplates",
            "prompts/list": "prompts",
        }
        assert method in collections, (
            "Metadata fixture received an invocation or unapproved request"
        )
        collection = collections[method]
        tools = [
            {
                "name": "search",
                "description": "Search fixture documents",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True},
            },
            {"name": "summarize", "inputSchema": {"type": "object"}},
        ]
        if self.variant == "changed_schema":
            tools[0]["inputSchema"]["properties"]["query"]["type"] = "integer"
        if self.variant == "removed_tool":
            tools = tools[1:]
        inventories = {
            "tools": tools,
            "resources": [
                {
                    "name": "document",
                    "uri": "fixture://documents/1",
                    "mimeType": "text/plain",
                    "size": 12,
                }
            ],
            "resourceTemplates": [{"name": "documents", "uriTemplate": "fixture://documents/{id}"}],
            "prompts": [{"name": "review", "arguments": [{"name": "text", "required": True}]}],
        }
        values = inventories[collection]
        if self.variant == "empty_collections":
            values = []
        cursor = request.get("params", {}).get("cursor")
        if self.variant == "notification" and method == "prompts/list":
            assert self.client is not None
            self.client.observe_notification(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            )
        if self.variant == "duplicate_identity" and method == "tools/list":
            values = [tools[0], tools[0]]
        if self.variant == "reverse_order":
            values = list(reversed(values))
        offset = int(cursor) if cursor is not None else 0
        page = values[offset : offset + 1]
        result = {"resultType": "complete", collection: page, "ttlMs": 60_000}
        if offset + 1 < len(values):
            result["nextCursor"] = str(offset + 1)
        if self.variant == "cursor_loop" and method == "tools/list":
            result[collection] = []
            result["nextCursor"] = "1"
        if self.variant == "missing_collection" and method == "prompts/list":
            result.pop(collection)
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}
