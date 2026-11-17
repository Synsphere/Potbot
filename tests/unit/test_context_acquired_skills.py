"""Acquired packages use exact Context reads and never enter guest filesystem views."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot.context.rpc_contract import SCHEMA, SkillRequest, parse_request
from potbot.context.rpc_transport import response_bytes, serve_context_streams
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import ContextSession
from potbot.context.skill_catalogue import SessionSkillCatalogue, published_skills
from potbot.context.skill_projection import SkillLibraryProjector
from potbot.controller.operation_catalogue import project_observations
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError


def request(**overrides):
    return {
        "schema": SCHEMA,
        "request_id": str(uuid4()),
        "method": "skills.read",
        "skill_id": str(uuid4()),
        "release_sha256": "a" * 64,
        "resource_path": "references/guide.txt",
        "offset": 0,
        "max_bytes": 65_536,
        **overrides,
    }


def parse(value):
    return parse_request(json.dumps(value).encode())


def test_resource_reads_have_an_exact_non_authorizing_wire_contract():
    value = request()
    parsed = parse(value)
    assert isinstance(parsed, SkillRequest) and not parsed.mutates
    for field in value:
        with pytest.raises(InvalidInputError):
            parse({key: item for key, item in value.items() if key != field})
    for field in ("principal_id", "session_id", "setup_token", "path", "argv", "agent_id"):
        with pytest.raises(InvalidInputError):
            parse({**value, field: "not-authority"})
    assert parsed.fingerprint != parse({**value, "offset": 1}).fingerprint


@pytest.mark.parametrize(
    "field,value",
    [
        ("offset", True),
        ("offset", -1),
        ("offset", 2**31),
        ("offset", 0.0),
        ("max_bytes", False),
        ("max_bytes", 0),
        ("max_bytes", 65_537),
        ("max_bytes", 1.5),
        ("resource_path", "/etc/passwd"),
        ("resource_path", "../secret"),
        ("resource_path", "assets/../secret"),
        ("resource_path", "assets//file"),
        ("resource_path", "C:\\secret"),
        ("resource_path", "assets/./file"),
        ("resource_path", "assets/file\x00"),
        ("resource_path", None),
    ],
)
def test_resource_paths_and_ranges_cannot_select_host_files(field, value):
    with pytest.raises(InvalidInputError):
        parse(request(**{field: value}))


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def skill_row(context, *, identity=None, agent=None, origin="acquired"):
    agent = agent or uuid4()
    namespace = (
        "imported-" + context.principal_id.hex if origin == "acquired" else "agent-" + agent.hex
    )
    return {
        "id": identity or uuid4(),
        "slug": "text-tools",
        "owner_principal_id": context.principal_id,
        "owner_agent_id": None if origin == "acquired" else agent,
        "context_object_id": uuid4(),
        "context_name": namespace + ".text-tools",
        "published": True,
        "state": "active",
        "enabled": True,
        "origin": origin,
        "draft_revision": 1,
        "latest_sha256": "a" * 64,
    }


@pytest.mark.parametrize("method", ["skills.save", "skills.publish", "skills.archive"])
def test_same_principal_agent_cannot_mutate_acquired_skill(method):
    context = WorkspaceContext(uuid4(), uuid4())
    grant = ContextSession(context, uuid4(), uuid4(), uuid4(), 1, "a" * 43)
    row = skill_row(context)
    # Origin is independently enforced even if a malformed row supplies the
    # current agent's UUID. A guest must never turn an import into authored code.
    row["owner_agent_id"] = grant.agent_id
    connection = SimpleNamespace(execute=lambda *a: Cursor([row]))
    catalogue = SessionSkillCatalogue(connection, grant, {}, None)
    with pytest.raises(ScopeDeniedError):
        catalogue.authorize(
            SkillRequest(
                uuid4(),
                method,
                skill_id=row["id"],
                expected_revision=1,
                content="# rewrite" if method == "skills.save" else None,
            )
        )


def test_authored_discovery_does_not_open_native_credential_store():
    context = WorkspaceContext(uuid4(), uuid4())
    row = skill_row(context, origin="authored")
    connection = SimpleNamespace(execute=lambda *a: Cursor([row]))

    def forbidden():
        pytest.fail("Authored skill discovery read the native operator credential")

    assert (
        published_skills(connection, context, setup_token=forbidden)["skills"][0]["origin"]
        == "authored"
    )
    assert (
        WorkspaceContextService(object(), skill_setup_token=forbidden).skill_setup_token
        is forbidden
    )


def test_projector_excludes_acquired_bytes_and_advances_empty_pages(tmp_path, monkeypatch):
    import potbot.context.skill_projection as module

    context = WorkspaceContext(uuid4(), uuid4())
    row = skill_row(context)
    calls = []

    def page(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["include_acquired"] is False
        if kwargs["after"] is None:
            return {
                "skills": [{"origin": "acquired", "skill_id": str(row["id"])}],
                "next": str(row["id"]),
            }
        assert kwargs["after"] == row["id"]
        return {"skills": [], "next": None}

    @contextmanager
    def transaction(_):
        yield object()

    monkeypatch.setattr(module, "published_skills", page)
    monkeypatch.setattr(
        module,
        "load_skill",
        lambda *a, **kw: pytest.fail("Imported bytes reached mounted projector"),
    )
    root = tmp_path / "library"
    root.mkdir(mode=0o700)
    result = SkillLibraryProjector(root).sync(
        SimpleNamespace(database=SimpleNamespace(transaction=transaction)), context
    )
    assert result["skills"] == 0 and len(calls) == 2
    catalogue = json.loads((root / "_agents/catalogue.json").read_text())
    assert catalogue["skills"] == []
    assert {entry.name for entry in (root / "_agents").iterdir()} == {
        ".projection.lock",
        "catalogue.json",
    }
    with pytest.raises(InvalidInputError):
        SkillLibraryProjector(root)._snapshot(context, {"origin": "acquired"})
    monkeypatch.setattr(
        module, "published_skills", lambda *a, **kw: {"skills": [], "next": str(row["id"])}
    )
    with pytest.raises(StateMismatchError):
        SkillLibraryProjector(root).sync(
            SimpleNamespace(database=SimpleNamespace(transaction=transaction)), context
        )


def client_module():
    path = Path(__file__).resolve().parents[2] / "sandbox/clients/context_client.py"
    spec = importlib.util.spec_from_file_location("acquired_skill_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resource_response(value, data=b"package resource"):
    part = data[value["offset"] : value["offset"] + value["max_bytes"]]
    end = value["offset"] + len(part)
    return {
        "skill_id": value["skill_id"],
        "release_sha256": value["release_sha256"],
        "revision": 1,
        "resource_path": value["resource_path"],
        "resource_sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "offset": value["offset"],
        "next_offset": end,
        "eof": end == len(data),
        "encoding": "base64",
        "data": base64.b64encode(part).decode(),
        "range_sha256": hashlib.sha256(part).hexdigest(),
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("skill_id", str(uuid4())),
        ("release_sha256", "b" * 64),
        ("resource_path", "references/different.txt"),
        ("offset", 1),
        ("next_offset", 1),
        ("eof", False),
        ("data", "@@@"),
        ("range_sha256", "0" * 64),
        ("resource_sha256", "0" * 64),
    ],
)
def test_guest_client_rejects_resource_response_rebinding_and_corruption(field, value):
    module = client_module()
    wire = request()
    result = {**resource_response(wire), field: value}
    framed = response_bytes(
        {"schema": SCHEMA, "request_id": wire["request_id"], "ok": True, "result": result}
    )
    client = module.ContextClient(io.BytesIO(framed), io.BytesIO())
    with pytest.raises(module.ContextRPCError) as error:
        client._exchange(wire)
    assert error.value.code == "integrity_error"


def test_guest_client_loads_bundle_identity_separately_from_original_text():
    module = client_module()
    wire = {
        key: value
        for key, value in request(method="skills.load").items()
        if key not in {"resource_path", "offset", "max_bytes"}
    }
    content = "---\nname: portable\ndescription: Original text\n---\nKeep bytes.\n"
    result = {
        "skill_id": wire["skill_id"],
        "revision": 1,
        "release_sha256": wire["release_sha256"],
        "sha256": wire["release_sha256"],
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    framed = response_bytes(
        {"schema": SCHEMA, "request_id": wire["request_id"], "ok": True, "result": result}
    )
    assert (
        module.ContextClient(io.BytesIO(framed), io.BytesIO())._exchange(wire)["content"] == content
    )


def test_guest_client_verifies_imported_manifest_matches_selected_bundle():
    from potbot.context.skill_packages import package_from_files

    module = client_module()
    package = package_from_files(
        {
            "SKILL.md": b"---\nname: portable\ndescription: A package\n---\nRead resources.\n",
            "references/guide.txt": b"original",
        },
        source={"kind": "fixture"},
    )
    wire = {
        key: value
        for key, value in request(
            method="skills.load", release_sha256=package.release_sha256
        ).items()
        if key not in {"resource_path", "offset", "max_bytes"}
    }
    result = {
        "skill_id": wire["skill_id"],
        "revision": 1,
        "release_sha256": package.release_sha256,
        "sha256": package.release_sha256,
        "content": package.content,
        "content_sha256": package.content_sha256,
        "manifest": package.manifest,
    }

    def frame():
        return response_bytes(
            {"schema": SCHEMA, "request_id": wire["request_id"], "ok": True, "result": result}
        )

    assert (
        module.ContextClient(io.BytesIO(frame()), io.BytesIO())._exchange(wire)["manifest"]
        == package.manifest
    )
    result["manifest"] = {**package.manifest, "description": "tampered"}
    with pytest.raises(module.ContextRPCError) as error:
        module.ContextClient(io.BytesIO(frame()), io.BytesIO())._exchange(wire)
    assert error.value.code == "integrity_error"


def test_guest_resource_client_uses_real_framed_subprocess_pipes():
    path = Path(__file__).resolve().parents[2] / "sandbox/clients"
    identity = str(uuid4())
    seen = []
    program = """import base64,sys
