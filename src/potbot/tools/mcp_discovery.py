"""Bounded immutable observations of the interfaces a live MCP server declares.

Definitions and server capabilities remain peer claims. This module performs
no resource reads, prompt expansion, URI fetching, tool calls or admission.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.mcp_protocol import MODERN, SUPPORTED_LEGACY, McpProtocolError, McpToolInfo

SCHEMA = "potbot-mcp-live-catalog/v1"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_DEFINITION_BYTES = 128 * 1024
MAX_COLLECTION_ITEMS = 256
MAX_CATALOG_ITEMS = 4 * MAX_COLLECTION_ITEMS
COLLECTIONS = {
    "tools": ("tools", "tools/list", "tool"),
    "resources": ("resources", "resources/list", "resource"),
    "resourceTemplates": ("resources", "resources/templates/list", "resource_template"),
    "prompts": ("prompts", "prompts/list", "prompt"),
}
NOTIFICATION_COLLECTIONS = {
    "notifications/tools/list_changed": "tools",
    "notifications/resources/list_changed": "resources",
    "notifications/prompts/list_changed": "prompts",
    "notifications/resources/updated": "resources",
}
SUBSCRIPTION_FILTERS = {
    "tools": "toolsListChanged",
    "resources": "resourcesListChanged",
    "prompts": "promptsListChanged",
}


def _text(
    value: Any, label: str, maximum: int, *, empty: bool = False, multiline: bool = False
) -> str:
    if (
        not isinstance(value, str)
        or not (0 if empty else 1) <= len(value) <= maximum
        or any(
            (ord(char) < 32 and not (multiline and char in "\n\t")) or ord(char) == 127
            for char in value
        )
    ):
        raise McpProtocolError(f"Invalid bounded MCP {label}")
    return value


def bounded_metadata(value: Any, *, maximum: int = MAX_DEFINITION_BYTES) -> Any:
    """Return detached finite JSON without following schemas, icons or URIs."""
    try:
        encoded = canonical_json(value)
        size = len(encoded.encode("utf-8", "strict"))
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise McpProtocolError("MCP metadata is not finite JSON") from error
    if size > maximum:
        raise McpProtocolError("MCP metadata exceeds its byte allowance")
    detached = json.loads(encoded)
    stack, count = [(detached, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 32 or count > 8192:
            raise McpProtocolError("MCP metadata exceeds its structure allowance")
        if isinstance(node, dict):
            if any(not isinstance(key, str) or len(key) > 512 for key in node):
                raise McpProtocolError("MCP metadata has an invalid object key")
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    return detached


def server_observation(
    result: dict[str, Any], version: str
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Extract display identity and capability claims from the negotiated era."""
    if result.get("resultType", "complete") != "complete":
        raise McpProtocolError("MCP discovery requires a completed result")
    capabilities = bounded_metadata(result.get("capabilities", {}), maximum=65_536)
    if not isinstance(capabilities, dict) or any(
        not isinstance(value, dict) for value in capabilities.values()
    ):
        raise McpProtocolError("MCP capabilities must be bounded objects")
    for name in ("tools", "resources", "prompts"):
        claim = capabilities.get(name, {})
        for field in ("listChanged", "subscribe"):
            if field in claim and type(claim[field]) is not bool:
                raise McpProtocolError("MCP capability flags must be booleans")
    metadata = result.get("_meta", {})
    if not isinstance(metadata, dict):
        raise McpProtocolError("MCP result metadata must be an object")
    info = (
        metadata.get("io.modelcontextprotocol/serverInfo")
        if version == MODERN
        else result.get("serverInfo")
    )
    if info is not None:
        info = bounded_metadata(info, maximum=16_384)
        if not isinstance(info, dict):
            raise McpProtocolError("MCP server identity must be an object")
        _text(info.get("name"), "server name", 256)
        _text(info.get("version"), "server version", 128)
        if "title" in info:
            _text(info["title"], "server title", 512)
    return info, capabilities


def cache_ttl(result: dict[str, Any]) -> int | None:
    value = result.get("ttlMs")
    if value is not None and (type(value) is not int or not 0 <= value < 2**53):
        raise McpProtocolError("MCP cache TTL must be a nonnegative bounded integer")
    return value


