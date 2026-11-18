"""Exact discovery linkage at the existing installed MCP execution boundary."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.execution import ToolDefinition
from potbot.domain.serialization import canonical_json
from potbot.tools.capability_contracts import (
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
)
from potbot.tools.mcp_gateway import InstalledMcpTool, _frozen_input, mcp_configuration_hash
from potbot.tools.mcp_http_worker import WORKER_ARGS
from potbot.tools.mcp_protocol import MAX_WIRE_BYTES, McpToolInfo


def installed_fixture(
    context,
    *,
    linked=None,
    provider=None,
    executable_sha256="e" * 64,
    discovery_configuration_sha256="b" * 64,
):
    """Actual validated owner contracts; executable/provider remain explicit fixtures."""
    definition = ToolDefinition(
        "fixture-mcp",
        "test",
        ("/usr/bin/python3", *WORKER_ARGS) if provider else ("/usr/bin/python3",),
        mutates=False,
        accepts_stdin=True,
        protocol="mcp",
    )
    tool = McpToolInfo.from_wire(
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
        }
    )
    capability = tool.to_capability(server_slug="fixture", effect_class="read_only")
    implementation = ToolImplementationDescriptor(
        "syn://thing/fixture-mcp-implementation",
        capability.fingerprint,
        "d" * 64,
        "Fixture publisher",
        "1.0.0",
    )
    installation = ToolInstallationDescriptor(
        "syn://thing/fixture-mcp-installation",
        context.workspace_id,
        implementation.fingerprint,
        mcp_configuration_hash(
            definition,
            tool,
            executable_sha256,
            http_provider=provider,
            principal_id=context.principal_id,
        ),
        "f" * 64,
        ("test",),
    )
    return InstalledMcpTool(
        definition,
        tool,
        executable_sha256,
        capability,
        implementation,
        installation,
        http_provider=provider,
        principal_id=context.principal_id,
        discovery_binding_id=linked,
        discovery_configuration_sha256=discovery_configuration_sha256 if linked else None,
    )


@pytest.fixture
def installed():
    return installed_fixture(WorkspaceContext(uuid4(), uuid4()))


def document(installed, **extra):
    return {
        "installation_hash": installed.installation.fingerprint,
        "arguments": {"query": "fixture"},
        **extra,
    }


def test_existing_unlinked_input_contract_is_preserved(installed):
    value = document(installed)
    assert _frozen_input(canonical_json(value).encode(), installed.installation) == value


def test_linked_input_requires_and_retains_exact_reference(installed):
    binding = uuid4()
    installed = replace(
        installed, discovery_binding_id=binding, discovery_configuration_sha256="b" * 64
    )
    reference = {"bindingId": str(binding), "snapshotId": str(uuid4()), "digest": "a" * 64}
    value = document(installed, mcp_reference=reference)
    assert (
        _frozen_input(
            canonical_json(value).encode(), installed.installation, discovery_binding_id=binding
        )
        == value
    )
    with pytest.raises(InvalidInputError):
        _frozen_input(
            canonical_json(document(installed)).encode(),
            installed.installation,
            discovery_binding_id=binding,
        )


@pytest.mark.parametrize("reference", [None, "approval", [], True])
def test_reference_must_be_an_exact_object(installed, reference):
    with pytest.raises(InvalidInputError):
        _frozen_input(
            canonical_json(document(installed, mcp_reference=reference)).encode(),
            installed.installation,
            discovery_binding_id=uuid4(),
        )


def test_unlinked_installation_cannot_accept_an_unchecked_reference(installed):
    with pytest.raises(InvalidInputError):
        _frozen_input(
            canonical_json(document(installed, mcp_reference={"approved": True})).encode(),
            installed.installation,
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b'{"arguments":{},"arguments":{},"installation_hash":"x"}',
        b'{"arguments":{"query":"\\ud800"},"installation_hash":"x","mcp_reference":{}}',
        b"[1]",
        b" " * (MAX_WIRE_BYTES + 1),
    ],
)
def test_frozen_input_refuses_ambiguous_or_unbounded_bytes(installed, raw):
    with pytest.raises(InvalidInputError):
        _frozen_input(raw, installed.installation, discovery_binding_id=uuid4())


def test_exact_installation_hash_still_required(installed):
    value = document(installed)
    value["installation_hash"] = "1" * 64
    with pytest.raises(ScopeDeniedError):
        _frozen_input(canonical_json(value).encode(), installed.installation)


def test_discovery_link_requires_canonical_typed_identity_and_principal(installed):
    with pytest.raises(InvalidInputError):
        replace(installed, discovery_binding_id=str(uuid4()))
    with pytest.raises(InvalidInputError):
        replace(installed, discovery_binding_id=uuid4(), principal_id=None)


@pytest.mark.parametrize("pin", [None, "bad", "A" * 64, 3])
def test_discovery_link_requires_an_exact_native_configuration_pin(installed, pin):
    with pytest.raises(InvalidInputError):
        replace(installed, discovery_binding_id=uuid4(), discovery_configuration_sha256=pin)


def test_unlinked_owner_cannot_carry_a_detached_discovery_configuration_pin(installed):
    with pytest.raises(InvalidInputError):
        replace(installed, discovery_configuration_sha256="b" * 64)
