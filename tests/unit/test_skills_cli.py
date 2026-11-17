"""Operator CLI confirmation, shared service dispatch and complete resource verification."""

from __future__ import annotations

import base64
import builtins
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context import skills_cli
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError


@pytest.mark.parametrize(
    "command",
    [
        "list",
        "packages",
        "import-zip",
        "import-directory",
        "import-github",
        "plan",
        "install",
        "activate",
        "disable",
        "rollback",
        "revoke",
        "read",
        "download",
        "history",
    ],
)
def test_help_never_loads_optional_database_network_yaml_or_vault(command, monkeypatch, capsys):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"psycopg", "fastapi", "httpx", "keyring", "yaml"} or name in {
            "potbot.context.skill_acquisition",
            "potbot.service_credentials",
        }:
            raise AssertionError("CLI help loaded an acquisition dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit) as exited:
        skills_cli.main(
            ["--workspace-id", str(uuid4()), "--principal-id", str(uuid4()), command, "--help"]
        )
    assert exited.value.code == 0 and "usage:" in capsys.readouterr().out


@pytest.fixture
def cli(monkeypatch):
    from potbot.api import jobs_cli, operations_cli
    from potbot.context import skill_acquisition

    context = WorkspaceContext(uuid4(), uuid4())
    identity, package_id = uuid4(), uuid4()
    database = object()
    calls, vault = [], []
    token = "cli-operator-private-canary-" + "x" * 32

    class Service:
        def __init__(self, selected):
            assert selected is database

        def __getattr__(self, method):
            def invoke(actor, *args, **kwargs):
                assert actor == context
                calls.append((method, args, kwargs))
                return {
                    "schema": "potbot-skill-acquisition/v1",
                    "workspaceId": str(context.workspace_id),
                    "principalId": str(context.principal_id),
                }

            return invoke

    def capability():
        vault.append("setup")
        return token

    monkeypatch.setattr(jobs_cli, "database_context", lambda args: (database, context))
    monkeypatch.setattr(operations_cli, "_setup_token", capability)
    monkeypatch.setattr(skill_acquisition, "SkillAcquisition", Service)
    scope = [
        "--workspace-id",
        str(context.workspace_id),
        "--principal-id",
        str(context.principal_id),
    ]
    return SimpleNamespace(**locals())


def test_install_and_rollback_bind_review_revision_key_and_operator_capability(cli, capsys):
    r = cli
    assert (
        skills_cli.main(
            [
                *r.scope,
                "install",
                str(r.package_id),
                "--plan",
                "a" * 64,
                "--revision",
                "0",
                "--key",
                "review-once",
                "--confirm",
            ]
        )
        == 0
    )
    assert r.calls[-1] == (
        "install",
        (r.package_id, "a" * 64, 0, "review-once"),
        {"skill_id": None, "setup_token": r.token},
    )
    assert (
        skills_cli.main(
            [
                *r.scope,
                "rollback",
                str(r.identity),
                "--release",
                "b" * 64,
                "--revision",
                "4",
                "--key",
                "rollback-once",
                "--confirm",
            ]
        )
        == 0
    )
    assert r.calls[-1] == (
        "control",
        (r.identity, "rollback", 4, "rollback-once"),
        {"release_sha256": "b" * 64, "setup_token": r.token},
    )
    assert r.vault == ["setup", "setup"]
    assert r.token not in capsys.readouterr().out


def test_read_does_not_resolve_setup_and_mutation_without_confirmation_stops_before_owner(
    cli, capsys
):
    r = cli
    assert (
        skills_cli.main([*r.scope, "history", str(r.identity), "--limit", "2", "--after", "3"]) == 0
    )
    assert r.calls[-1] == ("history", (r.identity,), {"limit": 2, "after": "3"})
    assert not r.vault
    with pytest.raises(SystemExit) as error:
        skills_cli.main(
            [*r.scope, "revoke", str(r.identity), "--revision", "1", "--key", "revoke-once"]
        )
    assert error.value.code == 2 and len(r.calls) == 1


def test_github_import_defers_source_read_to_same_idempotent_owner(cli, monkeypatch):
    from potbot.context import skill_packages

    r = cli
    monkeypatch.setattr(
        skill_packages,
        "fetch_github_skill",
        lambda *args: pytest.fail("Adapter eagerly fetched before owner replay check"),
    )
    repository, commit = "https://github.com/example/skills", "a" * 40
    assert (
        skills_cli.main(
            [
                *r.scope,
                "import-github",
                repository,
                "--commit",
                commit,
                "--path",
                "skills/one",
                "--key",
                "source-once",
            ]
        )
        == 0
    )
    method, args, kwargs = r.calls[-1]
    assert method == "acquire" and args[0] == {
        "kind": "github",
        "repository": repository,
        "commit": commit,
        "subdirectory": "skills/one",
    }
    assert args[1] == "source-once" and callable(args[2]) and kwargs == {}


def resource_service(context, content, *, tamper=False, scope=False):
    class Service:
        def package_resource(self, actor, identity, path, *, offset, length):
            assert actor == context
            end = min(len(content), offset + length)
            chunk = content[offset:end]
            if tamper and chunk:
                chunk = b"x" + chunk[1:]
            return {
                "schema": "potbot-skill-acquisition/v1",
                "workspaceId": str(uuid4() if scope else context.workspace_id),
                "principalId": str(context.principal_id),
                "resource": {
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "mediaType": "application/octet-stream",
                },
                "offset": offset,
                "dataBase64": base64.b64encode(chunk).decode(),
                "nextOffset": end if end < len(content) else None,
            }

    return Service()


def test_download_verifies_all_ranges_before_creating_destination(tmp_path):
    context = WorkspaceContext(uuid4(), uuid4())
    content = b"original bytes\n" * 10000
    target = tmp_path / "verified.bin"
    result = skills_cli.download_skill_resource(
        resource_service(context, content), context, uuid4(), "assets/file.bin", target
    )
    assert target.read_bytes() == content
    assert result == {
        "path": str(target),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }
    with pytest.raises(FileExistsError):
        skills_cli.download_skill_resource(
            resource_service(context, content), context, uuid4(), "assets/file.bin", target
        )
    assert target.read_bytes() == content
    assert json.loads(json.dumps(result))["bytes"] == len(content)


@pytest.mark.parametrize("settings", [{"tamper": True}, {"scope": True}])
def test_download_cannot_write_mixed_or_foreign_resources(tmp_path, settings):
    context = WorkspaceContext(uuid4(), uuid4())
    target = tmp_path / "must-not-exist.bin"
    with pytest.raises(InvalidInputError):
        skills_cli.download_skill_resource(
            resource_service(context, b"original bytes", **settings),
            context,
            uuid4(),
            "assets/file.bin",
            target,
        )
    assert not target.exists()