sys.path.insert(0,sys.argv[1])
from context_client import ContextClient
c=ContextClient()
r=c.read_skill_resource(sys.argv[2], 'a'*64, 'references/guide.txt', max_bytes=7)
assert base64.b64decode(r['data'])==b'package'
assert r['next_offset']==7 and r['eof'] is False
"""

    def handler(req):
        seen.append(req)
        value = request(
            skill_id=str(req.skill_id),
            resource_path=req.resource_path,
            offset=req.offset,
            max_bytes=req.max_bytes,
        )
        return resource_response(value)

    async def run():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(path),
            identity,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        result = await serve_context_streams(process.stdout, process.stdin, handler)
        assert await asyncio.wait_for(process.wait(), 3) == 0, (
            await process.stderr.read()
        ).decode()
        assert result.requests == 1 and result.errors == 0

    asyncio.run(run())
    assert seen[0].method == "skills.read" and seen[0].skill_id == UUID(identity)


@pytest.mark.parametrize(
    "state,enabled,expected",
    [
        ("active", False, "skill_disabled"),
        ("revoked", False, "skill_revoked"),
        ("active", True, "skill_activation_signature_not_evaluated"),
    ],
)
def test_operation_catalogue_reports_imported_records_without_granting_access(
    state, enabled, expected
):
    context = WorkspaceContext(uuid4(), uuid4())
    data = {
        key: []
        for key in (
            "runtime",
            "tool_history",
            "upgrades",
            "models",
            "deployments",
            "skills",
            "syn",
            "recent",
            "accounting",
        )
    }
    data["skills"] = [
        {
            "id": str(uuid4()),
            "slug": "3d-reference",
            "context_object_id": str(uuid4()),
            "latest_sha256": "a" * 64,
            "source_revision": 1,
            "origin": "acquired",
            "enabled": enabled,
            "skill_state": state,
        }
    ]
    [entry] = project_observations(context, data)
    assert entry["name"] == "3d-reference" and expected in entry["reasons"]
    assert entry["state"] == ("installed_unqualified" if enabled else "policy_disabled")
    assert "skill_resource_access_requires_context_session" in entry["reasons"]
