"""Real PostgreSQL acquired skill loading through genuine Context sessions.

Framed subprocess tests exercise the production protocol and database service;
they do not claim native sandbox or installed-host qualification.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest

from potbot.context.model import document_templates
from potbot.context.rpc_contract import SCHEMA, ContextRequest, SkillRequest
from potbot.context.rpc_transport import evaluate_context_frame, serve_context_streams
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import ContextSessionStore
from potbot.context.skill_acquisition import SkillAcquisition
from potbot.context.skill_packages import parse_skill_zip
from potbot.context.skill_projection import SkillLibraryProjector
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.persistence.database import admin_connection, provision_principal

pytestmark = pytest.mark.postgres
TOKEN = "operator-fixture-" + "a" * 64
CONTENT = b"---\nname: 3d-reference\ndescription: Read a portable reference\n---\nRead references/guide.bin.\n"


def package(*, resource=b"first reference", content=CONTENT):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("SKILL.md", content)
        archive.writestr("references/guide.bin", resource)
        archive.writestr(
            "AGENTS.md", b"Package instructions must remain untrusted resource data.\n"
        )
        archive.writestr("scripts/example.py", b"raise RuntimeError('must never execute')\n")
    raw = stream.getvalue()
    return raw, parse_skill_zip(raw)


def install(env, *, resource=b"first reference", content=CONTENT, service=None, skill_id=None):
    service = service or SkillAcquisition(env.db, setup_token_resolver=lambda: TOKEN)
    raw, bundle = package(resource=resource, content=content)
    imported = service.acquire(
        env.context,
        {"kind": "zip", "sha256": hashlib.sha256(raw).hexdigest(), "subdirectory": ""},
        uuid4().hex,
        lambda: bundle,
    )
    package_id = UUID(imported["package"]["id"])
    plan = service.plan(env.context, package_id, skill_id=skill_id)["plan"]
    result = service.install(
        env.context,
        package_id,
        plan["sha256"],
        plan["expectedRevision"],
        uuid4().hex,
        skill_id=skill_id,
        setup_token=TOKEN,
    )["skill"]
    return service, result, bundle


def control(env, service, skill, action, release=None):
    return service.control(
        env.context,
        UUID(skill["id"]),
        action,
        skill["revision"],
        uuid4().hex,
        release_sha256=release,
        setup_token=TOKEN,
    )["skill"]


def session(env, *, database=None, actor=None, token=lambda: TOKEN):
    database, actor = database or env.db, actor or env.context
    agent = uuid4()
    service = WorkspaceContextService(database, skill_setup_token=token)
    service.provision_private(actor, "agent", str(agent), idempotency_key=uuid4().hex)
    store = ContextSessionStore(database, skill_setup_token=token)
    return service, store, store.issue(actor, agent, uuid4(), lease_seconds=300)


def load(skill, *, release=None):
    return SkillRequest(
        uuid4(),
        "skills.load",
        skill_id=UUID(skill["id"]),
        release_sha256=release or skill["releaseSha256"],
    )


def read(skill, *, release=None, path="references/guide.bin", offset=0, maximum=65_536):
    return SkillRequest(
        uuid4(),
        "skills.read",
        skill_id=UUID(skill["id"]),
        release_sha256=release or skill["releaseSha256"],
        resource_path=path,
        offset=offset,
        max_bytes=maximum,
    )


def unavailable(store, grant, skill):
    assert store.execute(grant, SkillRequest(uuid4(), "skills.list"))["skills"] == []
    for request in (
        load(skill),
        read(skill),
        ContextRequest(uuid4(), "context.read", "skill", "AGENTS.md", skill_id=UUID(skill["id"])),
    ):
        with pytest.raises(ScopeDeniedError):
            store.execute(grant, request)


def test_import_install_activate_update_rollback_and_revoke_through_context(app_env):
    service, first, original = install(app_env)
    _, store, grant = session(app_env)
    unavailable(store, grant, first)
    active = control(app_env, service, first, "activate")
    view = store.execute(grant, load(active))
    assert view["content"].encode() == CONTENT
    assert view["content_sha256"] == original.content_sha256
    assert view["sha256"] == original.release_sha256 != original.content_sha256
    assert view["manifest"] == original.manifest and view["provenance"] == original.source
    assert view["namespace"].startswith("imported-")
    assert view["resource_access"] == "context-rpc" and view["trusted"] is False
    assert {role: doc["content"] for role, doc in view["context"].items()} == document_templates(
        "skill", active["contextName"]
    )
    fragment = store.execute(grant, read(active, maximum=5))
    assert base64.b64decode(fragment["data"]) == b"first" and fragment["next_offset"] == 5
    assert fragment["resource_sha256"] == hashlib.sha256(b"first reference").hexdigest()
    package_agents = store.execute(grant, read(active, path="AGENTS.md"))
    assert base64.b64decode(package_agents["data"]) == original.files["AGENTS.md"]

    _, updated, replacement = install(
        app_env, service=service, skill_id=UUID(active["id"]), resource=b"second reference"
    )
    assert replacement.content_sha256 == original.content_sha256
    assert replacement.release_sha256 != original.release_sha256
    unavailable(store, grant, updated)
    current = control(app_env, service, updated, "activate")
    assert base64.b64decode(store.execute(grant, read(current))["data"]) == b"second reference"
    # A stale request is never redirected to the newer package. Bytes already
    # returned above remain in the caller; later old-digest reads are denied.
    for request in (
        load(current, release=original.release_sha256),
        read(current, release=original.release_sha256),
    ):
        with pytest.raises(ScopeDeniedError):
            store.execute(grant, request)
    rolled = control(app_env, service, current, "rollback", original.release_sha256)
    unavailable(store, grant, rolled)
    restored = control(app_env, service, rolled, "activate")
    assert store.execute(grant, load(restored))["manifest"] == original.manifest
    with pytest.raises(ScopeDeniedError):
        store.execute(grant, read(restored, release=replacement.release_sha256))
    disabled = control(app_env, service, restored, "disable")
    unavailable(store, grant, disabled)
    enabled = control(app_env, service, disabled, "activate")
    revoked = control(app_env, service, enabled, "revoke")
    unavailable(store, grant, revoked)
    with app_env.db.transaction(app_env.context) as conn:
        rows = conn.execute(
            "SELECT session_id,package_id FROM workspace_context_skill_versions WHERE skill_id=%s ORDER BY revision",
            (UUID(first["id"]),),
        ).fetchall()
    assert len(rows) == 2 and all(row["session_id"] is None and row["package_id"] for row in rows)


def test_shared_consumer_loads_owner_signed_package_but_cannot_mutate_it(app_env):
    service, installed, _ = install(app_env)
    active = control(app_env, service, installed, "activate")
    _, foreign_store, foreign = session(
        app_env, database=app_env.other_db, actor=app_env.other_context
    )
    unavailable(foreign_store, foreign, active)
    peer = provision_principal(
        app_env.admin_dsn,
        app_env.schema,
        urlsplit(app_env.other_db.dsn).username,
        app_env.context.workspace_id,
        principal_id=app_env.other_context.principal_id,
    )
    _, store, grant = session(app_env, database=app_env.other_db, actor=peer)
    assert store.execute(grant, load(active))["content"].encode() == CONTENT
    for method in ("skills.save", "skills.publish", "skills.archive"):
        with pytest.raises(ScopeDeniedError):
            store.execute(
                grant,
                SkillRequest(
                    uuid4(),
                    method,
                    skill_id=UUID(active["id"]),
                    expected_revision=1,
                    content="changed" if method == "skills.save" else None,
                ),
            )
    with pytest.raises(ScopeDeniedError):
        store.execute(
            grant,
            ContextRequest(
                uuid4(),
                "context.update",
                "skill",
                "MODEL.md",
                "modified",
                1,
                skill_id=UUID(active["id"]),
            ),
        )
    _, owner_store, owner = session(app_env)
    with pytest.raises(ScopeDeniedError):
        owner_store.execute(
            owner,
            SkillRequest(
                uuid4(), "skills.publish", skill_id=UUID(active["id"]), expected_revision=1
            ),
        )


def test_control_mac_rotation_and_database_forgery_deny_every_context_read(app_env):
    service, installed, _ = install(app_env)
    active = control(app_env, service, installed, "activate")
    tokens = {"value": TOKEN}
    _, store, grant = session(app_env, token=lambda: tokens["value"])
    assert store.execute(grant, load(active))["content"].encode() == CONTENT
    tokens["value"] = "rotated-" + "b" * 64
    unavailable(store, grant, active)
    tokens["value"] = None
    unavailable(store, grant, active)
    tokens["value"] = TOKEN
    assert store.execute(grant, read(active))["size"] == len(b"first reference")
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE workspace_context_skills SET control_revision=control_revision+1,control_mac=%s WHERE id=%s",
            ("0" * 64, UUID(active["id"])),
        )
    unavailable(store, grant, active)


def test_resource_integrity_and_range_bounds_are_checked_on_every_read(app_env):
    service, installed, _ = install(app_env, resource=b"z" * 70_000)
    active = control(app_env, service, installed, "activate")
    _, store, grant = session(app_env)
    first = store.execute(grant, read(active))
    assert len(base64.b64decode(first["data"])) == 65_536 and not first["eof"]
    last = store.execute(grant, read(active, offset=65_536))
    assert len(base64.b64decode(last["data"])) == 4464 and last["eof"]
    eof = store.execute(grant, read(active, offset=70_000))
    assert eof["data"] == "" and eof["eof"]
    with pytest.raises(InvalidInputError):
        store.execute(grant, read(active, offset=70_001))
    with pytest.raises(ScopeDeniedError):
        store.execute(grant, read(active, path="references/missing.bin"))
    # An administrator fault-injects bytes after successful reads. Ordinary
    # application credentials cannot rewrite the append-only package table.
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        conn.execute(
            "ALTER TABLE workspace_context_skill_package_files DISABLE TRIGGER immutable_record"
        )
        conn.execute(
            "UPDATE workspace_context_skill_package_files SET content=%s WHERE workspace_id=%s AND package_id=%s AND path='references/guide.bin'",
            (b"x" * 70_000, app_env.context.workspace_id, UUID(active["packageId"])),
        )
        conn.execute(
            "ALTER TABLE workspace_context_skill_package_files ENABLE TRIGGER immutable_record"
        )
    for request in (load(active), read(active, offset=65_536)):
        with pytest.raises((InvalidInputError, RevisionConflictError, ScopeDeniedError)):
            store.execute(grant, request)


def test_imported_skills_never_enter_legacy_shared_filesystem_projection(app_env, tmp_path):
    acquisition, installed, _ = install(app_env)
    control(app_env, acquisition, installed, "activate")
    service, _, _ = session(app_env)
    root = tmp_path / "skills"
    root.mkdir(mode=0o700)
    result = SkillLibraryProjector(root).sync(service, app_env.context)
    assert result["skills"] == 0
    assert json.loads((root / "_agents/catalogue.json").read_text())["skills"] == []
    assert not list(root.rglob("SKILL.md")) and not list(root.rglob("guide.bin"))


def test_acquired_package_uses_real_context_protocol_and_database_session(app_env):
    service, installed, bundle = install(app_env)
    active = control(app_env, service, installed, "activate")
    _, store, grant = session(app_env)
    path = Path(__file__).resolve().parents[2] / "sandbox/clients"
    program = """import base64,sys
