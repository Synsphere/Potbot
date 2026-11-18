"""Real durable gateway admission with a marked MCP process outcome fixture.

The actual discovery validator, installation owner, run intent and receipts
remain in use. No test in this module qualifies a deployed native MCP server.
"""

import json
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from potbot.domain.errors import DomainError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import ActionProposal
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.gateway import ToolGateway
from potbot.tools.mcp_discovery_host import PROFILE_SCHEMA, read_mcp_profile
from potbot.tools.mcp_protocol import McpCallResult
from potbot.workspace_sandbox import WorkspaceSandbox
from tests.integration.test_execution_gateway import reserve, setup
from tests.integration.test_mcp_discovery import (
    TOKEN,
    admit,
    catalog,
    complete,
    control,
    reference,
    rig,
    runtime_rig,
)
from tests.unit.test_mcp_discovery_gateway import document, installed_fixture

pytestmark = pytest.mark.postgres
__all__ = ["rig", "runtime_rig"]


def installed_http(r):
    provider = SimpleNamespace(grant=r.actor, registration_fingerprint="2" * 64)
    r.installed = installed_fixture(
        r.actor, provider=provider, executable_sha256=r.installed.executable_sha256
    )
    r.binding = admit(r, key="admit-http", name="HTTP fixture MCP")
    r.installed = replace(
        r.installed,
        discovery_binding_id=UUID(r.binding["id"]),
        discovery_configuration_sha256=r.configuration,
    )


def gateway_fixture(r, tmp_path, monkeypatch, selected, *, missing_reference=False, phase=None):
    # The setup capability is an explicit secret-store fixture; all database
    # signature and current event-head checks still use production code.
    monkeypatch.setattr(
        "potbot.service_credentials.ServiceCredentials",
        lambda: SimpleNamespace(store=SimpleNamespace(get=lambda _name: TOKEN)),
    )
    home = r.runtime.homes.lookup(r.actor.workspace_id, r.agent)
    sandbox = WorkspaceSandbox.prepare(r.runtime.runtime.home / home.directory)
    execution = setup(
        r.env,
        tmp_path,
        tools=(r.installed.definition,),
        candidate_root=sandbox.path("candidates"),
    )
    gateway = ToolGateway(
        r.env.db,
        execution.artifacts,
        execution.candidates,
        execution.registry,
        execution.capabilities,
        execution.receipts,
        shared_runtime=r.runtime.runtime,
        shared_leases=r.runtime.ledger,
        agent_homes=r.runtime.homes,
        host_agent_id=r.agent,
        mcp_installations=(r.installed,),
        mcp_routes=(r.route,),
    )
    value = document(r.installed, **({} if missing_reference else {"mcp_reference": selected}))
    artifact = execution.artifacts.put(r.actor, canonical_json(value).encode(), "application/json")
    inputs = execution.store.create_inputs(
        r.actor,
        execution.episode_id,
        execution.contract_id,
        execution.candidate["state_id"],
        "linked-inputs",
    )
    action = execution.store.propose(
        r.actor,
        UUID(inputs["node_id"]),
        ActionProposal(r.installed.definition.name),
        "linked-action",
        stdin_artifact_id=artifact["id"],
    )
    reservation = reserve(execution, r.installed.definition.name)
    starts, calls = [], []

    class ExplicitExecutionClient:
        def __init__(self, **config):
            self.config = config

        async def __aenter__(self):
            starts.append(self.config["managed_scope"].run_id)
            return self

        async def __aexit__(self, *_):
            return None

        async def call(self, name, supplied):
            assert name == "search" and supplied == {"query": "fixture"}
            if phase == "approve":
                control(r, "disable")
            self.config["approve_call"](r.installed.tool, supplied)
            if phase == "http-dispatch":
                control(r, "revoke")
            if r.installed.http_provider:
                assert self.config["before_http_call"] is not None
                await self.config["before_http_call"](
                    {
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": supplied},
                    }
                )
            calls.append(name)
            return McpCallResult.from_wire(name, {"structuredContent": {"matches": 1}})

        async def close(self):
            return r.exit

    monkeypatch.setattr("potbot.tools.mcp_gateway.SandboxedMcpClient", ExplicitExecutionClient)
    return SimpleNamespace(
        gateway=gateway,
        execution=execution,
        action=UUID(action["id"]),
        reservation=reservation,
        starts=starts,
        calls=calls,
    )


@pytest.mark.parametrize(
    "block", ["missing-reference", "disabled", "revoked", "expired", "schema-changed"]
)
def test_linked_gateway_refuses_stale_authority_before_run_admission(
    rig, tmp_path, monkeypatch, block
):
    r = rig
    if block == "expired":
        r.clock[0] -= timedelta(seconds=61)
    complete(r)
    selected = reference(r)
    if block in {"disabled", "revoked"}:
        control(r, "disable" if block == "disabled" else "revoke")
    elif block == "schema-changed":
        complete(r, key="changed-schema", variant="changed_schema")
    g = gateway_fixture(
        r, tmp_path, monkeypatch, selected, missing_reference=block == "missing-reference"
    )
    with pytest.raises(DomainError):
        g.gateway.execute(r.actor, g.action, g.execution.lease, g.reservation)
    assert g.gateway.store.inspect(r.actor, g.action)["run"] is None
    assert g.starts == [] and g.calls == []


