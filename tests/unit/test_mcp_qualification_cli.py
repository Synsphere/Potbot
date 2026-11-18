"""Optional-dependency-free help, explicit scope and safe CLI command routing."""

import asyncio
import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api import mcp_qualification_cli as module
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "command",
    [
        None,
        "suites",
        "suite",
        "attempts",
        "attempt",
        "report",
        "admit",
        "control",
        "request",
        "run",
        "recover",
        "cancel",
    ],
)
def test_help_needs_no_optional_packages_database_credentials_or_native_host(command):
    args = [sys.executable, "-S", "-m", "potbot", "mcp-qualification"]
    if command:
        args += ["--workspace-id", str(uuid4()), "--principal-id", str(uuid4()), command]
    result = subprocess.run(
        [*args, "--help"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("command", ["run", "recover"])
def test_native_commands_require_reviewed_identity_and_explicit_private_owner_configuration(
    command, capsys
):
    with pytest.raises(SystemExit) as stopped:
        module._parser().parse_args(
            ["--workspace-id", str(uuid4()), "--principal-id", str(uuid4()), command, str(uuid4())]
        )
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    for flag in (
        "--revision",
        "--digest",
        "--profile",
        "--suite",
        "--manifest",
        "--workspace-name",
        "--artifacts-root",
        "--receipts-root",
        "--key",
        "--confirm",
    ):
        assert flag in error


@pytest.mark.parametrize("command", ["run", "recover"])
@pytest.mark.parametrize("state", ["running", "succeeded", "failed", "cancelled", "indeterminate"])
def test_exact_command_retry_recovers_before_any_native_import_or_configuration(
    monkeypatch, command, state
):
    args = SimpleNamespace(
        command=command, identity=uuid4(), revision=1, digest="a" * 64, key="native-once"
    )
    receipt = {"attempt": {"id": str(args.identity), "status": state}}
    calls = []

    def replay(*values, **options):
        calls.append((values, options))
        return receipt

    original_import = builtins.__import__

    def no_native(name, *args, **kwargs):
        if name in {
            "potbot.controller.mcp_discovery",
            "potbot.tools.mcp_discovery_host",
            "potbot.tools.mcp_qualification_host",
        }:
            raise AssertionError(
                "A durable command replay must not load or configure a native host"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_native)
    service, context = SimpleNamespace(execution_replay=replay), object()
    assert (
        asyncio.run(module._execute(service, object(), context, args, "private-token")) == receipt
    )
    assert calls == [
        (
            (context, args.identity, args.revision, args.digest, args.key),
            {"setup_token": "private-token", "recovery": command == "recover"},
        )
    ]


@pytest.fixture
def routing(monkeypatch):
    from potbot.api import jobs_cli, operations_cli
    from potbot.controller import mcp_qualification
    from potbot.tools import mcp_discovery_host, mcp_qualification_host

    context = WorkspaceContext(uuid4(), uuid4())
    database, calls, credential_reads = object(), [], []
    token = "private-cli-setup-token-canary-" + "x" * 40
    profile, suite = object(), object()
    failure = [None]

    class Service:
        def __getattr__(self, name):
            def invoke(actor, *args, **kwargs):
                assert actor is context
                calls.append((name, args, kwargs))
                if failure[0] is not None:
                    raise failure[0]
                return {"schema": "potbot-mcp-qualification/v1", "command": name}

            return invoke

    service = Service()

    def setup_token():
        credential_reads.append(True)
        return token

    async def native(selected_service, db, actor, args, supplied_token):
        assert (selected_service, db, actor, supplied_token) == (service, database, context, token)
        calls.append(("native", (args,), {}))
        return {"schema": "potbot-mcp-qualification/v1", "command": args.command}

    monkeypatch.setattr(jobs_cli, "database_context", lambda args: (database, context))
    monkeypatch.setattr(operations_cli, "_setup_token", setup_token)
    monkeypatch.setattr(
        mcp_qualification,
        "McpQualificationService",
        lambda selected: service if selected is database else None,
    )
    monkeypatch.setattr(mcp_discovery_host, "read_mcp_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(
        mcp_qualification_host, "read_mcp_qualification_suite", lambda *args, **kwargs: suite
    )
    monkeypatch.setattr(module, "_execute", native)
    scope = [
        "--workspace-id",
        str(context.workspace_id),
        "--principal-id",
        str(context.principal_id),
    ]
    return SimpleNamespace(**locals())


@pytest.mark.parametrize("command", ["suites", "attempts", "suite", "attempt", "report"])
def test_retained_cli_reads_never_resolve_setup_authority(routing, capsys, command):
    r = routing
    identity, cursor = uuid4(), uuid4()
    tail = [command]
    if command in {"suites", "attempts"}:
        flag = "--binding" if command == "suites" else "--suite"
        tail += [flag, str(identity), "--limit", "11", "--after", str(cursor)]
    else:
        tail += [str(identity)]
    assert module.main([*r.scope, *tail]) == 0
    assert not r.credential_reads
    assert [call[0] for call in r.calls] == [command]
    if command in {"suites", "attempts"}:
        key = "binding_id" if command == "suites" else "suite_id"
        assert r.calls[0][2] == {key: identity, "limit": 11, "after": str(cursor)}
    else:
        assert r.calls[0][1] == (identity,)
    assert json.loads(capsys.readouterr().out) == {
        "schema": "potbot-mcp-qualification/v1",
        "command": command,
    }


@pytest.mark.parametrize("command", ["control", "request", "cancel"])
def test_cli_decisions_preserve_exact_review_key_and_independent_setup_token(
    routing, capsys, command
):
    r = routing
    identity = uuid4()
    tail = [command, str(identity)] + (["revoke"] if command == "control" else [])
    tail += ["--revision", "3", "--digest", "a" * 64, "--key", "reviewed-command", "--confirm"]
    assert module.main([*r.scope, *tail]) == 0
    name, values, options = r.calls[0]
    assert name == command and values[0] == identity
    assert values[-3:] == (3, "a" * 64, "reviewed-command")
    assert options == {"setup_token": r.token} and r.credential_reads == [True]
    assert r.token not in capsys.readouterr().out


def test_cli_admission_uses_private_parsers_and_existing_task_identity(
    routing, monkeypatch, capsys
):
    from potbot.tools import mcp_discovery_host, mcp_qualification_host

    r = routing
    binding, episode, state = uuid4(), uuid4(), uuid4()
    profile_path, suite_path = (
        Path("/operator/private-profile.json"),
        Path("/operator/private-suite.json"),
    )
    parsed = []

    def profile(actor, path, *, binding_id):
        parsed.append((actor, path, binding_id))
        return r.profile

    def suite(path):
        parsed.append(path)
        return r.suite

    monkeypatch.setattr(mcp_discovery_host, "read_mcp_profile", profile)
    monkeypatch.setattr(mcp_qualification_host, "read_mcp_qualification_suite", suite)
    assert (
        module.main(
            [
                *r.scope,
                "admit",
                "--binding",
                str(binding),
                "--episode",
                str(episode),
                "--state",
                str(state),
                "--profile",
                str(profile_path),
                "--suite",
                str(suite_path),
                "--key",
                "admission-once",
                "--confirm",
            ]
        )
        == 0
    )
    assert parsed == [(r.context, profile_path, binding), suite_path]
    assert r.calls == [
        ("admit", (r.profile, r.suite, episode, state, "admission-once"), {"setup_token": r.token})
    ]
    output = capsys.readouterr().out
    assert "/operator/" not in output and r.token not in output


@pytest.mark.parametrize("command", ["run", "recover"])
def test_cli_native_commands_pass_explicit_existing_storage_and_runtime_configuration(
    routing, capsys, command
):
    r = routing
    identity = uuid4()
    assert (
        module.main(
            [
                *r.scope,
                command,
                str(identity),
                "--revision",
                "3",
                "--digest",
                "a" * 64,
                "--key",
                "native-once",
                "--confirm",
                "--profile",
                "/operator/profile.json",
                "--suite",
                "/operator/suite.json",
                "--manifest",
                "/operator/runtime.json",
                "--workspace-name",
                "fixture-workspace",
                "--artifacts-root",
                "/operator/artifacts",
                "--receipts-root",
                "/operator/receipts",
            ]
        )
        == 0
    )
    name, (args,), options = r.calls[0]
    assert name == "native" and not options
    assert args.command == command and args.identity == identity
    assert args.artifacts_root == Path("/operator/artifacts")
    assert args.receipts_root == Path("/operator/receipts")
    assert args.manifest == Path("/operator/runtime.json")
    output = capsys.readouterr().out
    assert "/operator/" not in output and r.token not in output


@pytest.mark.parametrize(
    "failure",
    [
        ScopeDeniedError("private native path/token canary"),
        RuntimeError("private database DSN canary"),
    ],
)
def test_cli_errors_emit_only_existing_redacted_error_envelope(routing, capsys, failure):
    r = routing
    r.failure[0] = failure
    assert module.main([*r.scope, "suites"]) == 1
    output = capsys.readouterr().out
    value = json.loads(output)
    assert value["schema_version"] == 1 and value["data"] is None
    assert value["error"]["retryable"] is False
    assert "canary" not in output and "Traceback" not in output
