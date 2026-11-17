"""Real PostgreSQL package privacy, signed decisions, CAS, replay and upgrades.

Package bytes in these tests are explicit fixtures, not upstream trust or
installed-host qualification. No source script is executed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest

from potbot.context.rpc_contract import SkillRequest
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import ContextSessionStore
from potbot.context.skill_acquisition import SkillAcquisition, verify_active_release
from potbot.context.skill_packages import package_from_files
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.persistence.database import (
    Database,
    admin_connection,
    grant_application_role,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations

pytestmark = pytest.mark.postgres
TOKEN = "skill-control-fixture-" + "a" * 48


def bundle(*, name="portable-notes", resource=b"private reference", source="a", metadata=""):
    return package_from_files(
        {
            "SKILL.md": (
                f"---\nname: {name}\ndescription: Read the exact retained reference\n"
                f"{metadata}---\nRead references/guide.txt.\n"
            ).encode(),
            "references/guide.txt": resource,
        },
        source={
            "kind": "zip",
            "archive_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "path": "",
            "verification": "content-integrity-only",
        },
    )


def stage(env, service, package, *, key=None, actor=None):
    return service.acquire(
        actor or env.context,
        {"kind": "zip", "sha256": package.source["archive_sha256"], "subdirectory": ""},
        key or uuid4().hex,
        lambda: package,
    )["package"]


def install(env, service, package, *, key=None, skill_id=None):
    plan = service.plan(env.context, UUID(package["id"]), skill_id=skill_id)["plan"]
    return service.install(
        env.context,
        UUID(package["id"]),
        plan["sha256"],
        plan["expectedRevision"],
        key or uuid4().hex,
        skill_id=skill_id,
        setup_token=TOKEN,
    )["skill"]


def control(env, service, skill, action, *, key=None, release=None):
    return service.control(
        env.context,
        UUID(skill["id"]),
        action,
        skill["revision"],
        key or uuid4().hex,
        release_sha256=release,
        setup_token=TOKEN,
    )["skill"]


def peer(env):
    return provision_principal(
        env.admin_dsn,
        env.schema,
        urlsplit(env.other_db.dsn).username,
        env.context.workspace_id,
        principal_id=env.other_context.principal_id,
    )


def test_private_imports_principal_keys_and_shared_current_reads(app_env):
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    remote = SkillAcquisition(app_env.other_db, setup_token_resolver=lambda: TOKEN)
    package = stage(app_env, service, bundle(), key="same-client-key")
    actor = peer(app_env)
    package_id = UUID(package["id"])
    assert remote.packages(actor)["items"] == []
    for call in (
        lambda: remote.package(actor, package_id),
        lambda: remote.package_resource(actor, package_id, "references/guide.txt"),
        lambda: remote.plan(actor, package_id),
    ):
        with pytest.raises(ScopeDeniedError):
            call()
    with app_env.other_db.transaction(actor) as conn:
        assert conn.execute("SELECT * FROM workspace_context_skill_packages").fetchall() == []
        assert conn.execute("SELECT * FROM workspace_context_skill_package_files").fetchall() == []
    # Workspace-wide command keys cannot recover a different principal's import.
    other_package = stage(app_env, remote, bundle(), key="same-client-key", actor=actor)
    assert other_package["id"] != package["id"]
    assert other_package["ownerPrincipalId"] == str(actor.principal_id)
    installed = install(app_env, service, package)
    skill_id = UUID(installed["id"])
    assert remote.list(actor)["items"] == []
    with pytest.raises(ScopeDeniedError):
        remote.inspect(actor, skill_id)
    active = control(app_env, service, installed, "activate")
    shared = remote.inspect(actor, skill_id)["skill"]
    assert shared["approvalValid"] and not shared["owned"]
    assert shared["ownerPrincipalId"] == str(app_env.context.principal_id)
    assert remote.list(actor)["items"] == [shared]
    data = remote.resource(actor, skill_id, active["releaseSha256"], "references/guide.txt")
    assert base64.b64decode(data["dataBase64"]) == b"private reference"
    for call in (
        lambda: remote.history(actor, skill_id),
        lambda: remote.control(
            actor, skill_id, "disable", active["revision"], "peer-control", setup_token=TOKEN
        ),
        lambda: remote.inspect(app_env.other_context, skill_id),
    ):
        with pytest.raises(ScopeDeniedError):
            call()
    disabled = control(app_env, service, active, "disable")
    assert remote.list(actor)["items"] == []
    with pytest.raises(ScopeDeniedError):
        remote.resource(actor, skill_id, active["releaseSha256"], "references/guide.txt")
    # Owners retain review access to disabled bytes; Context runtime access is
    # independently denied by the current enabled control.
    assert service.resource(
        app_env.context, skill_id, disabled["releaseSha256"], "references/guide.txt"
    )["resource"]["size"] == len(b"private reference")


def test_source_only_update_retains_every_decision_and_exact_provenance(app_env):
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    first = stage(app_env, service, bundle(source="first source"))
    initial = install(app_env, service, first)
    active = control(app_env, service, initial, "activate")
    second = stage(app_env, service, bundle(source="second source"))
    assert second["releaseSha256"] == first["releaseSha256"]
    assert second["sourceSha256"] != first["sourceSha256"]
    identity = UUID(active["id"])
    plan = service.plan(app_env.context, UUID(second["id"]), skill_id=identity)["plan"]
    assert plan["sourceChanged"]
    assert plan["added"] == plan["changed"] == plan["removed"] == []
    updated = install(app_env, service, second, skill_id=identity)
    assert not updated["enabled"] and updated["packageId"] == second["id"]
    activated = control(app_env, service, updated, "activate")
    with app_env.db.transaction(app_env.context) as conn:
        verified = verify_active_release(
            conn, app_env.context, identity, activated["releaseSha256"], TOKEN
        )
        assert verified["provenance"] == second["source"]
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skill_releases").fetchone()[
                "n"
            ]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skill_versions").fetchone()[
                "n"
            ]
            == 1
        )
    pages, after = [], None
    while True:
        page = service.history(app_env.context, identity, limit=1, after=after)
        pages.extend(page["events"])
        if page["nextCursor"] is None:
            break
        after = page["nextCursor"]
    assert [event["revision"] for event in pages] == [4, 3, 2, 1]
    assert [event["action"] for event in pages] == ["activate", "update", "activate", "install"]
    assert [event["packageId"] for event in pages] == [second["id"]] * 2 + [first["id"]] * 2
    assert pages[0]["source"] == second["source"] and pages[-1]["source"] == first["source"]
    public = json.dumps([page, activated, plan])
    assert TOKEN not in public and '"mac"' not in public and "control_mac" not in public


def test_committed_retries_preserve_original_results_and_failed_import_is_atomic(app_env):
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    loaded = bundle()
    request = {"kind": "zip", "sha256": loaded.source["archive_sha256"], "subdirectory": ""}
    calls = []

    def loader():
        calls.append("read")
        return loaded

    acquired = service.acquire(app_env.context, request, "import-retry", loader)
    assert (
        service.acquire(
            app_env.context, request, "import-retry", lambda: pytest.fail("replayed source fetched")
        )
        == acquired
    )
    assert calls == ["read"]
    package = acquired["package"]
    plan = service.plan(app_env.context, UUID(package["id"]))["plan"]
    first = install(app_env, service, package, key="install-retry")
    activated = control(app_env, service, first, "activate", key="activate-retry")
    revoked = control(app_env, service, activated, "revoke")
    assert revoked["state"] == "revoked"
    replayed = service.install(
        app_env.context,
        UUID(package["id"]),
        plan["sha256"],
        0,
        "install-retry",
        setup_token=TOKEN,
    )["skill"]
    assert replayed == first
    assert control(app_env, service, first, "activate", key="activate-retry") == activated
    with pytest.raises(RevisionConflictError):
        control(app_env, service, revoked, "activate", key="activate-retry")
    with pytest.raises(RevisionConflictError):
        service.acquire(
            app_env.context, {**request, "subdirectory": "changed"}, "import-retry", loader
        )

    def unavailable():
        raise InvalidInputError("Fixture source is gone")

    with pytest.raises(InvalidInputError):
        service.acquire(app_env.context, request, "atomic-failure", unavailable)
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skill_packages").fetchone()[
                "n"
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM command_results WHERE operation='skills.acquire'"
            ).fetchone()["n"]
            == 1
        )
    assert len(service.packages(app_env.context)["items"]) == 1


def test_current_revision_serializes_competing_controls_and_stale_plans(app_env):
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    first = stage(app_env, service, bundle())
    installed = install(app_env, service, first)
    next_package = stage(app_env, service, bundle(resource=b"updated", source="updated"))
    identity = UUID(installed["id"])
    stale = service.plan(app_env.context, UUID(next_package["id"]), skill_id=identity)["plan"]
    barrier = Barrier(2)

    def contender(action):
        barrier.wait(timeout=10)
        try:
            return control(app_env, service, installed, action)
        except RevisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(contender, ("activate", "disable")))
    assert responses.count("conflict") == 1
    winner = next(response for response in responses if isinstance(response, dict))
    assert winner["revision"] == installed["revision"] + 1
    with pytest.raises(RevisionConflictError):
        service.install(
            app_env.context,
            UUID(next_package["id"]),
            stale["sha256"],
            stale["expectedRevision"],
            "stale-plan",
            skill_id=identity,
            setup_token=TOKEN,
        )
    assert service.inspect(app_env.context, identity)["skill"] == winner
    assert len(service.history(app_env.context, identity)["events"]) == 2


def test_raw_database_release_and_control_cannot_be_laundered_into_approval(app_env):
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    first = stage(app_env, service, bundle())
    active = control(app_env, service, install(app_env, service, first), "activate")
    target_bundle = bundle(resource=b"unreviewed", source="unreviewed")
    target = stage(app_env, service, target_bundle)
    identity = UUID(active["id"])
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "INSERT INTO workspace_context_skill_versions(workspace_id,skill_id,revision,content,sha256,package_id) VALUES(%s,%s,2,%s,%s,%s)",
            (
                app_env.context.workspace_id,
                identity,
                target_bundle.content,
                target_bundle.content_sha256,
                UUID(target["id"]),
            ),
        )
        conn.execute(
            "INSERT INTO workspace_context_skill_releases(workspace_id,skill_id,sha256,content_sha256,source_revision,package_id) VALUES(%s,%s,%s,%s,2,%s)",
            (
                app_env.context.workspace_id,
                identity,
                target_bundle.release_sha256,
                target_bundle.content_sha256,
                UUID(target["id"]),
            ),
        )
        conn.execute(
            "INSERT INTO workspace_context_skill_controls(workspace_id,skill_id,revision,owner_principal_id,action,release_sha256,package_id,source_sha256,state,enabled,mac) VALUES(%s,%s,100,%s,'install',%s,%s,%s,'active',false,%s)",
            (
                app_env.context.workspace_id,
                identity,
                app_env.context.principal_id,
                target["releaseSha256"],
                UUID(target["id"]),
                target["sourceSha256"],
                "0" * 64,
            ),
        )
    with pytest.raises(ScopeDeniedError):
        control(app_env, service, active, "rollback", release=target["releaseSha256"])
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE workspace_context_skills SET control_revision=control_revision+1,control_mac=%s WHERE id=%s",
            ("0" * 64, identity),
        )
    forged = service.inspect(app_env.context, identity)["skill"]
    assert not forged["approvalValid"]
    for action in ("activate", "disable", "rollback"):
        with pytest.raises(ScopeDeniedError):
            control(
                app_env,
                service,
                forged,
                action,
                release=first["releaseSha256"] if action == "rollback" else None,
            )
    revoked = control(app_env, service, forged, "revoke")
    assert revoked["state"] == "revoked" and not revoked["enabled"]
    with pytest.raises(RevisionConflictError):
        control(app_env, service, revoked, "activate")


def test_package_integrity_storage_is_immutable_and_ranges_are_exact(app_env):
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    contents = b"\x00\xff" * 40000
    package = stage(app_env, service, bundle(resource=contents))
    package_id = UUID(package["id"])
    first = service.package_resource(app_env.context, package_id, "references/guide.txt")
    rest = service.package_resource(
        app_env.context, package_id, "references/guide.txt", offset=first["nextOffset"]
    )
    assert base64.b64decode(first["dataBase64"]) + base64.b64decode(rest["dataBase64"]) == contents
    assert rest["nextOffset"] is None
    assert first["resource"]["sha256"] == hashlib.sha256(contents).hexdigest()
    for arguments in (
        {"offset": -1},
        {"offset": len(contents) + 1},
        {"length": 65537},
        {"length": True},
    ):
        with pytest.raises(InvalidInputError):
            service.package_resource(
                app_env.context, package_id, "references/guide.txt", **arguments
            )
    for statement in (
        "UPDATE workspace_context_skill_package_files SET content='changed'::bytea WHERE package_id=%s",
        "DELETE FROM workspace_context_skill_packages WHERE id=%s",
    ):
        with pytest.raises(ScopeDeniedError):
            with app_env.db.transaction(app_env.context) as conn:
                conn.execute(statement, (package_id,))
    # Corruption is injected only through the test database administrator. Normal
    # application roles have no UPDATE/DELETE grant on retained package bytes.
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        conn.execute(
            "ALTER TABLE workspace_context_skill_package_files DISABLE TRIGGER immutable_record"
        )
        conn.execute(
            "UPDATE workspace_context_skill_package_files SET content=%s WHERE package_id=%s AND path='references/guide.txt'",
            (b"x" * len(contents), package_id),
        )
        conn.execute(
            "ALTER TABLE workspace_context_skill_package_files ENABLE TRIGGER immutable_record"
        )
    with pytest.raises(RevisionConflictError):
        service.package_resource(app_env.context, package_id, "references/guide.txt", length=1)


def test_raw_database_package_metadata_cannot_relabel_a_manifest_or_review_plan(app_env):
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    original = stage(app_env, service, bundle())
    for name, description in (
        ("forged-name", original["description"]),
        (original["name"], "Forged instructions description"),
    ):
        identity = uuid4()
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "INSERT INTO workspace_context_skill_packages(workspace_id,id,owner_principal_id,name,description,release_sha256,content_sha256,source_sha256,manifest,source,total_bytes,file_count) "
                "SELECT workspace_id,%s,owner_principal_id,%s,%s,release_sha256,content_sha256,source_sha256,manifest,source,total_bytes,file_count "
                "FROM workspace_context_skill_packages WHERE id=%s",
                (identity, name, description, UUID(original["id"])),
            )
            conn.execute(
                "INSERT INTO workspace_context_skill_package_files(workspace_id,package_id,path,sha256,byte_length,content) "
                "SELECT workspace_id,%s,path,sha256,byte_length,content FROM workspace_context_skill_package_files WHERE package_id=%s",
                (identity, UUID(original["id"])),
            )
        for read in (
            lambda identity=identity: service.package(app_env.context, identity),
            lambda identity=identity: service.plan(app_env.context, identity),
            lambda identity=identity: service.package_resource(
                app_env.context, identity, "references/guide.txt"
            ),
        ):
            with pytest.raises(RevisionConflictError):
                read()


def test_byte_bounded_catalogue_and_history_pages_do_not_drop_items(app_env, monkeypatch):
    from potbot.context import skill_acquisition

    # Reduce the byte allowance while preserving the production pagination path
    # so a small fixture exercises budget truncation before the requested limit.
    monkeypatch.setattr(skill_acquisition, "PAGE_BYTES", 5000)
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: TOKEN)
    installed = []
    packages = []
    for index in range(7):
        item = stage(app_env, service, bundle(name=f"notes-{index}", source=str(index)))
        packages.append(item)
        installed.append(install(app_env, service, item))
    current = installed[0]
    for revision in range(5):
        item = stage(
            app_env,
            service,
            bundle(
                name="notes-0", resource=f"Revision {revision}".encode(), source=f"new-{revision}"
            ),
        )
        packages.append(item)
        current = install(app_env, service, item, skill_id=UUID(current["id"]))

    def collect(fetch):
        items, after, cursors = [], None, set()
        while True:
            page = fetch(after)
            assert len(canonical_json(page).encode()) < 6000
            items.extend(page["items"])
            after = page["nextCursor"]
            if after is None:
                return items, len(cursors) + 1
            assert after not in cursors and page["items"]
            cursors.add(after)

    package_page, package_count = collect(
        lambda after: service.packages(app_env.context, limit=50, after=after)
    )
    skill_page, skill_count = collect(
        lambda after: service.list(app_env.context, limit=50, after=after)
    )
    assert package_count > 1 and skill_count > 1
    assert sorted(item["id"] for item in package_page) == sorted(item["id"] for item in packages)
    assert sorted(item["id"] for item in skill_page) == sorted(item["id"] for item in installed)
    history, after, cursors = [], None, set()
    while True:
        page = service.history(app_env.context, UUID(current["id"]), limit=50, after=after)
        assert len(canonical_json(page).encode()) <= 5000
        history.extend(page["events"])
        after = page["nextCursor"]
        if after is None:
            break
        assert after not in cursors and page["events"]
        cursors.add(after)
    assert cursors and [item["revision"] for item in history] == [6, 5, 4, 3, 2, 1]


def test_populated_v39_upgrade_preserves_genuine_authored_sessions_and_releases(app_env, tmp_path):
    schema = "skill_acquisition_upgrade"
    migrations = application_migrations()
    assert migrate(app_env.admin_dsn, schema, migrations[:39]) == tuple(range(1, 40))
    workspace = provision_workspace(
        app_env.admin_dsn, schema, "existing authored skills", sandbox_root=tmp_path / "upgrade"
    )
    role = urlsplit(app_env.db.dsn).username
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    database = Database(app_env.db.dsn, schema)
    agent = uuid4()
    context_service = WorkspaceContextService(database)
    context_service.provision_private(actor, "agent", str(agent), idempotency_key="legacy-agent")
    sessions = ContextSessionStore(database)
    grant = sessions.issue(actor, agent, uuid4(), lease_seconds=300)
    original = "---\nname: authored-notes\ndescription: Keep authored history\n---\nOriginal authored bytes.\n"
    created = sessions.execute(
        grant, SkillRequest(uuid4(), "skills.create", name="authored-notes", content=original)
    )
    identity = UUID(created["skill_id"])
    digest = hashlib.sha256(original.encode()).hexdigest()
    # Write the genuine pre-migration release shape. The current application
    # publisher also supplies the newly introduced content_sha256 column.
    with database.transaction(actor) as conn:
        conn.execute(
            "INSERT INTO workspace_context_skill_releases(workspace_id,skill_id,sha256,source_revision,session_id) VALUES(%s,%s,%s,1,%s)",
            (workspace, identity, digest, grant.session_id),
        )
        conn.execute(
            "UPDATE workspace_context_skills SET published=true,latest_sha256=%s WHERE id=%s",
            (digest, identity),
        )
        before_skill = dict(conn.execute("SELECT * FROM workspace_context_skills").fetchone())
        before_release = dict(
            conn.execute("SELECT * FROM workspace_context_skill_releases").fetchone()
        )
        before_version = dict(
            conn.execute("SELECT * FROM workspace_context_skill_versions").fetchone()
        )
    with admin_connection(app_env.admin_dsn, schema) as conn:
        ledger = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert migrate(app_env.admin_dsn, schema) == tuple(range(1, 41))
    grant_application_role(app_env.admin_dsn, schema, role)
    assert migrate(app_env.admin_dsn, schema) == tuple(range(1, 41))
    with database.transaction(actor) as conn:
        current = conn.execute("SELECT * FROM workspace_context_skills").fetchone()
        assert current == {
            **before_skill,
            "origin": "authored",
            "enabled": True,
            "control_revision": 0,
            "control_mac": None,
            "current_package_id": None,
        }
        assert conn.execute("SELECT * FROM workspace_context_skill_releases").fetchone() == {
            **before_release,
            "content_sha256": digest,
            "package_id": None,
        }
        assert conn.execute("SELECT * FROM workspace_context_skill_versions").fetchone() == {
            **before_version,
            "package_id": None,
        }
        assert conn.execute("SELECT * FROM workspace_context_skill_packages").fetchall() == []
        assert conn.execute("SELECT * FROM workspace_context_skill_controls").fetchall() == []
    with admin_connection(app_env.admin_dsn, schema) as conn:
        assert (
            conn.execute(
                "SELECT * FROM schema_migrations WHERE version<=39 ORDER BY version"
            ).fetchall()
            == ledger
        )
    loaded = sessions.execute(
        grant, SkillRequest(uuid4(), "skills.load", skill_id=identity, release_sha256=digest)
    )
    assert loaded["content"] == original
    saved = sessions.execute(
        grant,
        SkillRequest(
            uuid4(),
            "skills.save",
            skill_id=identity,
            expected_revision=1,
            content=original + "Updated.\n",
        ),
    )
    published = sessions.execute(
        grant,
        SkillRequest(
            uuid4(), "skills.publish", skill_id=identity, expected_revision=saved["revision"]
        ),
    )
    assert published["release_sha256"] != digest
    assert len(canonical_json(loaded).encode()) < 2 * 1024 * 1024
