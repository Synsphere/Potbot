"""Native discovery owners with explicit runtime/launcher/provider fixtures.

Real SQLite leases, Agent registration, subprocess shutdown and MCP collection
are exercised. The fixture launcher does not qualify native isolation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from potbot.api.hosted_mcp_oauth import read_mcp_oauth_registration
from potbot.credentials import CredentialStore
from potbot.domain.errors import (
    DomainError,
    IndeterminateExecutionError,
    InvalidInputError,
    OperationCancelledError,
    OperationTimeoutError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.service_credentials import ServiceCredentials
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.mcp_discovery_host import (
    MAX_PROFILE_BYTES,
    McpCatalogChangedError,
    McpDiscoveryHost,
    build_mcp_observation,
    configured_mcp_discovery_host,
    load_discovery_installation,
    read_mcp_profile,
)
from potbot.tools.mcp_http import McpHttpTransport
from potbot.tools.mcp_protocol import TOOLS_LISTEN
from potbot.tools.syn_mcp_route import SynMcpRoute
from tests.fixtures.mcp_live_catalog import RawCatalogPeer
from tests.unit import test_mcp_oauth as oauth_fixtures
from tests.unit import test_skill_eligibility as runtime_fixtures
from tests.unit.test_mcp_discovery_gateway import installed_fixture

runtime_rig = runtime_fixtures.rig
signing_key = oauth_fixtures.signing_key
STDIO = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_live_stdio_server.py"


def profile_document(installed, agent_id, oauth=None):
    tool = installed.tool
    wire = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": json.loads(tool.input_schema_json),
    }
    if tool.output_schema_json is not None:
        wire["outputSchema"] = json.loads(tool.output_schema_json)
    return json.loads(
        json.dumps(
            {
                "schema": "potbot-mcp-installed-profile/v1",
                "agentId": str(agent_id),
                "definition": asdict(installed.definition),
                "tool": wire,
                "capability": installed.capability.to_dict(),
                "implementation": installed.implementation.to_dict(),
                "installation": installed.installation.to_dict(),
                "executableSha256": installed.executable_sha256,
                "oauthRegistration": str(oauth) if oauth is not None else None,
            }
        )
    )


def write_profile(path, value):
    path.write_text(canonical_json(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def operator_directory():
    # The CI workspace may itself be group/world writable; native operator
    # selections must live beneath trusted ancestors even in the fixture.
    with tempfile.TemporaryDirectory(prefix="potbot-mcp-profile-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def profile(runtime_rig, operator_directory):
    runtime = runtime_rig
    installed = installed_fixture(
        runtime.context,
        executable_sha256=hashlib.sha256(runtime.interpreter.read_bytes()).hexdigest(),
    )
    path = write_profile(
        operator_directory / "mcp.json", profile_document(installed, runtime.agent_id)
    )
    return SimpleNamespace(runtime=runtime, installed=installed, path=path)


def test_profile_roundtrip_binds_exact_bytes_and_keeps_link_outside_legacy_installation_hash(
    profile,
):
    plain = read_mcp_profile(profile.runtime.context, profile.path)
    binding = uuid4()
    linked = read_mcp_profile(profile.runtime.context, profile.path, binding_id=binding)
    assert plain.installed == profile.installed
    assert linked.installed.discovery_binding_id == binding
    assert linked.installed.discovery_configuration_sha256 == linked.configuration_sha256
    assert plain.configuration_sha256 == linked.configuration_sha256
    assert linked.private_paths == (profile.path,)
    assert plain.installed.installation.fingerprint == linked.installed.installation.fingerprint
    assert (
        load_discovery_installation(profile.runtime.context, profile.path, binding)
        == linked.installed
    )
    profile.path.write_text(profile.path.read_text() + "\n")
    changed = read_mcp_profile(profile.runtime.context, profile.path)
    assert changed.installed == plain.installed
    assert changed.configuration_sha256 != plain.configuration_sha256


@pytest.mark.parametrize("binding", [None, "not-a-uuid", 1, True])
def test_linked_loader_never_falls_back_to_unlinked(profile, binding):
    with pytest.raises(InvalidInputError):
        load_discovery_installation(profile.runtime.context, profile.path, binding)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema="unknown"),
        lambda value: value.update(extra=True),
        lambda value: value.update(agentId="unregistered-text"),
        lambda value: value["definition"].pop("memory_bytes"),
        lambda value: value["definition"].update(argv_prefix="/usr/bin/python3"),
        lambda value: value["definition"].update(mutates=True),
        lambda value: value["definition"].update(environment=["LANG=C"]),
        lambda value: value["installation"].update(configuration_hash="0" * 64),
        lambda value: value["installation"].update(workspace_id=str(uuid4())),
        lambda value: value.update(oauthRegistration=[]),
        lambda value: value.update(executableSha256="not-a-pin"),
    ],
)
def test_profile_rejects_ambiguous_or_changed_operator_contracts(profile, mutate):
    value = json.loads(profile.path.read_bytes())
    mutate(value)
    write_profile(profile.path, value)
    with pytest.raises(DomainError):
        read_mcp_profile(profile.runtime.context, profile.path)


@pytest.mark.parametrize(
    "fault", ["mode", "link", "ancestor", "hardlink", "fifo", "oversize", "duplicate"]
)
def test_private_profile_file_boundaries(profile, fault):
    selected = profile.path
    if fault == "mode":
        selected.chmod(0o644)
    elif fault == "link":
        selected = selected.with_name("link.json")
        selected.symlink_to(profile.path)
    elif fault == "ancestor":
        alias = profile.path.parent / "alias"
        alias.symlink_to(profile.path.parent, target_is_directory=True)
        selected = alias / profile.path.name
    elif fault == "hardlink":
        os.link(selected, selected.with_name("hardlink.json"))
    elif fault == "fifo":
        selected.unlink()
        os.mkfifo(selected, mode=0o600)
    elif fault == "oversize":
        selected.write_bytes(b" " * (MAX_PROFILE_BYTES + 1))
    else:
        selected.write_bytes(b'{"schema":"one","schema":"two"}')
    with pytest.raises((DomainError, OSError)):
        read_mcp_profile(profile.runtime.context, selected)


def test_profile_inode_change_while_parsing_is_rejected(profile, monkeypatch):
    from potbot.tools import mcp_discovery_host as module

    original = module.ToolCapabilityDescriptor.from_dict

    def changed(value):
        profile.path.write_bytes(profile.path.read_bytes())
        return original(value)

    monkeypatch.setattr(module.ToolCapabilityDescriptor, "from_dict", changed)
    with pytest.raises(StateMismatchError):
        read_mcp_profile(profile.runtime.context, profile.path)


@pytest.fixture
def oauth_profile(runtime_rig, signing_key, operator_directory):
    runtime = runtime_rig

    class NoCredentialReads:
        def get_password(self, *_):
            pytest.fail("Reading a native installation must not access a bearer credential")

    credentials = ServiceCredentials(
        store=CredentialStore(backend=NoCredentialReads()), environment={}
    )
    registration = {
        "resource": "https://mcp.fixture.example/mcp",
        "issuer": "https://login.fixture.example/tenant",
        "scopes": ["tools:read"],
        "client_id": "fixture-client",
        "subject": "fixture-subject",
        "callback": "https://controller.fixture.example/v1/mcp/oauth/callback",
        "authorization_endpoint": "https://login.fixture.example/authorize",
        "token_endpoint": "https://login.fixture.example/token",
        "key_id": "fixture-key",
        "signing_key_pem": signing_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    }
    oauth = write_profile(operator_directory / "oauth.json", registration)
    provider = read_mcp_oauth_registration(oauth, runtime.context, credentials).provider
    installed = installed_fixture(
        runtime.context,
        provider=provider,
        executable_sha256=hashlib.sha256(runtime.interpreter.read_bytes()).hexdigest(),
    )
    path = write_profile(
        operator_directory / "mcp-http.json", profile_document(installed, runtime.agent_id, oauth)
    )
    return SimpleNamespace(**locals())


def test_http_profile_reuses_actual_registration_without_reading_credentials(oauth_profile):
    fixture = oauth_profile
    loaded = read_mcp_profile(
        fixture.runtime.context, fixture.path, credentials=fixture.credentials
    )
    assert (
        loaded.installed.http_provider.registration_fingerprint
        == fixture.provider.registration_fingerprint
    )
    assert loaded.private_paths == (fixture.path, fixture.oauth)
    fixture.oauth.write_text(fixture.oauth.read_text() + "\n")
    changed = read_mcp_profile(
        fixture.runtime.context, fixture.path, credentials=fixture.credentials
    )
    assert changed.installed.installation.fingerprint == loaded.installed.installation.fingerprint
    assert changed.configuration_sha256 != loaded.configuration_sha256
    value = dict(fixture.registration, subject="another-subject")
    write_profile(fixture.oauth, value)
    with pytest.raises(ScopeDeniedError):
        read_mcp_profile(fixture.runtime.context, fixture.path, credentials=fixture.credentials)


class ExplicitFixtureBackend:
    def prepare(self, *, argv, status_fd, **_):
        os.write(status_fd, b"explicit subprocess fixture; not native qualification\n")
        return SimpleNamespace(argv=argv, pass_fds=(status_fd,), identity="a" * 64)


class ExplicitInteractiveSupervisor(InteractiveProcessSupervisor):
    """Fixture launch only; the production native owners remain around it."""

    def __init__(self, backend, launcher):
        super().__init__(backend, monitor_config=SimpleNamespace(scoped_launcher=launcher))
        self.requests = []
        self.children = []
        self.during = lambda: None
        self.uncertain = False
        self.linger = False

    async def open(self, request, *, cancelled=None):
        self.requests.append(request)
        suffix = request.argv[1:] or ("-u", str(STDIO))
        if self.linger:
            suffix = (*suffix, "--linger-after-eof")
        child = await InteractiveProcessSupervisor(ExplicitFixtureBackend()).open(
            replace(request, argv=(sys.executable, *suffix), guest_uid=None, guest_gid=None),
            cancelled=cancelled,
        )
        self.children.append(child)
        if self.uncertain:
            close = child.close

            async def unconfirmed(**kwargs):
                return replace(await close(**kwargs), processes_stopped=False)

            child.close = unconfirmed
        self.during()
        return child


@pytest.fixture
def native(profile):
    rig = profile.runtime
    supervisor = ExplicitInteractiveSupervisor(
        rig.supervisor.backend, SimpleNamespace(is_socket=lambda: True)
    )
    deliveries = []

    def exchange(identity, payload):
        deliveries.append(identity)
        return route.accept(
            {
                "profile": "syn.local-rpc-dispatch/1",
                "workspace_id": str(rig.context.workspace_id),
                "method": "mcp.command",
                "request_id": str(identity),
                "boot_id": "a" * 32,
                "caller_uid": 1000,
                "size": len(payload),
            },
            payload,
        ), "a" * 32

    route = SynMcpRoute(rig.context.workspace_id, "a" * 32, 1000, exchange)
    host = McpDiscoveryHost(rig.runtime, rig.homes, supervisor, route, clock=lambda: rig.now)
    yield SimpleNamespace(**locals())
    route.close()


def observe(native, **changes):
    kwargs = {
        "attempt_id": uuid4(),
        "installed": native.profile.installed,
        "configuration_sha256": "b" * 64,
        "agent_id": native.rig.agent_id,
        "watch_ms": 0,
        "authorize_attempt": lambda: None,
    }
    kwargs.update(changes)
    return asyncio.run(native.host.observe(native.rig.context, **kwargs))


def test_host_runs_collector_with_real_lease_and_confirmed_actual_subprocess_stop(native):
    admitted = []

    def authorize():
        admitted.extend(
            row["operation"] for row in native.rig.ledger.active(native.rig.context.workspace_id)
        )

    catalog, report = observe(native, authorize_attempt=authorize)
    assert len(catalog.to_dict()["items"]) == 5
    assert report["processesStopped"] is True and report["agentHomeRevision"] == 1
    assert report["configurationSha256"] == "b" * 64
    assert set(admitted) == {"mcp.discovery.prepare", "mcp.sandbox.execute"}
    assert native.rig.ledger.active(native.rig.context.workspace_id) == []
    assert native.supervisor.children[0].closed
    assert native.supervisor.requests[0].argv == native.profile.installed.definition.argv_prefix
    assert native.supervisor.requests[0].cwd.name.startswith("mcp-discovery-")
    assert len(report["stopReceiptSha256"]) == 64


def test_authority_revoked_after_native_lease_start_never_launches(native):
    def authorize():
        if any(
            row["operation"] == "mcp.sandbox.execute"
            for row in native.rig.ledger.active(native.rig.context.workspace_id)
        ):
            raise ScopeDeniedError("fixture admission revoked inside lease")

    with pytest.raises(ScopeDeniedError):
        observe(native, authorize_attempt=authorize)
    assert native.supervisor.requests == []
    assert native.rig.ledger.active(native.rig.context.workspace_id) == []


def test_unknown_native_stop_fences_existing_lease_and_retains_candidate(native):
    native.supervisor.uncertain = True
    with pytest.raises(IndeterminateExecutionError):
        observe(native)
    leases = native.rig.ledger.active(native.rig.context.workspace_id)
    assert len(leases) == 1 and leases[0]["state"] == "indeterminate"
    assert native.supervisor.requests[0].cwd.is_dir()
    with pytest.raises(StateMismatchError):
        observe(native)
    assert len(native.supervisor.requests) == 1


def test_attempt_candidate_is_create_only_and_not_replayed(native):
    attempt = uuid4()
    observe(native, attempt_id=attempt)
    with pytest.raises(StateMismatchError):
        observe(native, attempt_id=attempt)
    assert len(native.supervisor.requests) == 1


def test_runtime_executable_changed_during_launch_prevents_publication(native):
    native.supervisor.during = lambda: native.rig.interpreter.write_bytes(
        b"changed fixture interpreter"
    )
    with pytest.raises(StateMismatchError):
        observe(native)
    assert native.supervisor.children[0].closed
    assert native.rig.ledger.active(native.rig.context.workspace_id) == []


def test_cancellation_and_invalid_clock_prevent_launch(native):
    with pytest.raises(OperationCancelledError):
        observe(native, cancelled=lambda: True)
    native.host.clock = lambda: native.rig.now.replace(tzinfo=None)
    with pytest.raises(InvalidInputError):
        observe(native)
    assert native.supervisor.requests == []


@pytest.mark.parametrize("interruption", ["caller", "deadline"])
def test_shutdown_interruption_retains_real_receipt_and_finalizes_native_lease(
    native, monkeypatch, interruption
):
    from potbot.tools import mcp_discovery_host as module
    from potbot.tools.mcp_sandbox import SandboxedMcpClient

    native.supervisor.linger = True
    sessions = []

    class ObservedClient(SandboxedMcpClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            sessions.append(self)

    monkeypatch.setattr(module, "SandboxedMcpClient", ObservedClient)
    if interruption == "deadline":
        monkeypatch.setattr(module, "MAX_ATTEMPT_SECONDS", 0.25)

    async def scenario():
        pending = asyncio.create_task(
            native.host.observe(
                native.rig.context,
                attempt_id=uuid4(),
                installed=native.profile.installed,
                configuration_sha256="b" * 64,
                agent_id=native.rig.agent_id,
                watch_ms=0,
                authorize_attempt=lambda: None,
            )
        )
        async with asyncio.timeout(3):
            while not native.supervisor.children or not native.supervisor.children[0].closed:
                await asyncio.sleep(0.005)
        if interruption == "caller":
            pending.cancel()
        with pytest.raises(
            asyncio.CancelledError if interruption == "caller" else OperationTimeoutError
        ):
            await pending
        child = native.supervisor.children[0]
        assert child.process.returncode is not None
        assert sessions[0].completion is not None and sessions[0].completion.processes_stopped
        assert native.rig.ledger.active(native.rig.context.workspace_id) == []

    asyncio.run(scenario())


def test_operator_profiles_must_remain_outside_every_worker_mount(native):
    assert native.host.validate_private_paths((native.profile.path,)) == (native.profile.path,)
    inside = write_profile(native.rig.home / "untrusted-profile.json", {"data": "fixture"})
    with pytest.raises(ScopeDeniedError):
        native.host.validate_private_paths((inside,))


@pytest.mark.parametrize("changed", [False, True])
def test_baseline_http_metadata_watch_keeps_exact_argv_and_has_no_tool_call(
    native, oauth_profile, changed, monkeypatch
):
    # The OAuth fixture shares the same pytest runtime_rig and its pinned owner.
    installed = oauth_profile.installed
    peer = RawCatalogPeer()
    methods = []

    async def response(request):
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] != TOOLS_LISTEN:
            return httpx.Response(200, json=await peer.exchange(body, True))
        meta = {"io.modelcontextprotocol/subscriptionId": body["id"]}
        messages = [
            {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": meta, "notifications": body["params"]["notifications"]},
            }
        ]
        messages.append(
            {
                "jsonrpc": "2.0",
                "method": "notifications/tools/list_changed",
                "params": {"_meta": meta},
            }
            if changed
            else {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"resultType": "complete", "_meta": meta},
            }
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content="".join("data: " + json.dumps(message) + "\n\n" for message in messages),
        )

    @asynccontextmanager
    async def broker(grant, **_):
        assert grant == installed.http_provider.grant
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            yield McpHttpTransport(grant.resource, client, network_approval=lambda _: None)

    installed.http_provider.broker = SimpleNamespace(open=broker)
    from potbot.tools.mcp_sandbox import SandboxedMcpClient

    original = SandboxedMcpClient.discover_catalog

    async def check_denial(session):
        with pytest.raises(ScopeDeniedError):
            await session.call("search", {"query": "not admitted"})
        return await original(session)

    monkeypatch.setattr(SandboxedMcpClient, "discover_catalog", check_denial)
    if changed:
        with pytest.raises(McpCatalogChangedError):
            observe(native, installed=installed, watch_ms=250)
    else:
        catalog, report = observe(native, installed=installed, watch_ms=250)
        assert len(catalog.to_dict()["items"]) == 5 and report["processesStopped"]
    assert methods.count(TOOLS_LISTEN) == 1 and "tools/call" not in methods
    assert len(native.deliveries) == 2
    assert native.supervisor.requests[0].argv == installed.definition.argv_prefix
    assert native.rig.ledger.active(native.rig.context.workspace_id) == []


def test_report_rejects_unconfirmed_shutdown_and_excessive_lifetime(native):
    catalog, _report = observe(native)
    child = native.supervisor.children[0]
    receipt = asyncio.run(child.close())
    kwargs = {
        "installed": native.profile.installed,
        "agent": native.rig.homes.lookup(native.rig.context.workspace_id, native.rig.agent_id),
        "configuration_sha256": "b" * 64,
        "runtime": native.rig.runtime,
        "runtime_sha256": content_hash(catalog.to_dict()),
        "observed_at": native.rig.now,
        "completed_at": native.rig.now,
        "completion": receipt,
    }
    with pytest.raises(IndeterminateExecutionError):
        build_mcp_observation(
            native.rig.context,
            **{**kwargs, "completion": replace(receipt, processes_stopped=False)},
        )
    with pytest.raises(OperationTimeoutError):
        build_mcp_observation(
            native.rig.context,
            **{**kwargs, "completed_at": native.rig.now + timedelta(seconds=121)},
        )


def test_configured_host_requires_existing_native_services_before_reading_manifest(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("POTBOT_OPERATOR_REQUIRED", raising=False)
    with (
        pytest.raises(ScopeDeniedError),
        configured_mcp_discovery_host(
            None, None, manifest=tmp_path / "absent.json", workspace_name="fixture"
        ),
    ):
        pytest.fail("No installed operator was selected")


def test_valid_runtime_manifest_inside_worker_home_is_denied_before_owner_binding(
    runtime_rig, operator_directory, monkeypatch
):
    from potbot.controller import native_cog_dispatch
    from potbot.tools import tool_release_gate_s12

    rig = runtime_rig
    mounted_home = operator_directory / "worker-home"
    mounted_home.mkdir()
    manifest = write_profile(
        mounted_home / "operator.json",
        {
            "workspace_id": str(rig.context.workspace_id),
            "image_digest": rig.runtime.image_digest,
            "qualification_sha256": rig.runtime.qualification_sha256,
            "rootfs": str(rig.rootfs),
            "home": str(mounted_home),
            "model_staging": str(rig.staging),
            "shared_models": str(rig.models),
            "shared_tools": str(rig.tools),
            "ai_config_dir": str(rig.config),
            "ai_history_dir": str(rig.history),
            "controller_ledger": str(rig.private / "ledger.sqlite"),
        },
    )
    manifest.chmod(0o644)  # Existing manifests permit public reads, never guest writes.
    selection = write_profile(operator_directory / "native-cog.json", {"fixture": True})
    monkeypatch.setenv("POTBOT_OPERATOR_REQUIRED", "1")
    monkeypatch.setenv("POTBOT_OPERATOR_SCOPED_LAUNCHER", "/explicit/fixture-launcher")
    monkeypatch.setenv("POTBOT_NATIVE_COG_CONFIG", str(selection))
    monkeypatch.setattr(
        native_cog_dispatch,
        "read_profile",
        lambda _: (
            {
                "profile": "potbot.hosted-native-cog/3",
                "workspace_id": str(rig.context.workspace_id),
                "workspace_name": "fixture",
                "image_digest": rig.runtime.image_digest,
                "qualification_sha256": rig.runtime.qualification_sha256,
            },
            "a" * 64,
        ),
    )
    monkeypatch.setattr(
        tool_release_gate_s12,
        "bind_installed_tool_releases",
        lambda *_: pytest.fail("No owner binding before containment preflight"),
    )
    with (
        pytest.raises(ScopeDeniedError, match="outside every worker mount"),
        configured_mcp_discovery_host(
            None, rig.context, manifest=manifest, workspace_name="fixture"
        ),
    ):
        pytest.fail("Worker-visible manifest must not configure a native attempt")
