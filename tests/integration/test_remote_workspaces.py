"""Real PostgreSQL native registration, RLS, grant fencing and graph concurrency."""

from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from potbot.controller.remote_workspaces import (
    current_control_grant,
    install_control_grant,
    install_workspace_owner,
    record_provisioning_plan,
    register_deployment,
    register_node,
)
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.remote_workspaces import (
    NativeEnvironment,
    RemoteNode,
    WorkspaceControlGrant,
    WorkspaceDeployment,
)
from potbot.persistence.database import (
    admin_connection,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.remote_preflight import HostObservation, ReleasePin, provisioning_manifest

pytestmark = pytest.mark.postgres


def _node(context):
    identity = uuid4()
    wire = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    return RemoteNode(
        identity,
        context.workspace_id,
        context.principal_id,
        "fixture.example.invalid",
        22,
        "operator",
        "ssh-ed25519 " + base64.b64encode(wire).decode(),
        "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip("="),
        f"secret://remote-nodes/{identity}/ssh",
    )


def _grant(source, target):
    return WorkspaceControlGrant(
        uuid4(),
        target.workspace_id,
        target.principal_id,
        source.workspace_id,
        source.principal_id,
        ("workspace.inspect", "tool.execute"),
        datetime.now(UTC) + timedelta(hours=1),
    )


def _owners(environment):
    with admin_connection(environment.admin_dsn, environment.schema) as connection:
        for context in (environment.context, environment.other_context):
            install_workspace_owner(connection, context.workspace_id, context.principal_id)


def test_upgrade_retains_sandbox_policy_and_restricted_logins_cannot_self_install(app_env):
    _owners(app_env)
    assert migrate(app_env.admin_dsn) == tuple(item.version for item in application_migrations())
    with app_env.db.transaction(app_env.context) as connection:
        sandbox = connection.execute("SELECT * FROM workspace_sandboxes").fetchone()
        assert sandbox["isolation_requirement"] == "required"
        assert len(connection.execute("SELECT * FROM remote_workspace_owners").fetchall()) == 1
        connection.execute(
            "SELECT set_config('potbot.workspace_id',%s,true),"
            "set_config('potbot.principal_id',%s,true)",
            (str(app_env.other_context.workspace_id), str(app_env.other_context.principal_id)),
        )
        assert connection.execute("SELECT * FROM remote_workspace_owners").fetchall() == []
    for statement, parameters in (
        (
            "INSERT INTO remote_workspace_owners(workspace_id,principal_id) VALUES (%s,%s)",
            (app_env.context.workspace_id, app_env.context.principal_id),
        ),
        (
            "UPDATE remote_workspace_owners SET principal_id=%s WHERE workspace_id=%s",
            (app_env.context.principal_id, app_env.context.workspace_id),
        ),
        (
            "DELETE FROM remote_workspace_owners WHERE workspace_id=%s AND principal_id=%s",
            (app_env.context.workspace_id, app_env.context.principal_id),
        ),
    ):
        with pytest.raises(ScopeDeniedError):
            with app_env.db.transaction(app_env.context) as connection:
                connection.execute(statement, parameters)
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            register_node(connection, _node(app_env.context))


def test_node_registration_is_idempotent_and_pin_changes_need_explicit_rotation(app_env):
    _owners(app_env)
    node = _node(app_env.context)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        assert register_node(connection, node) == register_node(connection, node)
        with pytest.raises(RevisionConflictError):
            register_node(connection, replace(node, hostname="changed.example.invalid"))
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute("SELECT state FROM remote_nodes").fetchone()["state"] == "registered"
        )
    with app_env.other_db.transaction(app_env.other_context) as connection:
        assert connection.execute("SELECT * FROM remote_nodes").fetchall() == []


def test_latest_grant_fences_stale_replay_and_cycle_issuance(app_env):
    _owners(app_env)
    grant = _grant(app_env.context, app_env.other_context)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        assert install_control_grant(connection, grant) == grant.to_dict()
        assert install_control_grant(connection, grant) == grant.to_dict()
        with pytest.raises(ScopeDeniedError, match="cycles"):
            install_control_grant(connection, _grant(app_env.other_context, app_env.context))
    with app_env.db.transaction(app_env.context) as connection:
        with pytest.raises(ScopeDeniedError):
            current_control_grant(connection, grant.workspace_id, grant.id)
    with app_env.other_db.transaction(app_env.other_context) as connection:
        current = current_control_grant(connection, grant.workspace_id, grant.id)
        current.authorize(
            app_env.context,
            target_workspace_id=grant.workspace_id,
            permission="tool.execute",
            expected_revision=1,
            revocation_generation=1,
            now=datetime.now(UTC),
        )
    revoked = replace(grant, revision=2, revocation_generation=2, revoked=True)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        install_control_grant(connection, revoked)
        with pytest.raises(RevisionConflictError):
            install_control_grant(connection, grant)
        with pytest.raises(RevisionConflictError):
            install_control_grant(
                connection, replace(revoked, revision=3, revocation_generation=3, revoked=False)
            )
    with app_env.other_db.transaction(app_env.other_context) as connection:
        current = current_control_grant(connection, grant.workspace_id, grant.id)
        assert current.revoked and current.revision == 2
        with pytest.raises(ScopeDeniedError):
            current.authorize(
                app_env.context,
                target_workspace_id=grant.workspace_id,
                permission="tool.execute",
                expected_revision=1,
                revocation_generation=1,
                now=datetime.now(UTC),
            )