def catalog_item(kind: str, value: Any, *, allow_http_headers: bool = False) -> dict[str, Any]:
    definition = bounded_metadata(value)
    if not isinstance(definition, dict):
        raise McpProtocolError("MCP interface definition must be an object")
    name = _text(definition.get("name"), "interface name", 128)
    description = _text(
        definition.get("description", ""), "description", 4096, empty=True, multiline=True
    )
    if "title" in definition:
        _text(definition["title"], "interface title", 512)
    if kind == "tool":
        McpToolInfo.from_wire(definition, allow_http_headers=allow_http_headers)
        identity = name
    elif kind in {"resource", "resource_template"}:
        field = "uri" if kind == "resource" else "uriTemplate"
        identity = _text(definition.get(field), "resource identity", 2048)
        if any(ord(char) < 33 for char in identity):
            raise McpProtocolError("MCP resource identity contains whitespace or controls")
        if kind == "resource" and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", identity):
            raise McpProtocolError("MCP resource identity must have a URI scheme")
        if "mimeType" in definition:
            _text(definition["mimeType"], "resource MIME type", 256)
        if "size" in definition and (
            type(definition["size"]) is not int or not 0 <= definition["size"] < 2**53
        ):
            raise McpProtocolError("MCP resource size must be a nonnegative bounded integer")
    elif kind == "prompt":
        identity = name
        arguments = definition.get("arguments", [])
        if not isinstance(arguments, list) or len(arguments) > 128:
            raise McpProtocolError("MCP prompt argument inventory exceeds its allowance")
        names = set()
        for argument in arguments:
            if not isinstance(argument, dict):
                raise McpProtocolError("MCP prompt argument must be an object")
            arg_name = _text(argument.get("name"), "prompt argument name", 128)
            if arg_name in names:
                raise McpProtocolError("MCP prompt argument names must be unique")
            names.add(arg_name)
            _text(
                argument.get("description", ""),
                "prompt argument description",
                4096,
                empty=True,
                multiline=True,
            )
            if "required" in argument and type(argument["required"]) is not bool:
                raise McpProtocolError("MCP prompt requirement must be a boolean")
    else:
        raise McpProtocolError("Unknown MCP interface collection")
    return {
        "kind": kind,
        "identity": identity,
        "name": name,
        "description": description,
        "definition": definition,
        "definitionSha256": content_hash(definition),
        "invocable": False,
    }


@dataclass(frozen=True, slots=True)
class McpDiscoveryCatalog:
    _json: str

    @classmethod
    def from_dict(cls, value: Any) -> McpDiscoveryCatalog:
        fields = {
            "schema",
            "protocolVersion",
            "serverInfo",
            "capabilities",
            "collections",
            "items",
            "cacheTtlMs",
            "notificationSupport",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != SCHEMA
            or not isinstance(value["protocolVersion"], str)
            or value["protocolVersion"] not in {MODERN, *SUPPORTED_LEGACY}
        ):
            raise McpProtocolError("Invalid live MCP catalogue envelope")
        version = value["protocolVersion"]
        info, capabilities = server_observation(
            {
                "capabilities": value["capabilities"],
                "serverInfo": value["serverInfo"],
                "_meta": {"io.modelcontextprotocol/serverInfo": value["serverInfo"]},
            },
            version,
        )
        collections = value["collections"]
        if not isinstance(collections, dict) or set(collections) != set(COLLECTIONS):
            raise McpProtocolError("MCP catalogue must account for every collection")
        for collection, (capability, _method, _kind) in COLLECTIONS.items():
            expected = "observed" if capability in capabilities else "not_advertised"
            if collections[collection] != expected:
                raise McpProtocolError("MCP collection state differs from its capability claim")
        support = {
            name: capabilities.get(name, {}).get("listChanged") is True
            for name in SUBSCRIPTION_FILTERS
        }
        if value["notificationSupport"] != support or any(
            type(flag) is not bool for flag in value["notificationSupport"].values()
        ):
            raise McpProtocolError("MCP notification support differs from its capability claim")
        items = value["items"]
        if not isinstance(items, list) or len(items) > MAX_CATALOG_ITEMS:
            raise McpProtocolError("MCP catalogue exceeds its interface allowance")
        normalized = []
        total = 0
        seen: set[tuple[str, str]] = set()
        counts = {kind: 0 for _capability, _method, kind in COLLECTIONS.values()}
        kind_collection = {kind: collection for collection, (_, _, kind) in COLLECTIONS.items()}
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "kind",
                "identity",
                "name",
                "description",
                "definition",
                "definitionSha256",
                "invocable",
            }:
                raise McpProtocolError("Invalid live MCP interface envelope")
            item_kind = item["kind"]
            if not isinstance(item_kind, str) or item_kind not in counts:
                raise McpProtocolError("Unknown live MCP interface kind")
            expected_item = catalog_item(item_kind, item["definition"], allow_http_headers=True)
            if item != expected_item or item["invocable"] is not False:
                raise McpProtocolError("MCP interface differs from its exact definition")
            total += len(canonical_json(expected_item).encode())
            if total > MAX_CATALOG_BYTES:
                raise McpProtocolError("MCP catalogue exceeds its combined byte allowance")
            key = (item_kind, item["identity"])
            counts[item_kind] += 1
            if (
                key in seen
                or counts[item_kind] > MAX_COLLECTION_ITEMS
                or collections[kind_collection[item_kind]] != "observed"
            ):
                raise McpProtocolError("MCP interface inventory is duplicated or unadvertised")
            seen.add(key)
            normalized.append(expected_item)
        normalized.sort(key=lambda item: (item["kind"], item["identity"]))
        result = {
            "schema": SCHEMA,
            "protocolVersion": version,
            "serverInfo": info,
            "capabilities": capabilities,
            "collections": dict(collections),
            "items": normalized,
            "cacheTtlMs": cache_ttl({"ttlMs": value["cacheTtlMs"]}),
            "notificationSupport": support,
        }
        encoded = canonical_json(result)
        if len(encoded.encode()) > MAX_CATALOG_BYTES:
            raise McpProtocolError("MCP catalogue exceeds its byte allowance")
        return cls(encoded)

    def to_dict(self) -> dict[str, Any]:
        return dict(json.loads(self._json))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._json.encode()).hexdigest()
