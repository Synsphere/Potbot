"""Genuine PostgreSQL/RLS and native-only grant revocation, with actual HTTP reads."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_settings import register_settings_routes
from potbot.controller.remote_workspaces import (
    current_control_grant,
    install_control_grant,
    install_workspace_owner,
    register_node,
)
from potbot.controller.settings import SettingsService
from potbot.controller.swarm_requests import SCHEMA, apply_revocation, request_revocation
from potbot.domain.configuration import resolve_configuration
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.domain.remote_workspaces import RemoteNode, WorkspaceControlGrant
from potbot.domain.serialization import content_hash
from potbot.models.configuration import store_configuration
from potbot.persistence.database import admin_connection, provision_principal, provision_workspace
from potbot.tools.local_inventory import InventoryProfile

pytestmark = pytest.mark.postgres
ORIGIN = "http://localhost:3100"


def seed(environment):
    owner, source = environment.context, environment.other_context
    identity = uuid4()
    wire = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    node = RemoteNode(
        identity,
        owner.workspace_id,
        owner.principal_id,
        "fixture.example.invalid",
        22,
        "operator",
        "ssh-ed25519 " + base64.b64encode(wire).decode(),
        "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip("="),
        f"secret://remote-nodes/{identity}/ssh",
    )
    grant = WorkspaceControlGrant(
        uuid4(),
        owner.workspace_id,
        owner.principal_id,
        source.workspace_id,
        source.principal_id,
        ("workspace.inspect", "tool.execute"),
        datetime.now(UTC) + timedelta(hours=1),
    )
    with admin_connection(environment.admin_dsn, environment.schema) as connection:
        for actor in (owner, source):
            install_workspace_owner(connection, actor.workspace_id, actor.principal_id)
        register_node(connection, node)
        install_control_grant(connection, grant)
    return node, grant


def reviewed(grant):
    return {
        "schema": SCHEMA,
        "grantId": str(grant.id),
        "expectedRevision": grant.revision,
        "expectedGeneration": grant.revocation_generation,
        "grantSha256": content_hash(grant.to_dict()),
    }


def client_for(environment, profile=None):
    context = environment.context
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def errors(_request, error):
        return JSONResponse({"error": error.code.value}, status_code=403)

    def authenticate(value):
        if value != "Bearer fixture-workspace":
            raise ScopeDeniedError("Unavailable")
        return context

    def setup(value):
        if value != "fixture-setup":
            raise ScopeDeniedError("Unavailable")

    register_settings_routes(
        app, environment.db, authenticate, (ORIGIN,), context.workspace_id, setup, profile
    )
    return TestClient(app)


def test_real_http_inventory_and_snapshots_keep_private_paths_and_refs_out(app_env, tmp_path):
    node, grant = seed(app_env)
    project = tmp_path / "inventory-project"
    (project / "models").mkdir(parents=True)
    (project / "packages" / "fixture-1.dist-info").mkdir(parents=True)
    (project / "models" / "fixture.gguf").write_bytes(b"fixture bytes, never inference weights")
    (project / "packages" / "fixture-1.dist-info" / "METADATA").write_text(
        "Name: fixture-tool\nVersion: 1.0\n\nprivate body"
    )
    selected = InventoryProfile(
        app_env.context.workspace_id, project, "Local fixture", ("models",), ("packages",)
    )
    with app_env.db.transaction(app_env.context) as connection:
        store_configuration(
            connection,
            app_env.context,
            resolve_configuration(
                {
                    "workspace": {
                        "secret_refs": {"provider": "secret://providers/hidden-account"},
                        "source_access": ["/private/source"],
                    },
                }
            ),
        )
    client = client_for(app_env, selected)
    headers = {"Authorization": "Bearer fixture-workspace"}
    paths = {
        "catalogue": "/v1/ui/settings/catalogue",
        "effective": "/v1/ui/settings/effective",
        "models": "/v1/ui/inventory/models",
        "tools": "/v1/ui/inventory/tools",
        "swarm": "/v1/ui/swarm/nodes",
    }
    records = {}
    for kind, path in paths.items():
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert str(project) not in response.text
        assert "hidden-account" not in response.text and "/private/source" not in response.text
        assert node.credential_reference not in response.text
        records[kind] = response.json()
    assert records["effective"]["liveLayersAvailable"] is False
    assert records["effective"]["latestRecordedSnapshot"] is not None
    assert records["models"]["items"][0]["presence"] == "observed"
    assert records["models"]["items"][0]["qualification"] == "unknown"
    assert records["tools"]["items"][0]["name"] == "fixture-tool"
    assert records["swarm"]["nodes"][0]["runtimeQualified"] is False
    assert records["swarm"]["grants"][0]["id"] == str(grant.id)
    # Cross-language CI contract: these are actual authenticated SQL-backed HTTP responses.
    output = Path("artifacts/configuration/settings-http.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, indent=2), encoding="utf-8")


def test_registered_nodes_and_grants_do_not_cross_workspace_boundaries(app_env):
    seed(app_env)
    result = SettingsService(app_env.other_db).swarm(app_env.other_context, request_available=True)
    assert result["nodes"] == [] and result["grants"] == [] and result["requests"] == []
    assert result["liveCapacityAvailable"] is False
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            # Restricted application roles still cannot mutate remote authority.
            connection.execute(
                "DELETE FROM remote_nodes WHERE workspace_id=%s", (app_env.context.workspace_id,)
            )


def test_owner_request_is_not_revocation_and_native_apply_is_exactly_replayable(app_env):
    _node, grant = seed(app_env)
    identity = uuid4()
    value = reviewed(grant)
    with app_env.db.transaction(app_env.context) as connection:
        first = request_revocation(connection, app_env.context, identity, value)
        assert first == request_revocation(connection, app_env.context, identity, value)
        assert not current_control_grant(connection, grant.workspace_id, grant.id).revoked
    service = SettingsService(app_env.db)
    assert (
        service.swarm(app_env.context, request_available=True)["requests"][0]["state"]
        == "requested"
    )
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        result = apply_revocation(
            connection,
            workspace_id=grant.workspace_id,
            owner_principal_id=app_env.context.principal_id,
            request_id=identity,
        )
        assert result == apply_revocation(
            connection,
            workspace_id=grant.workspace_id,
            owner_principal_id=app_env.context.principal_id,
            request_id=identity,
        )
    observed = service.swarm(app_env.context, request_available=True)
    assert observed["grants"][0]["state"] == "revoked"
    assert observed["requests"][0]["state"] == "applied"
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM workspace_control_grant_revisions WHERE workspace_id=%s AND id=%s",
                (grant.workspace_id, grant.id),
            ).fetchone()["n"]
            == 2
        )


def test_changed_review_and_foreign_requests_fail_without_replacing_the_original(app_env):
    _node, grant = seed(app_env)
    identity = uuid4()
    with app_env.db.transaction(app_env.context) as connection:
        request_revocation(connection, app_env.context, identity, reviewed(grant))
        with pytest.raises(RevisionConflictError):
            request_revocation(
                connection, app_env.context, identity, reviewed(grant) | {"grantSha256": "f" * 64}
            )
    with app_env.other_db.transaction(app_env.other_context) as connection:
        with pytest.raises((ScopeDeniedError, RevisionConflictError)):
            request_revocation(connection, app_env.other_context, uuid4(), reviewed(grant))
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        install_control_grant(
            connection,
            replace(grant, revision=2, revocation_generation=2, permissions=("workspace.inspect",)),
        )
        with pytest.raises(RevisionConflictError):
            apply_revocation(
                connection,
                workspace_id=grant.workspace_id,
                owner_principal_id=app_env.context.principal_id,
                request_id=identity,
            )
    result = SettingsService(app_env.db).swarm(app_env.context, request_available=True)
    assert result["requests"][0]["state"] == "needs_review"
    assert result["grants"][0]["state"] == "active"


def test_forged_completion_journal_cannot_claim_a_grant_has_been_revoked(app_env):
    from psycopg.types.json import Jsonb

    _node, grant = seed(app_env)
    identity = uuid4()
    with app_env.db.transaction(app_env.context) as connection:
        request_revocation(connection, app_env.context, identity, reviewed(grant))
        connection.execute(
            "INSERT INTO command_results(workspace_id,id,operation,idempotency_key,request_hash,"
            "result,principal_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                app_env.context.workspace_id,
                uuid4(),
                "swarm.revoke.complete",
                str(identity),
                "a" * 64,
                Jsonb({"revoked": True}),
                app_env.context.principal_id,
            ),
        )
    result = SettingsService(app_env.db).swarm(app_env.context, request_available=True)
    assert result["requests"][0]["state"] == "requested"
    assert result["grants"][0]["state"] == "active"


def test_settings_recheck_current_membership_and_do_not_relabel_local_roots(app_env, tmp_path):
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    extra = provision_workspace(app_env.admin_dsn, app_env.schema, "temporary-read-scope")
    context = provision_principal(app_env.admin_dsn, app_env.schema, role, extra)
    service = SettingsService(app_env.db)
    assert service.catalogue(context)["workspaceId"] == str(extra)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        connection.execute(
            "DELETE FROM workspace_memberships WHERE workspace_id=%s AND principal_id=%s",
            (extra, context.principal_id),
        )
    with pytest.raises(ScopeDeniedError):
        service.catalogue(context)
    profile = InventoryProfile(app_env.context.workspace_id, tmp_path, "Pinned local", (), ())
    with pytest.raises(ScopeDeniedError):
        SettingsService(app_env.other_db, profile).inventory_view(app_env.other_context, "models")


def test_tampered_owner_intent_is_not_native_authority(app_env):
    from psycopg.types.json import Jsonb

    _node, grant = seed(app_env)
    identity = uuid4()
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute(
            "INSERT INTO command_results(workspace_id,id,operation,idempotency_key,request_hash,"
            "result,principal_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                app_env.context.workspace_id,
                identity,
                "swarm.revoke.request",
                str(identity),
                "a" * 64,
                Jsonb(reviewed(grant)),
                app_env.context.principal_id,
            ),
        )
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        with pytest.raises(InvalidInputError):
            apply_revocation(
                connection,
                workspace_id=grant.workspace_id,
                owner_principal_id=app_env.context.principal_id,
                request_id=identity,
            )
    result = SettingsService(app_env.db).swarm(app_env.context, request_available=True)
    assert result["requests"][0]["state"] == "needs_review"
    assert result["grants"][0]["state"] == "active"
