"""The CLI exposes the same scoped read projection, not host execution."""

from __future__ import annotations

import builtins
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot import cli
from potbot.api import operations_cli
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import RevisionConflictError


@pytest.mark.parametrize(
    "entry", (operations_cli.main, lambda args: cli.main(["operations", *args]))
)
def test_help_requires_no_database_web_or_native_dependencies(entry, monkeypatch, capsys):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"fastapi", "uvicorn", "httpx", "psycopg", "keyring"}:
            raise AssertionError("catalogue help imported runtime dependencies")
        if name == "potbot.controller.operation_catalogue":
            raise AssertionError("catalogue help constructed a runtime reader")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit) as result:
        entry(["--help"])
    assert result.value.code == 0
    assert "inspect" in capsys.readouterr().out


@pytest.fixture
def cli_rig(monkeypatch):
    from potbot.api import jobs_cli
    from potbot.controller import operation_catalogue

    actor = WorkspaceContext(uuid4(), uuid4())
    calls = []

    class Catalogue:
        def __init__(self, database):
            assert database is sentinel

        def list(self, context, **query):
            assert context == actor
            calls.append(("list", query))
            if query.get("after") == "stale":
                raise RevisionConflictError("secret://private-source-canary")
            return {"schema": "potbot-operation-catalogue/v1", "items": [], "nextCursor": None}

        def inspect(self, context, identifier):
            assert context == actor
            calls.append(("inspect", identifier))
            return {"schema": "potbot-operation-catalogue/v1", "item": {"id": identifier}}

    sentinel = object()
    monkeypatch.setattr(jobs_cli, "database_context", lambda _args: (sentinel, actor))
    monkeypatch.setattr(operation_catalogue, "OperationCatalogue", Catalogue)
    args = ["--workspace-id", str(actor.workspace_id), "--principal-id", str(actor.principal_id)]
    return SimpleNamespace(actor=actor, args=args, calls=calls)


def test_list_and_inspect_use_existing_scope_without_dispatch(cli_rig, capsys):
    r = cli_rig
    assert (
        operations_cli.main([*r.args, "list", "--q", "profile", "--kind", "cog", "--limit", "3"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert r.calls == [
        ("list", {"query": "profile", "kind": "cog", "state": None, "after": None, "limit": 3})
    ]
    assert cli.main(["operations", *r.args, "inspect", "a" * 64]) == 0
    assert json.loads(capsys.readouterr().out)["item"]["id"] == "a" * 64
    assert r.calls[-1] == ("inspect", "a" * 64)


def test_stale_read_does_not_retry_and_private_failure_is_redacted(cli_rig, capsys):
    r = cli_rig
    assert operations_cli.main([*r.args, "list", "--after", "stale"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "revision_conflict"
    assert "private-source-canary" not in output
    assert len(r.calls) == 1


def test_cli_does_not_accept_mutation_commands(cli_rig):
    with pytest.raises(SystemExit) as result:
        operations_cli.main([*cli_rig.args, "execute", "a" * 64])
    assert result.value.code == 2
    assert cli_rig.calls == []
