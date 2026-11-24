"""Native planning contracts and direct authority fail closed without a runtime."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from potbot.api.remote_workspaces_cli import plan_from_file
from potbot.cli import main
from potbot.controller.remote_workspaces import creates_control_cycle
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.remote_workspaces import (
    NativeEnvironment,
    RemoteNode,
    WorkspaceControlGrant,
    WorkspaceDeployment,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.schema import application_migrations
from potbot.tools.remote_preflight import HostObservation, ReleasePin, provisioning_manifest


def node():
    identity = uuid4()
    wire = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    return RemoteNode(
        identity,
        uuid4(),
        uuid4(),
        "remote.example.invalid",
        22,
        "operator",
        "ssh-ed25519 " + base64.b64encode(wire).decode(),
        "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip("="),
        f"secret://remote-nodes/{identity}/ssh",
    )


def observation():
    return HostObservation(
        "Linux", "ubuntu", "24.04", "x86_64", "3.12.3", str(uuid4()), True, 4 * 1024**3
    )


def releases():
    return tuple(
        ReleasePin(name, "fixture-0", "a" * 64, "b" * 64, "c" * 64)
        for name in ("syn", "potbot-runner")
    )


def grant():
    return WorkspaceControlGrant(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        ("workspace.inspect", "tool.execute"),
        datetime.now(UTC) + timedelta(hours=1),
    )


def test_native_environment_changes_identity_without_claiming_sandbox_qualification():
    env = NativeEnvironment(
        "ubuntu", "24.04", "x86_64", "/opt/potbot/python", "a" * 64, "b" * 64, "c" * 64
    )
    assert env.to_dict()["execution_mode"] == "base_os"
    assert replace(env, dependencies_sha256="d" * 64).sha256 != env.sha256
    assert replace(env, device="cuda:0", driver="fixture-driver").sha256 != env.sha256
    assert "sandbox_image" not in env.to_dict()
    assert env.sha256 == content_hash(env.to_dict())


@pytest.mark.parametrize(
    "changes",
    [
        {"hostname": "-oProxyCommand=evil"},
        {"hostname": "host;command"},
        {"hostname": "name\nother"},
        {"hostname": "user@host"},
        {"hostname": "host/path"},
        {"username": "root;command"},
        {"port": True},
        {"port": 0},
        {"port": 65536},
        {"ssh_fingerprint": "SHA256:unverified"},
        {"ssh_public_key": "ssh-ed25519 AAAA"},
        {"ssh_public_key": "ssh-rsa AAAA"},
        {"credential_reference": "literal-private-key"},
        {"credential_reference": f"secret://remote-nodes/{uuid4()}/ssh"},
        {"generation": True},
    ],
)
def test_registration_rejects_unsafe_or_unpinned_inputs(changes):
    with pytest.raises(InvalidInputError):
        replace(node(), **changes)


@pytest.mark.parametrize("host", ["127.0.0.1", "2001:db8::1", "host.example.invalid"])
def test_registration_accepts_literal_addresses_and_dns_names(host):
    assert replace(node(), hostname=host).hostname == host


def test_grant_requires_exact_current_direct_authority():
    record = grant()
    ctx = WorkspaceContext(record.source_workspace_id, record.principal_id)
    request = dict(
        target_workspace_id=record.workspace_id,
        permission="tool.execute",
        expected_revision=1,
        revocation_generation=1,
        now=datetime.now(UTC),
    )
    record.authorize(ctx, **request)
    for changes in (
        {"target_workspace_id": uuid4()},
        {"permission": "apps.input"},
        {"expected_revision": 2},
        {"expected_revision": True},
        {"revocation_generation": 2},
        {"now": record.expires_at},
    ):
        with pytest.raises(ScopeDeniedError):
            record.authorize(ctx, **(request | changes))
    for forged in (
        WorkspaceContext(uuid4(), ctx.principal_id),
        WorkspaceContext(ctx.workspace_id, uuid4()),
    ):
        with pytest.raises(ScopeDeniedError):
            record.authorize(forged, **request)
    with pytest.raises(ScopeDeniedError):
        replace(record, revoked=True).authorize(ctx, **request)
    # B->C is a separate grant. A's authority over B supplies no authority over C.
    downstream = replace(
        record, id=uuid4(), source_workspace_id=record.workspace_id, workspace_id=uuid4()
    )
    with pytest.raises(ScopeDeniedError):
        downstream.authorize(ctx, **(request | {"target_workspace_id": downstream.workspace_id}))


@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": ("tool.execute", "tool.execute")},
        {"permissions": ("*")},
        {"permissions": ("grant.delegate",)},
        {"expires_at": datetime(2026, 11, 24)},
        {"revision": 0},
        {"revocation_generation": False},
        {"revoked": "false"},
    ],
)
def test_grant_rejects_ambiguous_authority(changes):
    with pytest.raises(InvalidInputError):
        replace(grant(), **changes)


def test_control_cycle_search_does_not_infer_transitive_permission():
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    assert creates_control_cycle(c, a, [(a, b), (b, c)])
    assert creates_control_cycle(a, a, [])
    assert not creates_control_cycle(c, d, [(a, b), (b, c)])


def test_manifest_is_deterministic_review_only_and_pins_native_project_storage():
    registration, facts, target = node(), observation(), uuid4()
    args = dict(target_workspace_id=target, project_root=f"/srv/potbot/{target}")
    first = provisioning_manifest(registration, facts, releases(), **args)
    assert first == provisioning_manifest(registration, facts, tuple(reversed(releases())), **args)
    manifest = first["manifest"]
    assert manifest["host_facts_compatible"]
    assert first["manifest_sha256"] == content_hash(manifest)
    assert not manifest["apply_available"]
    assert not any(manifest["capability_readiness"].values())
    assert manifest["models_root"] == args["project_root"] + "/models"
    assert manifest["observation_source"] == "supplied_host_facts"
    assert "authenticated_fresh_remote_host_inspection" in manifest["remaining_gates"]
    changed = provisioning_manifest(
        registration, replace(facts, boot_id=str(uuid4())), releases(), **args
    )
    assert changed["manifest_sha256"] != first["manifest_sha256"]


@pytest.mark.parametrize(
    "changes, blocker",
    [
        ({"system": "Windows"}, "initial_backend_requires_ubuntu_24_04_x86_64"),
        ({"architecture": "aarch64"}, "initial_backend_requires_ubuntu_24_04_x86_64"),
        ({"systemd_running": False}, "running_systemd_required"),
        ({"python_version": "3.11.9"}, "python_3_12_or_later_required"),
        ({"available_disk_bytes": 0}, "insufficient_installation_disk"),
        ({"boot_id": "unknown"}, "native_boot_identity_unavailable"),
    ],
)
def test_unsupported_or_incomplete_facts_cannot_pass_preflight(changes, blocker):
    plan = provisioning_manifest(
        node(),
        replace(observation(), **changes),
        releases(),
        target_workspace_id=uuid4(),
        project_root="/srv/potbot/fixture",
    )
    assert not plan["manifest"]["host_facts_compatible"]
    assert blocker in plan["manifest"]["host_fact_blockers"]
    assert not plan["manifest"]["apply_available"]


@pytest.mark.parametrize(
    "root", ["/", "/srv/../root", "relative", "C:\\workspace", "/srv//project", "/srv/project/"]
)
def test_deployment_rejects_ambiguous_native_roots(root):
    env = NativeEnvironment("ubuntu", "24.04", "x86_64", "python", "a" * 64, "b" * 64, "c" * 64)
    with pytest.raises(InvalidInputError):
        WorkspaceDeployment(uuid4(), uuid4(), uuid4(), uuid4(), root, "potbot-w-" + "a" * 16, env)


def test_cli_plan_roundtrip_and_duplicate_or_private_fields_fail_closed(tmp_path, capsys):
    from dataclasses import asdict

    fixture = {
        "node": node().to_dict(),
        "observation": asdict(observation()),
        "releases": [asdict(item) for item in releases()],
        "target_workspace_id": str(uuid4()),
        "project_root": "/srv/potbot/fixture",
    }
    path = tmp_path / "public-plan.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    expected = plan_from_file(path)
    assert main(["remote-workspace", "plan", "--input", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == expected
    path.write_text(json.dumps(fixture | {"private_key": "fixture-do-not-echo"}), encoding="utf-8")
    assert main(["remote-workspace", "plan", "--input", str(path)]) == 1
    failure = capsys.readouterr().out
    assert "fixture-do-not-echo" not in failure
    assert json.loads(failure)["error"]["code"] == "invalid_input"
    path.write_text('{"node":{},"node":{}}', encoding="utf-8")
    with pytest.raises(InvalidInputError, match="Duplicate"):
        plan_from_file(path)
    path.write_bytes(b"x" * 65_537)
    with pytest.raises(InvalidInputError, match="64 KiB"):
        plan_from_file(path)


def test_migration_is_additive_in_both_published_lineages():
    for lineage in ("main", "phase3-preview"):
        migrations = application_migrations(lineage=lineage)
        assert tuple(item.version for item in migrations) == tuple(range(1, 51))
        assert migrations[-1].name == "remote_workspace_schema"
        assert "isolation_requirement='required'" in migrations[13].statements[0]
        assert "workspace_sandboxes" not in migrations[-1].workspace_tables