sys.path.insert(0,sys.argv[1])
from context_client import ContextClient
c=ContextClient()
items=c.list_skills()['skills']; assert len(items)==1
loaded=c.load_skill(sys.argv[2],sys.argv[3])
assert loaded['content_sha256']==loaded['manifest']['content_sha256']
assert loaded['resource_access']=='context-rpc'
r=c.read_skill_resource(sys.argv[2],sys.argv[3],'references/guide.bin')
assert base64.b64decode(r['data'])==b'first reference'
"""

    async def run():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(path),
            active["id"],
            bundle.release_sha256,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        result = await serve_context_streams(
            process.stdout, process.stdin, lambda request: store.execute(grant, request)
        )
        assert await asyncio.wait_for(process.wait(), 3) == 0, (
            await process.stderr.read()
        ).decode()
        assert result.requests == 3 and result.errors == 0

    asyncio.run(run())


def test_oversized_full_load_fails_without_truncation_and_resources_remain_readable(app_env):
    content = CONTENT + b"\x01" * 700_000
    service, installed, bundle = install(app_env, content=content)
    active = control(app_env, service, installed, "activate")
    _, store, grant = session(app_env)
    before = None
    with app_env.db.transaction(app_env.context) as conn:
        before = conn.execute(
            "SELECT request_count FROM workspace_context_sessions WHERE id=%s", (grant.session_id,)
        ).fetchone()["request_count"]
    wire = {
        "schema": SCHEMA,
        "request_id": str(uuid4()),
        "method": "skills.load",
        "skill_id": active["id"],
        "release_sha256": bundle.release_sha256,
    }
    reply = json.loads(
        evaluate_context_frame(
            json.dumps(wire).encode(), lambda request: store.execute(grant, request)
        )
    )
    assert reply["ok"] is False and reply["error"]["code"] == "invalid_input"
    assert "result" not in reply and len(json.dumps(reply)) < 1024
    assert reply["error"]["message"] != "Context response exceeds its bound"
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT request_count FROM workspace_context_sessions WHERE id=%s",
                (grant.session_id,),
            ).fetchone()["request_count"]
            == before
        )
    first = store.execute(grant, read(active, path="SKILL.md"))
    assert base64.b64decode(first["data"]) == content[:65_536]
    assert first["resource_sha256"] == hashlib.sha256(content).hexdigest()


def test_full_mebibyte_imported_text_load_preserves_complete_original_bytes(app_env):
    content = CONTENT + b"a" * (1_048_576 - len(CONTENT))
    service, installed, bundle = install(app_env, content=content)
    active = control(app_env, service, installed, "activate")
    _, store, grant = session(app_env)
    wire = {
        "schema": SCHEMA,
        "request_id": str(uuid4()),
        "method": "skills.load",
        "skill_id": active["id"],
        "release_sha256": bundle.release_sha256,
    }
    reply = json.loads(
        evaluate_context_frame(
            json.dumps(wire).encode(), lambda request: store.execute(grant, request)
        )
    )
    assert reply["ok"] is True
    assert reply["result"]["content"].encode() == content
    assert reply["result"]["content_sha256"] == hashlib.sha256(content).hexdigest()


def test_jsonb_numeric_metadata_round_trip_preserves_manifest_and_original_text(app_env):
    content = (
        b"---\nname: 3d-reference\ndescription: Numeric metadata\n"
        b"metadata:\n  zero: -0.0\n  exponent: 1.25e-7\n  integral: 12.0\n"
        b"---\nOriginal metadata spelling remains in the source.\n"
    )
    service, installed, bundle = install(app_env, content=content)
    active = control(app_env, service, installed, "activate")
    _, store, grant = session(app_env)
    loaded = store.execute(grant, load(active))
    assert loaded["content"].encode() == content
    assert loaded["manifest"] == bundle.manifest
    assert loaded["sha256"] == bundle.release_sha256
    assert loaded["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert loaded["manifest"]["metadata"]["metadata"] == {
        "zero": 0,
        "exponent": 1.25e-7,
        "integral": 12,
    }
    resource = store.execute(grant, read(active, path="SKILL.md"))
    assert base64.b64decode(resource["data"]) == content


def test_rotated_operator_key_requires_reviewed_reinstall_before_context_access(app_env):
    tokens = {"value": TOKEN}
    service = SkillAcquisition(app_env.db, setup_token_resolver=lambda: tokens["value"])
    _, installed, bundle = install(app_env, service=service)
    active = control(app_env, service, installed, "activate")
    _, store, grant = session(app_env, token=lambda: tokens["value"])
    assert store.execute(grant, load(active))["sha256"] == bundle.release_sha256
    tokens["value"] = "rotated-operator-" + "b" * 64
    unavailable(store, grant, active)
    with pytest.raises(ScopeDeniedError):
        service.control(
            app_env.context,
            UUID(active["id"]),
            "activate",
            active["revision"],
            uuid4().hex,
            setup_token=tokens["value"],
        )
    package_id, skill_id = UUID(active["packageId"]), UUID(active["id"])
    plan = service.plan(app_env.context, package_id, skill_id=skill_id)["plan"]
    reviewed = service.install(
        app_env.context,
        package_id,
        plan["sha256"],
        plan["expectedRevision"],
        uuid4().hex,
        skill_id=skill_id,
        setup_token=tokens["value"],
    )["skill"]
    unavailable(store, grant, reviewed)
    restored = service.control(
        app_env.context,
        skill_id,
        "activate",
        reviewed["revision"],
        uuid4().hex,
        setup_token=tokens["value"],
    )["skill"]
    assert store.execute(grant, load(restored))["content"].encode() == CONTENT
    assert base64.b64decode(store.execute(grant, read(restored))["data"]) == b"first reference"
