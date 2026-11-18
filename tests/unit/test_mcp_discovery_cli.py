"""Dependency-free help and no-dispatch recovery for native MCP reconciliation."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api.mcp_discovery_cli import _parser, _reconcile

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "command",
    [
        None,
        "bindings",
        "binding",
        "entries",
        "reference",
        "attempts",
        "attempt",
        "admit",
        "control",
        "request",
        "reconcile",
        "cancel",
    ],
)
def test_help_does_not_import_optional_packages_or_open_a_native_runtime(command):
    args = [sys.executable, "-S", "-m", "potbot", "mcp-discovery"]
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
    assert "usage:" in result.stdout and "Traceback" not in result.stdout + result.stderr


def test_native_reconciliation_requires_exact_review_and_existing_host_configuration(capsys):
    with pytest.raises(SystemExit) as stopped:
        _parser().parse_args(
            [
                "--workspace-id",
                str(uuid4()),
                "--principal-id",
                str(uuid4()),
                "reconcile",
                str(uuid4()),
                "--confirm",
            ]
        )
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    for flag in ("--revision", "--digest", "--profile", "--manifest", "--workspace-name", "--key"):
        assert flag in error


@pytest.mark.parametrize("state", ["running", "succeeded", "failed", "cancelled", "indeterminate"])
def test_exact_retry_recovers_before_any_native_import_or_configuration(state):
    args = SimpleNamespace(identity=uuid4(), revision=1, digest="a" * 64, key="native-once")
    receipt = {"attempt": {"id": str(args.identity), "status": state}}
    calls = []

    def replay(*values, **options):
        calls.append((values, options))
        return receipt

    service = SimpleNamespace(execution_replay=replay)
    context = object()
    assert asyncio.run(_reconcile(service, object(), context, args, "native-token")) == receipt
    assert calls == [
        (
            (context, args.identity, args.revision, args.digest, args.key),
            {"setup_token": "native-token"},
        )
    ]
