"""Explicit selected-task CLI decisions, recovery identities and artifact downloads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api import task_execution_cli as module
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import RevisionConflictError


@pytest.mark.parametrize(
    "command",
    [
        None,
        "context",
        "check",
        "run",
        "jobs",
        "job",
        "events",
        "result",
        "artifact",
        "download",
        "cancel",
    ],
)
def test_all_help_paths_work_without_optional_runtime_dependencies(command):
    command_args = (
        ["--workspace-id", str(uuid4()), "--principal-id", str(uuid4()), command] if command else []
    )
    process = subprocess.run(
        [sys.executable, "-S", "-m", "potbot", "task-execution", *command_args, "--help"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    assert "usage:" in process.stdout


@pytest.fixture
def rig(monkeypatch):
    from potbot.api import jobs_cli, operations_cli
    from potbot.controller import task_execution

    actor = WorkspaceContext(uuid4(), uuid4())
    selection, job, artifact_id = uuid4(), uuid4(), uuid4()
    calls, credentials, response, failure = [], [], [None], [False]
    operation = "a" * 64
    artifact = b'{"type_ref":"synny://type/potbot-data-text","value":"fixture"}'

    class Service:
        def __init__(self, database):
            assert database is sentinel

        def __getattr__(self, name):
            def invoke(context, *args, **kwargs):
                assert context == actor
                calls.append((name, args, kwargs))
                if failure[0]:
                    raise RevisionConflictError("private-controller-detail")
                if response[0] is not None:
                    return response[0]
                value = {
                    "schema": "potbot-task-execution/v1",
                    "workspaceId": str(actor.workspace_id),
                    "principalId": str(actor.principal_id),
                    "operationId": operation,
                    "selectionId": str(selection),
                    "job": {"id": str(job)},
                }
                if name == "artifact":
                    value.update(
                        artifact={
                            "id": str(artifact_id),
                            "sha256": hashlib.sha256(artifact).hexdigest(),
                            "bytes": len(artifact),
                            "mediaType": "application/json",
                        },
                        offset=0,
                        nextOffset=None,
                        dataBase64=base64.b64encode(artifact).decode(),
                    )
                return value

            return invoke

    def token():
        credentials.append(True)
        return "selected-task-native-fixture-capability"

    sentinel = object()
    monkeypatch.setattr(task_execution, "TaskExecutionService", Service)
    monkeypatch.setattr(jobs_cli, "database_context", lambda _args: (sentinel, actor))
    monkeypatch.setattr(operations_cli, "_setup_token", token)
    scope = ["--workspace-id", str(actor.workspace_id), "--principal-id", str(actor.principal_id)]
    return SimpleNamespace(**locals())


def test_check_files_and_exact_launch_identity_are_forwarded_once(rig, tmp_path, capsys):
    value = rig
    request = {"job_type": "model.generate", "input": {"prompt": "fixture request"}}
    acceptance = {
        "schema": "potbot-task-result-oracle/v1",
        "assertions": [{"op": "equals", "pointer": "/text", "value": "expected"}],
    }
    request_file, acceptance_file = tmp_path / "request.json", tmp_path / "acceptance.json"
    request_file.write_text(json.dumps(request))
    acceptance_file.write_text(json.dumps(acceptance))
    assert (
        module.main(
            [
                *value.scope,
                "check",
                str(value.selection),
                "--selection-sha256",
                "b" * 64,
                "--input",
                str(request_file),
                "--acceptance",
                str(acceptance_file),
                "--key",
                "original-check",
                "--confirm",
            ]
        )
        == 0
    )
    assert value.calls[-1][1] == (value.selection, "b" * 64, request, acceptance, "original-check")
    assert len(value.credentials) == 1
    capsys.readouterr()
    args = [
        *value.scope,
        "run",
        str(value.selection),
        "--check-job",
        str(value.job),
        "--binding-sha256",
        "c" * 64,
        "--key",
        "original-run",
        "--confirm",
    ]
    for _ in range(2):
        assert module.main(args) == 0
        capsys.readouterr()
    assert (
        value.calls[-1][1]
        == value.calls[-2][1]
        == (value.selection, value.job, "c" * 64, "original-run")
    )
    assert value.calls[-1][0] == "launch"


def test_reads_and_owned_cancel_never_access_operator_vault(rig, capsys):
    value = rig
    for command in ("context", "jobs", "job", "events", "result", "artifact"):
        args = [*value.scope, command, str(value.selection)]
        if command not in {"context", "jobs"}:
            args.append(str(value.job))
        assert module.main(args) == 0
        capsys.readouterr()
    assert (
        module.main(
            [
                *value.scope,
                "cancel",
                str(value.selection),
                str(value.job),
                "--key",
                "original-cancel",
                "--confirm",
            ]
        )
        == 0
    )
    assert value.credentials == []
    assert value.calls[-1] == ("cancel", (value.selection, value.job, "original-cancel"), {})


def test_failed_mutation_does_not_retry_or_expose_private_exception(rig, capsys):
    rig.failure[0] = True
    assert (
        module.main(
            [
                *rig.scope,
                "run",
                str(rig.selection),
                "--check-job",
                str(rig.job),
                "--binding-sha256",
                "b" * 64,
                "--key",
                "kept-key",
                "--confirm",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "revision_conflict"
    assert "private-controller-detail" not in output
    assert len(rig.calls) == 1


def test_selected_download_reuses_verified_range_owner_and_refuses_overwrite(rig, tmp_path, capsys):
    target = tmp_path / "download.json"
    args = [*rig.scope, "download", str(rig.selection), str(rig.job), "--output", str(target)]
    assert module.main(args) == 0
    assert target.read_bytes() == rig.artifact
    assert json.loads(capsys.readouterr().out)["bytes"] == len(rig.artifact)
    assert rig.credentials == []
    assert module.main(args) == 1
    capsys.readouterr()
    assert target.read_bytes() == rig.artifact


def test_unconfirmed_control_is_rejected_before_database_or_vault(rig):
    with pytest.raises(SystemExit) as stopped:
        module.main(
            [
                *rig.scope,
                "run",
                str(rig.selection),
                "--check-job",
                str(rig.job),
                "--binding-sha256",
                "b" * 64,
                "--key",
                "once",
            ]
        )
    assert stopped.value.code == 2
    assert rig.calls == rig.credentials == []