def test_competing_cross_workspace_grants_cannot_commit_a_cycle(app_env):
    _owners(app_env)
    barrier = Barrier(2)

    def issue(grant):
        try:
            with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
                barrier.wait(timeout=10)
                install_control_grant(connection, grant)
            return "issued"
        except ScopeDeniedError:
            return "cycle_rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                issue,
                (
                    _grant(app_env.context, app_env.other_context),
                    _grant(app_env.other_context, app_env.context),
                ),
            )
        )
    assert sorted(results) == ["cycle_rejected", "issued"]


def test_reviewed_manifest_replay_never_creates_another_attempt(app_env):
    _owners(app_env)
    node = _node(app_env.context)
    facts = HostObservation(
        "Linux", "ubuntu", "24.04", "x86_64", "3.12.3", str(uuid4()), True, 4 * 1024**3
    )
    releases = tuple(
        ReleasePin(name, "fixture-0", "a" * 64, "b" * 64, "c" * 64)
        for name in ("syn", "potbot-runner")
    )
    arguments = dict(
        target_workspace_id=app_env.other_context.workspace_id, project_root="/srv/potbot/fixture"
    )
    plan = provisioning_manifest(node, facts, releases, **arguments)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        register_node(connection, node)
        identity = record_provisioning_plan(
            connection,
            node,
            plan,
            reviewed_manifest_sha256=plan["manifest_sha256"],
            idempotency_key="fixture-install",
        )
        assert identity == record_provisioning_plan(
            connection,
            node,
            plan,
            reviewed_manifest_sha256=plan["manifest_sha256"],
            idempotency_key="fixture-install",
        )
        with pytest.raises(InvalidInputError):
            record_provisioning_plan(
                connection,
                node,
                plan,
                reviewed_manifest_sha256="0" * 64,
                idempotency_key="fixture-install",
            )
        changed = provisioning_manifest(
            node, facts, releases, **(arguments | {"project_root": "/srv/potbot/changed"})
        )
        with pytest.raises(RevisionConflictError):
            record_provisioning_plan(
                connection,
                node,
                changed,
                reviewed_manifest_sha256=changed["manifest_sha256"],
                idempotency_key="fixture-install",
            )
    with app_env.db.transaction(app_env.context) as connection:
        rows = connection.execute("SELECT id,state FROM remote_provisioning_attempts").fetchall()
        assert rows == [{"id": identity, "state": "planned"}]


def test_native_deployment_is_unqualified_and_cannot_replace_a_registered_sandbox(app_env):
    _owners(app_env)
    node = _node(app_env.context)
    environment = NativeEnvironment(
        "ubuntu", "24.04", "x86_64", "python3.12", "a" * 64, "b" * 64, "c" * 64
    )
    sandbox_deployment = WorkspaceDeployment(
        app_env.context.workspace_id,
        node.workspace_id,
        node.id,
        node.owner_principal_id,
        "/srv/potbot/fixture",
        "potbot-w-" + "a" * 16,
        environment,
    )
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        register_node(connection, node)
        with pytest.raises(RevisionConflictError, match="sandbox"):
            register_deployment(connection, sandbox_deployment)
    target = provision_workspace(app_env.admin_dsn, app_env.schema, "native-fixture")
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    context = provision_principal(app_env.admin_dsn, app_env.schema, role, target)
    deployment = replace(sandbox_deployment, workspace_id=target)
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        install_workspace_owner(connection, target, context.principal_id)
        register_deployment(connection, deployment)
        register_deployment(connection, deployment)
    with app_env.db.transaction(context) as connection:
        rows = connection.execute(
            "SELECT execution_mode,qualification FROM native_workspace_deployments"
        ).fetchall()
        assert rows == [{"execution_mode": "base_os", "qualification": "unqualified"}]
        assert connection.execute("SELECT * FROM workspace_sandboxes").fetchall() == []