def test_changed_native_profile_bytes_fence_an_otherwise_identical_installed_owner(
    rig, tmp_path, monkeypatch
):
    r = rig
    peer_catalog, _peer = catalog()
    tool = next(
        item["definition"]
        for item in peer_catalog.to_dict()["items"]
        if item["kind"] == "tool" and item["name"] == "search"
    )
    profile = {
        "schema": PROFILE_SCHEMA,
        "agentId": str(r.agent),
        "definition": asdict(r.installed.definition),
        "tool": tool,
        "capability": r.installed.capability.to_dict(),
        "implementation": r.installed.implementation.to_dict(),
        "installation": r.installed.installation.to_dict(),
        "executableSha256": r.installed.executable_sha256,
        "oauthRegistration": None,
    }
    path = tmp_path / "native-mcp-profile.json"
    path.write_text(canonical_json(profile), encoding="utf-8")
    path.chmod(0o600)
    original = read_mcp_profile(r.actor, path)
    r.configuration, r.installed = original.configuration_sha256, original.installed
    r.binding = admit(r, key="admit-exact-profile")
    r.installed = read_mcp_profile(r.actor, path, binding_id=UUID(r.binding["id"])).installed
    complete(r)
    selected = reference(r)
    path.write_text(json.dumps(profile, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    replacement = read_mcp_profile(r.actor, path, binding_id=UUID(r.binding["id"]))
    assert replacement.installed.installation.fingerprint == r.installed.installation.fingerprint
    assert replacement.installed.tool.fingerprint == r.installed.tool.fingerprint
    assert replacement.configuration_sha256 != original.configuration_sha256
    r.installed = replacement.installed
    g = gateway_fixture(r, tmp_path, monkeypatch, selected)
    with pytest.raises(ScopeDeniedError, match="installed tool owner"):
        g.gateway.execute(r.actor, g.action, g.execution.lease, g.reservation)
    assert g.gateway.store.inspect(r.actor, g.action)["run"] is None
    assert g.starts == [] and g.calls == []


@pytest.mark.parametrize("phase", ["approve", "http-dispatch"])
def test_linked_gateway_rechecks_current_authority_at_the_actual_exchange_boundary(
    rig, tmp_path, monkeypatch, phase
):
    r = rig
    if phase == "http-dispatch":
        installed_http(r)
    complete(r)
    g = gateway_fixture(r, tmp_path, monkeypatch, reference(r), phase=phase)
    with pytest.raises(ScopeDeniedError):
        g.gateway.execute(r.actor, g.action, g.execution.lease, g.reservation)
    view = g.gateway.store.inspect(r.actor, g.action)
    assert view["execution_status"] == "indeterminate" and view["completion"] is None
    assert len(g.starts) == 1 and g.calls == []
    budget = g.execution.budgets.inspect(r.actor, g.execution.episode_id)
    assert budget["held"]["calls"] == 1 and not budget["used"]


@pytest.mark.parametrize("lost_response", [False, True])
def test_linked_gateway_pins_reference_and_recovers_a_stopped_receipt_after_revocation(
    rig, tmp_path, monkeypatch, lost_response
):
    r = rig
    complete(r)
    selected = reference(r)
    g = gateway_fixture(r, tmp_path, monkeypatch, selected)

    def interrupted():
        raise OSError("Explicit fixture loss after the durable stopped receipt")

    if lost_response:
        with pytest.raises(OSError, match="durable stopped receipt"):
            g.gateway.execute(
                r.actor, g.action, g.execution.lease, g.reservation, after_receipt=interrupted
            )
        view = g.gateway.store.inspect(r.actor, g.action)
        assert view["execution_status"] == "indeterminate"
    else:
        view = g.gateway.execute(r.actor, g.action, g.execution.lease, g.reservation)
        assert view["execution_status"] == "completed"
    run_id = UUID(view["run"]["id"])
    with r.env.db.transaction(r.actor) as conn:
        intent = conn.execute(
            "SELECT details FROM evidence_events WHERE run_id=%s AND event_type='tool_dispatch_intent'",
            (run_id,),
        ).fetchone()["details"]
        run = conn.execute("SELECT * FROM action_runs WHERE id=%s", (run_id,)).fetchone()
    assert intent["mcp_discovery_reference"] == selected
    assert intent["mcp_discovery_reference_sha256"] == content_hash(selected)
    receipt = g.gateway.receipts.read(r.actor.workspace_id, run_id)
    assert receipt["mcp_discovery_reference_sha256"] == content_hash(selected)
    with pytest.raises(StateMismatchError, match="discovery reference"):
        g.gateway._validate_receipt(
            r.actor,
            dict(run),
            {**receipt, "mcp_discovery_reference_sha256": "0" * 64},
            g.execution.candidate,
        )
    control(r, "revoke")
    if lost_response:
        view = g.gateway.reconcile(r.actor, run_id)
        assert view["execution_status"] == "completed"
    replay = g.gateway.execute(r.actor, g.action, g.execution.lease, g.reservation)
    assert replay["execution_status"] == "completed"
    assert replay["completion"]["details"]["mcp_discovery_reference_sha256"] == content_hash(
        selected
    )
    assert g.calls == ["search"] and len(g.starts) == 1
    budget = g.execution.budgets.inspect(r.actor, g.execution.episode_id)
    assert budget["used"]["calls"] == 1 and not budget["held"].get("calls", 0)
