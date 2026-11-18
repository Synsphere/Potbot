"""Real PostgreSQL live MCP observations and installed-owner reference fencing.

Peer frames and native process outcomes are explicit fixtures. Production
collector, signed persistence, scoped API and gateway admission remain in use.
These tests do not qualify a deployed MCP provider or installed native host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from potbot.api.ui_mcp_discovery import register_mcp_discovery_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.controller.capability_discovery import DiscoveryService
from potbot.controller.mcp_discovery import MAX_BINDINGS, McpDiscoveryService, _body
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.database import (
    Database,
    admin_connection,
    grant_application_role,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.interactive_supervisor import InteractiveExit, InteractiveProcessSupervisor
from potbot.tools.mcp_discovery_host import McpDiscoveryHost
from potbot.tools.mcp_protocol import McpClient
from potbot.tools.syn_mcp_route import SynMcpRoute
from tests.fixtures.mcp_live_catalog import RawCatalogPeer
from tests.unit import test_skill_eligibility as runtime_fixtures
from tests.unit.test_mcp_discovery_gateway import installed_fixture

pytestmark = pytest.mark.postgres
TOKEN = "mcp-discovery-native-fixture-" + "a" * 48
CONFIGURATION = "b" * 64
runtime_rig = runtime_fixtures.rig


@pytest.fixture
def rig(app_env, runtime_rig, monkeypatch):
    actor, agent = app_env.context, uuid4()
    runtime = runtime_rig
    runtime.ledger.pin_image(
        actor.workspace_id,
        digest=runtime.runtime.image_digest,
        image_ref="ghcr.io/test/image@sha256:" + runtime.runtime.image_digest,
        build_commit="b" * 40,
        platform="linux/amd64",
        rootfs_path=runtime.rootfs,
        qualification_sha256=runtime.runtime.qualification_sha256,
    )
    runtime.homes.provision(actor.workspace_id, agent)
    token, clock = [TOKEN], [datetime.now(UTC)]
    service = McpDiscoveryService(app_env.db, setup_token=lambda: token[0], clock=lambda: clock[0])
    result = SimpleNamespace(
        env=app_env,
        actor=actor,
        agent=agent,
        token=token,
        clock=clock,
        service=service,
        configuration=CONFIGURATION,
        runtime=runtime,
        installed=installed_fixture(
            actor, executable_sha256=hashlib.sha256(runtime.interpreter.read_bytes()).hexdigest()
        ),
        peers=[],
        variant="normal",
        era="modern",
        peer_hook=lambda _request: None,
        # Marked native outcomes at the SandboxClient seam, never a claim that
        # this fixture interpreter was launched or a deployed host qualified.
        exit=InteractiveExit(0, "a" * 64, 0, False, "b" * 64, False, True),
    )
    result.binding = admit(result)
    result.installed = replace(
        result.installed,
        discovery_binding_id=UUID(result.binding["id"]),
        discovery_configuration_sha256=result.configuration,
    )

    class ExplicitNativeClient:
        def __init__(self, **config):
            assert config["discovery_only"] is True
            assert config["confirmation"] is None if "confirmation" in config else True
            self.config = config
            self.completion = None
            self.isolation_identity = None
            self.peer = RawCatalogPeer(variant=result.variant, era=result.era)
            result.peers.append(self.peer)

            async def exchange(request, expect_reply):
                config["authorize_discovery"]()
                result.peer_hook(request)
                response = await self.peer.exchange(request, expect_reply)
                config["authorize_discovery"]()
                return response

            self.client = McpClient(exchange)
            self.peer.client = self.client

        async def __aenter__(self):
            self.config["authorize_discovery"]()
            self.isolation_identity = result.exit.isolation_identity
            return self

        async def __aexit__(self, *_):
            await self.close()

        async def discover_catalog(self):
            return await self.client.discover_catalog()

        async def watch_catalog(self):
            pytest.fail("The stdio native fixture does not authorize a watch")

        async def close(self, *, force=False):
            self.completion = result.exit
            return self.completion

    monkeypatch.setattr("potbot.tools.mcp_discovery_host.SandboxedMcpClient", ExplicitNativeClient)
    result.route = SynMcpRoute(
        actor.workspace_id, "1" * 32, 1000, lambda *_: pytest.fail("Unexpected fixture Syn call")
    )
    result.host = McpDiscoveryHost(
        runtime.runtime,
        runtime.homes,
        InteractiveProcessSupervisor(runtime.supervisor.backend),
        result.route,
        clock=lambda: clock[0],
    )
    return result


def admit(r, *, key="admit", name="Fixture MCP", installed=None):
    return r.service.admit(
        r.actor,
        installed or r.installed,
        agent_id=r.agent,
        configuration_sha256=r.configuration,
        name=name,
        ttl_seconds=300,
        watch_ms=0,
        key=key,
        setup_token=r.token[0],
    )["binding"]


def current(r):
    return r.service.binding(r.actor, UUID(r.binding["id"]))["binding"]


def control(r, action, *, key=None):
    row = current(r)
    return r.service.control(
        r.actor,
        UUID(row["id"]),
        action,
        row["revision"],
        row["bindingSha256"],
        key or action,
        setup_token=r.token[0],
    )["binding"]


def request(r, *, key="request"):
    row = current(r)
    return r.service.request(
        r.actor,
        UUID(row["id"]),
        row["revision"],
        row["bindingSha256"],
        key,
        setup_token=r.token[0],
    )["attempt"]


def claim(r, attempt, *, key="claim", fresh_only=False):
    return r.service.claim(
        r.actor,
        UUID(attempt["id"]),
        attempt["revision"],
        attempt["bindingSha256"],
        key,
        setup_token=r.token[0],
        fresh_only=fresh_only,
    )


def catalog(*, variant="normal", era="modern"):
    peer = RawCatalogPeer(variant=variant, era=era)
    client = McpClient(peer.exchange)
    peer.client = client
    return asyncio.run(client.discover_catalog()), peer


def observe(r, attempt, claimed):
    return asyncio.run(
        r.host.observe(
            r.actor,
            attempt_id=UUID(attempt["id"]),
            installed=r.installed,
            configuration_sha256=r.configuration,
            agent_id=r.agent,
            watch_ms=0,
            authorize_attempt=lambda: (
                r.service.validate_claim(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]))
                and None
            ),
        )
    )


def complete(r, *, key="observe", variant="normal", attempt=None, claimed=None):
    r.variant = variant
    attempt = attempt or request(r, key=key + "-request")
    claimed = claimed or claim(r, attempt, key=key + "-claim")
    observed, native = observe(r, attempt, claimed)
    return r.service.complete(
        r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), observed, native
    )["attempt"]


def entries(r, **options):
    return r.service.entries(r.actor, UUID(r.binding["id"]), **options)


def reference(r, *, name="search", kind="tool"):
    entry = next(value for value in entries(r, kind=kind)["items"] if value["name"] == name)
    return r.service.reference(r.actor, UUID(r.binding["id"]), UUID(entry["id"]))["reference"]


def test_admission_is_private_and_does_not_claim_an_observed_catalogue(rig):
    r = rig
    binding = current(r)
    assert binding["state"] == "enabled" and binding["approvalValid"]
    assert binding["installationSha256"] == r.installed.installation.fingerprint
    assert binding["configurationSha256"] == CONFIGURATION
    assert binding["snapshotId"] is None and binding["stale"]
    assert binding["counts"] == dict.fromkeys(
        ("tools", "resources", "resourceTemplates", "prompts"), 0
    )
    assert r.service.entries(r.actor, UUID(binding["id"]))["items"] == []
    assert r.service.attempts(r.actor)["items"] == []
    assert r.service.bindings(r.actor)["items"] == [binding]

    with r.env.other_db.transaction(r.env.other_context) as conn:
        role = conn.execute("SELECT session_user AS role").fetchone()["role"]
    sibling = provision_principal(r.env.admin_dsn, r.env.schema, role, r.actor.workspace_id)
    other = McpDiscoveryService(r.env.other_db, setup_token=lambda: TOKEN)
    for actor in (sibling, r.env.other_context):
        assert other.bindings(actor)["items"] == []
        assert other.attempts(actor)["items"] == []
        with pytest.raises(ScopeDeniedError):
            other.binding(actor, UUID(binding["id"]))
        with r.env.other_db.transaction(actor) as conn:
            for table in (
                "mcp_discovery_bindings",
                "mcp_discovery_attempts",
                "mcp_discovery_snapshots",
                "mcp_discovery_events",
            ):
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == []


def test_request_claim_and_cancellation_are_fenced_durable_commands(rig):
    r = rig
    before = current(r)
    attempt = request(r)
    assert attempt["status"] == "requested"
    assert attempt["requestedBindingSha256"] == before["bindingSha256"]
    assert (
        r.service.request(
            r.actor,
            UUID(before["id"]),
            before["revision"],
            before["bindingSha256"],
            "request",
            setup_token=TOKEN,
        )["attempt"]
        == attempt
    )
    with pytest.raises(ScopeDeniedError):
        request(r, key="competing-request")
    claimed = claim(r, attempt)
    assert r.service.validate_claim(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]))
    assert claim(r, attempt) == claimed
    running = r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]
    assert running["status"] == "running"
    cancelled = r.service.cancel(
        r.actor,
        UUID(running["id"]),
        running["revision"],
        running["bindingSha256"],
        "cancel",
        setup_token=TOKEN,
    )["attempt"]
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ScopeDeniedError):
        r.service.validate_claim(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]))
    assert current(r)["activeAttemptId"] is None
    assert request(r, key="after-cancellation")["id"] != attempt["id"]


@pytest.mark.parametrize("action", ["disable", "revoke"])
def test_binding_controls_fence_running_attempts_without_native_redispatch(rig, action):
    r = rig
    attempt = request(r)
    claimed = claim(r, attempt)
    controlled = control(r, action)
    assert controlled["state"] == ("disabled" if action == "disable" else "revoked")
    with pytest.raises(DomainError):
        r.service.validate_claim(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]))
    with pytest.raises(DomainError):
        request(r, key="forbidden-request")
    if action == "disable":
        enabled = control(r, "enable")
        assert enabled["state"] == "enabled"
    else:
        assert controlled["actions"] == []
        with pytest.raises(DomainError):
            control(r, "enable", key="revocation-is-terminal")


def test_rotated_native_key_cannot_reapprove_old_authority(rig):
    r = rig
    r.token[0] = "new-independent-native-fixture-" + "b" * 48
    stale = current(r)
    assert not stale["approvalValid"] and stale["actions"] == ["revoke"]
    for action in ("disable", "enable"):
        with pytest.raises(DomainError):
            control(r, action)
    with pytest.raises(ScopeDeniedError):
        request(r)
    assert control(r, "revoke")["state"] == "revoked"


def test_claim_recovery_returns_current_state_without_reusing_dispatch_authority(rig):
    r = rig
    attempt = request(r)
    inputs = (r.actor, UUID(attempt["id"]), attempt["revision"], attempt["bindingSha256"], "claim")
    assert r.service.execution_replay(*inputs, setup_token=TOKEN) is None
    claimed = claim(r, attempt, fresh_only=True)
    assert "workerToken" in claimed
    recovered = r.service.execution_replay(*inputs, setup_token=TOKEN)
    assert recovered["attempt"]["status"] == "running"
    assert "workerToken" not in str(recovered)
    assert claim(r, attempt, fresh_only=True) == {"replay": recovered}
    with pytest.raises(RevisionConflictError):
        r.service.execution_replay(*(*inputs[:3], "0" * 64, "claim"), setup_token=TOKEN)
    control(r, "disable")
    recovered = r.service.execution_replay(*inputs, setup_token=TOKEN)
    assert recovered["attempt"]["status"] == "cancelled"
    assert claim(r, attempt, fresh_only=True) == {"replay": recovered}
    with pytest.raises(ScopeDeniedError):
        r.service.validate_claim(r.actor, UUID(claimed["id"]), UUID(claimed["workerToken"]))


def test_concurrent_native_claims_have_one_dispatch_winner(rig):
    r = rig
    attempt = request(r)

    def acquire(key):
        try:
            return claim(r, attempt, key=key, fresh_only=True)
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(acquire, ("worker-one", "worker-two")))
    winners = [value for value in results if isinstance(value, dict)]
    assert len(winners) == 1 and "workerToken" in winners[0]
    assert sum(isinstance(value, DomainError) for value in results) == 1
    with r.env.db.transaction(r.actor) as conn:
        events = conn.execute(
            "SELECT action FROM mcp_discovery_events WHERE subject_kind='attempt' "
            "AND subject_id=%s ORDER BY revision",
            (UUID(attempt["id"]),),
        ).fetchall()
    assert [value["action"] for value in events] == ["requested", "claimed"]


@pytest.mark.parametrize("target", ["binding", "attempt"])
def test_unsigned_rows_and_events_cannot_become_live_discovery_authority(rig, target):
    r = rig
    attempt = request(r)
    claimed = claim(r, attempt)
    with r.env.db.transaction(r.actor) as conn:
        if target == "binding":
            row = conn.execute(
                "UPDATE mcp_discovery_bindings SET revision=revision+1,"
                "config=jsonb_set(config,'{configurationSha256}',%s) WHERE id=%s RETURNING *",
                (Jsonb("0" * 64), UUID(r.binding["id"])),
            ).fetchone()
        else:
            row = conn.execute(
                "UPDATE mcp_discovery_attempts SET revision=revision+1,"
                "payload=jsonb_set(payload,'{installationSha256}',%s) WHERE id=%s RETURNING *",
                (Jsonb("0" * 64), UUID(attempt["id"])),
            ).fetchone()
        conn.execute(
            "INSERT INTO mcp_discovery_events(workspace_id,id,principal_id,subject_kind,"
            "subject_id,revision,action,binding_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                r.actor.workspace_id,
                uuid4(),
                r.actor.principal_id,
                target,
                row["id"],
                row["revision"],
                "unsigned_fixture_observation",
                content_hash(_body(target, row)),
            ),
        )
    with pytest.raises(ScopeDeniedError):
        r.service.validate_claim(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]))
    if target == "binding":
        forged = current(r)
        assert not forged["approvalValid"] and forged["actions"] == ["revoke"]
        with pytest.raises(ScopeDeniedError):
            control(r, "disable")
        with pytest.raises(ScopeDeniedError):
            request(r, key="forged-request")
        assert control(r, "revoke")["actions"] == []
    else:
        with pytest.raises(ScopeDeniedError):
            r.service.fail(
                r.actor,
                UUID(attempt["id"]),
                UUID(claimed["workerToken"]),
                "reconciliation_failed",
            )
    with r.env.db.transaction(r.actor) as conn:
        assert conn.execute("SELECT * FROM mcp_discovery_snapshots").fetchall() == []


def test_restoring_an_old_signed_binding_does_not_restore_current_authority(rig):
    r = rig
    with r.env.db.transaction(r.actor) as conn:
        original = conn.execute(
            "SELECT * FROM mcp_discovery_bindings WHERE id=%s", (UUID(r.binding["id"]),)
        ).fetchone()
    control(r, "disable")
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "UPDATE mcp_discovery_bindings SET revision=%s,state=%s,last_error_code=%s,mac=%s "
            "WHERE id=%s",
            (
                original["revision"],
                original["state"],
                original["last_error_code"],
                original["mac"],
                original["id"],
            ),
        )
    assert not current(r)["approvalValid"]
    with pytest.raises(ScopeDeniedError):
        request(r)


def test_live_binding_limit_recycles_only_terminal_revocations(rig):
    r = rig
    for index in range(1, MAX_BINDINGS):
        admit(r, key=f"admit-{index}", name=f"Fixture {index}")
    assert len(r.service.bindings(r.actor, limit=50)["items"]) == MAX_BINDINGS
    with pytest.raises(InvalidInputError):
        admit(r, key="overflow")
    control(r, "disable")
    with pytest.raises(InvalidInputError):
        admit(r, key="disabled-is-still-retained")
    control(r, "revoke")
    replacement = admit(r, key="replacement")
    assert replacement["id"] != r.binding["id"]
    assert len(r.service.bindings(r.actor, limit=50)["items"]) == MAX_BINDINGS + 1


def test_real_collector_and_native_report_publish_one_complete_retained_catalogue(rig):
    r = rig
    attempt = complete(r)
    binding = current(r)
    assert attempt["status"] == "succeeded"
    assert attempt["changes"] == {"added": 5, "removed": 0, "changed": 0, "unchanged": 0}
    assert not binding["stale"] and binding["snapshotId"] == attempt["snapshotId"]
    assert binding["counts"] == {"tools": 2, "resources": 1, "resourceTemplates": 1, "prompts": 1}
    assert datetime.fromisoformat(binding["expiresAt"]) - datetime.fromisoformat(
        binding["fetchedAt"]
    ) == timedelta(seconds=60)
    retained = entries(r)["items"]
    assert len(retained) == 5 and all(value["executable"] is False for value in retained)
    assert {value["kind"] for value in retained} == {
        "tool",
        "resource",
        "resource_template",
        "prompt",
    }
    assert sum(value["configuredTool"] for value in retained) == 1
    selected = reference(r)
    assert (
        r.service.validate_reference(r.actor, selected, installed=r.installed)["name"] == "search"
    )
    for kind, name in (
        ("tool", "summarize"),
        ("resource", "document"),
        ("resource_template", "documents"),
        ("prompt", "review"),
    ):
        metadata = reference(r, kind=kind, name=name)
        assert r.service.validate_reference(r.actor, metadata)["executable"] is False
        with pytest.raises(ScopeDeniedError):
            r.service.validate_reference(r.actor, metadata, installed=r.installed)
    assert [value["method"] for value in r.peers[0].calls] == [
        "server/discover",
        "tools/list",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "prompts/list",
    ]
    with r.env.db.transaction(r.actor) as conn:
        snapshot = conn.execute("SELECT * FROM mcp_discovery_snapshots").fetchone()
    assert snapshot["observation"]["processesStopped"] is True
    assert snapshot["observation"]["executableSha256"] == r.installed.executable_sha256
    assert snapshot["observation"]["agentHomeRevision"] == 1
    assert snapshot["observation"]["installationSha256"] == selected["installationSha256"]
    assert r.runtime.ledger.active(r.actor.workspace_id) == []


def test_ttl_expiry_invalidates_references_but_retains_catalogue_evidence(rig):
    r = rig
    complete(r)
    original, selected = entries(r)["items"], reference(r)
    r.clock[0] += timedelta(seconds=60)
    assert current(r)["stale"]
    retained = entries(r)["items"]
    assert [value["definition"] for value in retained] == [
        value["definition"] for value in original
    ]
    assert all(not value["referenceAvailable"] for value in retained)
    with pytest.raises(ScopeDeniedError):
        r.service.validate_reference(r.actor, selected, installed=r.installed)
    with pytest.raises(ScopeDeniedError):
        reference(r)
    complete(r, key="after-expiry")
    assert not current(r)["stale"]
    assert reference(r)["snapshotId"] != selected["snapshotId"]
    with pytest.raises(ScopeDeniedError):
        r.service.validate_reference(r.actor, selected)


@pytest.mark.parametrize(
    "reason", ["reconciliation_failed", "reconciliation_indeterminate", "catalog_changed"]
)
def test_failed_refresh_keeps_prior_bytes_stale_and_replays_without_native_work(rig, reason):
    r = rig
    complete(r)
    original, selected = current(r), reference(r)
    attempt = request(r, key="failed-refresh-request")
    assert current(r)["stale"]
    with pytest.raises(ScopeDeniedError):
        r.service.validate_reference(r.actor, selected)
    claimed = claim(r, attempt, key="failed-refresh-claim")
    failed = r.service.fail(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), reason)[
        "attempt"
    ]
    assert failed["status"] == (
        "indeterminate" if reason == "reconciliation_indeterminate" else "failed"
    )
    assert failed["reasonCode"] == reason
    stale = current(r)
    assert stale["snapshotId"] == original["snapshotId"] and stale["stale"]
    assert stale["lastErrorCode"] == reason and stale["activeAttemptId"] is None
    assert len(entries(r)["items"]) == 5 and len(r.peers) == 1
    assert (
        claim(r, attempt, key="failed-refresh-claim", fresh_only=True)["replay"]["attempt"]
        == failed
    )
    with pytest.raises(ScopeDeniedError):
        r.service.validate_claim(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]))


def test_schema_change_removal_and_readdition_never_revive_an_old_reference(rig):
    r = rig
    complete(r)
    original = reference(r)
    changed = complete(r, key="changed", variant="changed_schema")
    assert changed["changes"] == {"added": 0, "removed": 0, "changed": 1, "unchanged": 4}
    altered = reference(r)
    assert altered["definitionSha256"] != original["definitionSha256"]
    assert not r.service.validate_reference(r.actor, altered)["configuredTool"]
    for selected in (original, altered):
        with pytest.raises(ScopeDeniedError):
            r.service.validate_reference(r.actor, selected, installed=r.installed)
    removed = complete(r, key="removed", variant="removed_tool")
    assert removed["changes"] == {"added": 0, "removed": 1, "changed": 0, "unchanged": 4}
    assert [value["name"] for value in entries(r, kind="tool")["items"]] == ["summarize"]
    for selected in (original, altered):
        with pytest.raises(ScopeDeniedError):
            r.service.validate_reference(r.actor, selected)
    restored = complete(r, key="restored")
    assert restored["changes"] == {"added": 1, "removed": 0, "changed": 0, "unchanged": 4}
    latest = reference(r)
    assert latest["snapshotSha256"] == original["snapshotSha256"]
    assert latest["snapshotId"] != original["snapshotId"]
    assert r.service.validate_reference(r.actor, latest, installed=r.installed)["configuredTool"]
    with pytest.raises(ScopeDeniedError):
        r.service.validate_reference(r.actor, original, installed=r.installed)


def test_entry_pagination_is_bounded_and_rejects_a_cursor_from_an_old_snapshot(rig):
    r = rig
    complete(r)
    page = entries(r, limit=2)
    assert len(page["items"]) == 2 and page["nextCursor"] == page["items"][-1]["id"]
    second = entries(r, limit=2, after=page["nextCursor"])
    third = entries(r, limit=2, after=second["nextCursor"])
    assert len(third["items"]) == 1 and third["nextCursor"] is None
    assert len({value["id"] for part in (page, second, third) for value in part["items"]}) == 5
    assert [value["name"] for value in entries(r, query="SEARCH FIXTURE")["items"]] == ["search"]
    complete(r, key="unchanged-refresh", variant="reverse_order")
    with pytest.raises(RevisionConflictError):
        entries(r, after=page["nextCursor"])
    for options in (
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"after": "bad"},
        {"kind": "executable"},
        {"query": "x" * 257},
    ):
        with pytest.raises(InvalidInputError):
            entries(r, **options)


@pytest.mark.parametrize(
    "variant", ["notification", "cursor_loop", "duplicate_identity", "missing_collection"]
)
def test_partial_or_invalidated_collection_is_never_committed(rig, variant):
    r = rig
    complete(r)
    original = current(r)
    attempt = request(r, key="bad-refresh")
    claimed = claim(r, attempt, key="bad-refresh-claim")
    r.variant = variant
    with pytest.raises(DomainError):
        observe(r, attempt, claimed)
    r.service.fail(r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), "catalog_changed")
    assert current(r)["snapshotId"] == original["snapshotId"]
    assert current(r)["stale"]
    with r.env.db.transaction(r.actor) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM mcp_discovery_snapshots").fetchone()["n"] == 1
        )


def test_revocation_between_metadata_exchanges_stops_publication(rig):
    r = rig
    attempt, claimed = request(r), None
    claimed = claim(r, attempt)

    def revoke(request):
        if request["method"] == "tools/list":
            control(r, "revoke")

    r.peer_hook = revoke
    with pytest.raises(ScopeDeniedError):
        observe(r, attempt, claimed)
    assert current(r)["state"] == "revoked"
    assert r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]["status"] == "cancelled"
    assert [value["method"] for value in r.peers[0].calls] == ["server/discover", "tools/list"]
    assert entries(r)["items"] == []


@pytest.mark.parametrize(
    "change",
    [
        "workspaceId",
        "principalId",
        "agentId",
        "installationSha256",
        "configurationSha256",
        "definitionSha256",
        "executableSha256",
        "invalidRuntime",
        "badHomeRevision",
        "notStopped",
        "extraField",
        "naiveTime",
        "futureTime",
        "longLifetime",
        "oldCompletion",
    ],
)
def test_complete_requires_exact_fresh_native_lifetime_evidence(rig, change):
    r = rig
    attempt = request(r)
    claimed = claim(r, attempt)
    observed, native = observe(r, attempt, claimed)
    if change in {"workspaceId", "principalId", "agentId"}:
        native[change] = str(uuid4())
    elif change.endswith("Sha256"):
        native[change] = "0" * 64
    elif change == "invalidRuntime":
        native["runtimeSha256"] = "not-a-digest"
    elif change == "badHomeRevision":
        native["agentHomeRevision"] = True
    elif change == "notStopped":
        native["processesStopped"] = False
    elif change == "extraField":
        native["fixtureUncheckedAuthority"] = True
    elif change == "naiveTime":
        native["observedAt"] = r.clock[0].replace(tzinfo=None).isoformat()
    elif change == "futureTime":
        native["completedAt"] = (r.clock[0] + timedelta(seconds=1)).isoformat()
    elif change == "longLifetime":
        native["observedAt"] = (r.clock[0] - timedelta(seconds=121)).isoformat()
    elif change == "oldCompletion":
        native["observedAt"] = native["completedAt"] = (
            r.clock[0] - timedelta(seconds=31)
        ).isoformat()
    with pytest.raises(DomainError):
        r.service.complete(
            r.actor, UUID(attempt["id"]), UUID(claimed["workerToken"]), observed, native
        )
    assert current(r)["snapshotId"] is None
    assert r.service.attempt(r.actor, UUID(attempt["id"]))["attempt"]["status"] == "running"


def test_reference_fields_scope_and_current_installed_owner_are_exact(rig):
    r = rig
    complete(r)
    selected = reference(r)
    for key in selected:
        altered = {**selected, key: "different-fixture-value"}
        with pytest.raises(DomainError):
            r.service.validate_reference(r.actor, altered, installed=r.installed)
    with pytest.raises(ScopeDeniedError):
        r.service.validate_reference(r.actor, {**selected, "unexpected": True})
    with pytest.raises(ScopeDeniedError):
        r.service.validate_reference(r.env.other_context, selected)
    for owner in (
        replace(r.installed, discovery_binding_id=uuid4()),
        replace(r.installed, discovery_binding_id=None, discovery_configuration_sha256=None),
        replace(r.installed, discovery_configuration_sha256="0" * 64),
        installed_fixture(r.actor, linked=UUID(r.binding["id"]), executable_sha256="0" * 64),
        replace(
            r.installed,
            installation=replace(r.installed.installation, policy_revision_hash="0" * 64),
        ),
    ):
        with pytest.raises(ScopeDeniedError):
            r.service.validate_reference(r.actor, selected, installed=owner)


def test_actual_http_lifecycle_exports_only_public_mcp_contracts(rig):
    r = rig
    app, origin = FastAPI(), "http://127.0.0.1:3100"
    reads = {"Authorization": "Bearer mcp-discovery-fixture"}
    writes = {**reads, "Origin": origin, "X-Potbot-Setup-Token": TOKEN}

    def authenticate(value):
        if value != reads["Authorization"]:
            raise ScopeDeniedError("Unknown fixture identity")
        return r.actor

    def authorize(value):
        if value != r.token[0]:
            raise ScopeDeniedError("Missing fixture setup authority")

    @app.exception_handler(DomainError)
    async def failed(request, error):
        response = error_response(
            error,
            semantics=OperationSemantics(
                read_only=request.method == "GET",
                idempotency_key=getattr(request.state, "idempotency_key", None) is not None,
            ),
        )
        return JSONResponse(response.body, status_code=response.status)

    register_mcp_discovery_routes(
        app, r.env.db, authenticate, (origin,), r.actor.workspace_id, authorize, service=r.service
    )
    fixture = {"requests": {}}
    with TestClient(app) as client:

        def get(suffix):
            response = client.get("/v1/ui/mcp-discovery" + suffix, headers=reads)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            return response.json()

        def post(suffix, body, key):
            response = client.post(
                "/v1/ui/mcp-discovery" + suffix,
                headers={**writes, "Idempotency-Key": key},
                json=body,
            )
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            return response.json()

        def change(collection, row, action, key, *, save=False):
            body = {"revision": row["revision"], "digest": row["bindingSha256"], "confirm": True}
            if save:
                fixture["requests"][action] = body
            suffix = f"/{collection}/{row['id']}/{action}"
            result = post(suffix, body, key)
            assert post(suffix, body, key) == result
            return result

        identity = r.binding["id"]
        fixture["bindings"] = get("/bindings")
        fixture["binding"] = get("/bindings/" + identity)
        fixture["emptyEntries"] = get(f"/bindings/{identity}/entries")
        fixture["emptyAttempts"] = get("/attempts")
        fixture["requestedAttempt"] = change(
            "bindings", r.binding, "reconcile", "http-reconcile", save=True
        )
        attempt = fixture["requestedAttempt"]["attempt"]
        claimed = claim(r, attempt, key="http-native-claim")
        fixture["runningAttempt"] = get("/attempts/" + attempt["id"])
        completed = complete(r, attempt=attempt, claimed=claimed)
        fixture["freshBinding"] = get("/bindings/" + identity)
        fixture["entries"] = get(f"/bindings/{identity}/entries")
        for key, kind in (
            ("tools", "tool"),
            ("resources", "resource"),
            ("templates", "resource_template"),
            ("prompts", "prompt"),
        ):
            fixture[key] = get(f"/bindings/{identity}/entries?kind={kind}")
        fixture["search"] = get(f"/bindings/{identity}/entries?q=Search%20fixture")
        fixture["firstPage"] = get(f"/bindings/{identity}/entries?limit=2")
        cursor = fixture["firstPage"]["nextCursor"]
        fixture["secondPage"] = get(f"/bindings/{identity}/entries?limit=2&after={cursor}")
        entry_id = fixture["search"]["items"][0]["id"]
        fixture["reference"] = get(f"/bindings/{identity}/entries/{entry_id}/reference")
        fixture["succeededAttempt"] = get("/attempts/" + completed["id"])
        fixture["attempts"] = get("/attempts?bindingId=" + identity)
        assert len(r.peers) == 1
        r.clock[0] += timedelta(seconds=60)
        fixture["staleBinding"] = get("/bindings/" + identity)
        fixture["staleEntries"] = get(f"/bindings/{identity}/entries")
        pending = change(
            "bindings", fixture["staleBinding"]["binding"], "reconcile", "http-failed-refresh"
        )["attempt"]
        private = claim(r, pending, key="http-failed-claim")
        r.service.fail(
            r.actor, UUID(pending["id"]), UUID(private["workerToken"]), "reconciliation_failed"
        )
        fixture["failedAttempt"] = get("/attempts/" + pending["id"])
        fixture["disabledBinding"] = change(
            "bindings", current(r), "disable", "http-disable", save=True
        )
        fixture["enabledBinding"] = change(
            "bindings", fixture["disabledBinding"]["binding"], "enable", "http-enable", save=True
        )
        pending = change(
            "bindings", fixture["enabledBinding"]["binding"], "reconcile", "http-cancel-review"
        )["attempt"]
        fixture["cancelledAttempt"] = change(
            "attempts", pending, "cancel", "http-cancel", save=True
        )
        fixture["revokedBinding"] = change(
            "bindings", current(r), "revoke", "http-revoke", save=True
        )
        assert len(r.peers) == 1

    encoded = json.dumps(fixture, sort_keys=True, indent=2)
    for forbidden in (
        TOKEN,
        "workerToken",
        "runtimeSha256",
        "stopReceiptSha256",
        str(r.runtime.rootfs),
    ):
        assert forbidden not in encoded
    if destination := os.environ.get("POTBOT_MCP_DISCOVERY_CONTRACT_OUTPUT"):
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")


def test_populated_v42_migration_preserves_registry_authority_and_checksums(app_env, tmp_path):
    schema, migrations = "mcp_discovery_upgrade", application_migrations()
    assert migrate(app_env.admin_dsn, schema, migrations[:42]) == tuple(range(1, 43))
    workspace = provision_workspace(
        app_env.admin_dsn, schema, "MCP upgrade", sandbox_root=tmp_path / "legacy"
    )
    role = urlsplit(app_env.db.dsn).username
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    database = Database(app_env.db.dsn, schema)
    registry = DiscoveryService(database, setup_token=lambda: TOKEN)
    source = registry.add_source(
        actor,
        {
            "adapter": "curated",
            "name": "Retained registry source",
            "ttlSeconds": 900,
            "curatedManifestSha256": "a" * 64,
        },
        "preserved-source",
        setup_token=TOKEN,
    )["source"]
    with database.transaction(actor) as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("discovery_sources", "discovery_events")
        }
    with admin_connection(app_env.admin_dsn, schema) as conn:
        checksums = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert migrate(app_env.admin_dsn, schema, migrations[:43]) == tuple(range(1, 44))
    grant_application_role(app_env.admin_dsn, schema, role)
    assert migrate(app_env.admin_dsn, schema, migrations[:43]) == tuple(range(1, 44))
    with database.transaction(actor) as conn:
        for table, retained in before.items():
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == retained
        for table in (
            "mcp_discovery_bindings",
            "mcp_discovery_attempts",
            "mcp_discovery_snapshots",
            "mcp_discovery_events",
        ):
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == []
    with admin_connection(app_env.admin_dsn, schema) as conn:
        assert (
            conn.execute(
                "SELECT * FROM schema_migrations WHERE version<=42 ORDER BY version"
            ).fetchall()
            == checksums
        )
    retained = registry.source(actor, UUID(source["id"]))["source"]
    assert retained["id"] == source["id"] and retained["approvalValid"]
