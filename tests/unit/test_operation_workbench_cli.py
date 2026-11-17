"""CLI lifecycle uses the same scoped service, native capability and retry key."""

from __future__ import annotations

import builtins
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api import operations_cli
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, RevisionConflictError


@pytest.fixture
def workbench_cli(monkeypatch, tmp_path):
    from potbot.api import jobs_cli
    from potbot.controller import operation_workbench

    actor = WorkspaceContext(uuid4(), uuid4())
    operation, job = "a" * 64, uuid4()
    calls, vault = [], []
    secret = "operator-setup-cli-canary-" + "x" * 32
    database = object()

    class Workbench:
        def __init__(self, selected):
            assert selected is database

        def __getattr__(self, method):
            def invoke(context, identity, *args, **kwargs):
                assert context == actor and identity == operation
                calls.append((method, args, kwargs))
                if "stale" in args:
                    raise RevisionConflictError("private source path and setup canary")
                return {"schema": "potbot-operation-workbench/v1", "job": {"id": str(job)}}

            return invoke

    def token():
        vault.append("setup")
        return secret

    monkeypatch.setattr(jobs_cli, "database_context", lambda _args: (database, actor))
    monkeypatch.setattr(operation_workbench, "OperationWorkbench", Workbench)
    monkeypatch.setattr(operations_cli, "_setup_token", token)
    request = {"job_type": "model.generate", "input": {"prompt": "private-prompt-canary"}}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    scope = ["--workspace-id", str(actor.workspace_id), "--principal-id", str(actor.principal_id)]
    return SimpleNamespace(**locals())


@pytest.mark.parametrize(
    "command",
    ("check", "run", "job", "events", "result", "artifact", "download", "cancel", "contract"),
)
def test_lifecycle_help_never_loads_worker_database_or_credentials(command, monkeypatch, capsys):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"fastapi", "psycopg", "httpx", "keyring", "uvicorn"} or name in {
            "potbot.controller.operation_workbench",
            "potbot.controller.native_cog_dispatch",
            "potbot.service_credentials",
        }:
            raise AssertionError("help loaded an execution dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit) as exited:
        operations_cli.main(
            ["--workspace-id", str(uuid4()), "--principal-id", str(uuid4()), command, "--help"]
        )
    assert exited.value.code == 0 and "usage:" in capsys.readouterr().out


def test_check_and_run_preserve_input_key_and_private_operator_capability(workbench_cli, capsys):
    r = workbench_cli
    assert (
        operations_cli.main(
            [*r.scope, "check", r.operation, "--input", str(r.path), "--key", "check-1"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert r.secret not in output and "private-prompt-canary" not in output
    assert r.calls[-1] == ("check", (r.request, "check-1"), {"setup_token": r.secret})
    args = [
        *r.scope,
        "run",
        r.operation,
        "--input",
        str(r.path),
        "--check-job",
        str(r.job),
        "--key",
        "run-1",
        "--confirm",
    ]
    assert operations_cli.main(args) == 0
    assert operations_cli.main(args) == 0
    assert r.calls[-2:] == [
        ("launch", (r.job, r.request, "run-1"), {"setup_token": r.secret}),
        ("launch", (r.job, r.request, "run-1"), {"setup_token": r.secret}),
    ]
    assert len(r.vault) == 3


@pytest.mark.parametrize("command", ("run", "cancel"))
def test_effect_commands_need_explicit_confirmation(workbench_cli, command):
    r = workbench_cli
    args = [*r.scope, command, r.operation]
    if command == "run":
        args += ["--input", str(r.path), "--check-job", str(r.job), "--key", "run-1"]
    else:
        args += [str(r.job), "--key", "cancel-1"]
    with pytest.raises(SystemExit) as exited:
        operations_cli.main(args)
    assert exited.value.code == 2 and not r.calls and not r.vault


def test_owned_reads_and_cancel_do_not_open_operator_vault(workbench_cli, capsys):
    r = workbench_cli
    for command in ("contract", "job", "events", "result", "artifact", "cancel"):
        args = [*r.scope, command, r.operation]
        if command != "contract":
            args.append(str(r.job))
        if command == "events":
            args += ["--after", "7", "--limit", "5"]
        if command == "artifact":
            args += ["--offset", "17", "--length", "11"]
        if command == "cancel":
            args += ["--key", "cancel-1", "--confirm"]
        assert operations_cli.main(args) == 0
        assert json.loads(capsys.readouterr().out)["job"]["id"] == str(r.job)
    assert [row[0] for row in r.calls] == [
        "describe",
        "inspect",
        "events",
        "result",
        "artifact",
        "cancel",
    ]
    assert r.calls[2] == ("events", (r.job,), {"after": 7, "limit": 5})
    assert r.calls[4] == ("artifact", (r.job,), {"offset": 17, "length": 11})
    assert not r.vault


@pytest.mark.parametrize(
    "raw", ('{"input":1,"input":2}', '{"input":NaN}', "[]", '"text"', "x" * 16385)
)
def test_bad_input_never_submits_and_is_not_echoed(workbench_cli, capsys, raw):
    r = workbench_cli
    r.path.write_text(raw)
    assert (
        operations_cli.main(
            [*r.scope, "check", r.operation, "--input", str(r.path), "--key", "check-1"]
        )
        == 1
    )
    assert not r.calls
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_stale_check_is_not_automatically_retried(workbench_cli, capsys):
    r = workbench_cli
    assert (
        operations_cli.main(
            [*r.scope, "check", r.operation, "--input", str(r.path), "--key", "stale"]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "revision_conflict"
    assert "canary" not in output and len(r.calls) == 1


def test_locked_native_capability_never_queues(workbench_cli, monkeypatch, capsys):
    r = workbench_cli

    def locked():
        raise CapabilityUnavailableError("private native vault path")

    monkeypatch.setattr(operations_cli, "_setup_token", locked)
    assert (
        operations_cli.main(
            [*r.scope, "check", r.operation, "--input", str(r.path), "--key", "check-1"]
        )
        == 1
    )
    assert not r.calls
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "capability_unavailable"
    assert "private native vault" not in output
