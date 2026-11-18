"""Explicit research CLI authority, immutable keys and dependency-free help."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api import research_cli as module
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import RevisionConflictError


@pytest.mark.parametrize(
    "command",
    [
        None,
        "context",
        "need-create",
        "admit",
        "run",
        "runs",
        "inspect",
        "events",
        "result",
        "source",
        "cite",
        "compare",
        "comparison",
        "cancel",
        "worker-once",
    ],
)
def test_all_help_paths_work_without_optional_runtime_packages(command):
    args = (
        ["--workspace-id", str(uuid4()), "--principal-id", str(uuid4()), command] if command else []
    )
    process = subprocess.run(
        [sys.executable, "-S", "-m", "potbot", "research", *args, "--help"],
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
    from potbot.controller import research_workbench

    actor = WorkspaceContext(uuid4(), uuid4())
    episode, state, need, action, run, evidence = (uuid4() for _ in range(6))
    calls, credentials, failure = [], [], [False]
    sentinel = object()

    class Service:
        def __init__(self, database, *, policy=None):
            assert database is sentinel
            assert policy is None

        def __getattr__(self, name):
            def invoke(context, *args, **kwargs):
                assert context == actor
                calls.append((name, args, kwargs))
                if failure[0]:
                    raise RevisionConflictError("private-controller-secret")
                return {
                    "schema": "potbot-research-workbench/v1",
                    "scope": {
                        "workspaceId": str(actor.workspace_id),
                        "principalId": str(actor.principal_id),
                    },
                }

            return invoke

    def token():
        credentials.append(True)
        return "research-operator-fixture"

    monkeypatch.setattr(research_workbench, "ResearchWorkbench", Service)
    monkeypatch.setattr(jobs_cli, "database_context", lambda _args: (sentinel, actor))
    monkeypatch.setattr(operations_cli, "_setup_token", token)
    scope = ["--workspace-id", str(actor.workspace_id), "--principal-id", str(actor.principal_id)]
    return SimpleNamespace(**locals())


def test_admission_and_queue_preserve_original_review_and_key(rig, tmp_path, capsys):
    query = {"search_text": "literal Error42 v1.2", "required_identifiers": ["Error42", "v1.2"]}
    path = tmp_path / "query.json"
    path.write_text(json.dumps(query))
    assert (
        module.main(
            [
                *rig.scope,
                "admit",
                "--episode",
                str(rig.episode),
                "--state",
                str(rig.state),
                "--need",
                str(rig.need),
                "--context-sha256",
                "a" * 64,
                "--input",
                str(path),
                "--key",
                "review-original",
                "--confirm",
            ]
        )
        == 0
    )
    assert rig.calls[-1][0] == "admit"
    assert rig.calls[-1][1] == (
        rig.episode,
        rig.state,
        rig.need,
        "a" * 64,
        query,
        "review-original",
    )
    assert rig.calls[-1][2] == {"setup_token": "research-operator-fixture"}
    assert (
        module.main(
            [
                *rig.scope,
                "run",
                "--action",
                str(rig.action),
                "--policy-sha256",
                "b" * 64,
                "--disclosure-confirmed",
                "--key",
                "run-original",
                "--confirm",
            ]
        )
        == 0
    )
    assert rig.calls[-1] == (
        "queue",
        (rig.action, "b" * 64, "run-original"),
        {"setup_token": "research-operator-fixture", "disclosure_confirmed": True},
    )
    assert len(rig.credentials) == 2
    assert "research-operator-fixture" not in capsys.readouterr().out


def test_reads_citations_and_cancellation_do_not_load_setup_credential(rig, tmp_path, capsys):
    spec = {
        "runId": str(rig.run),
        "sourceId": "a" * 64,
        "startByte": 0,
        "endByte": 2,
        "quoteSha256": "b" * 64,
    }
    path = tmp_path / "citation.json"
    path.write_text(json.dumps(spec))
    commands = [
        ["context", "--episode", str(rig.episode), "--state", str(rig.state)],
        ["runs", "--episode", str(rig.episode), "--limit", "3", "--after", "opaque"],
        ["inspect", str(rig.run)],
        ["events", str(rig.run), "--after", "7", "--limit", "8"],
        ["result", str(rig.run)],
        ["source", str(rig.run), "a" * 64],
        ["cite", "--input", str(path)],
        ["comparison", str(rig.evidence)],
        ["cancel", str(rig.run), "--key", "cancel-original", "--confirm"],
    ]
    for args in commands:
        assert module.main([*rig.scope, *args]) == 0
    assert not rig.credentials
    assert rig.calls[-1] == ("cancel", (rig.run, "cancel-original"), {})
    assert rig.calls[-3] == ("citation", (spec,), {})
    assert "private-controller-secret" not in capsys.readouterr().out


def test_comparison_requires_exact_claims_file_wrapper(rig, tmp_path, capsys):
    path = tmp_path / "comparison.json"
    path.write_text(json.dumps({"claims": [], "fabricated_report": {}}))
    args = [
        *rig.scope,
        "compare",
        "--episode",
        str(rig.episode),
        "--need",
        str(rig.need),
        "--context-sha256",
        "a" * 64,
        "--input",
        str(path),
        "--key",
        "compare-original",
        "--confirm",
    ]
    assert module.main(args) == 1
    assert not rig.calls
    path.write_text(json.dumps({"claims": [{"key": "value", "label": "Value", "citations": []}]}))
    assert module.main(args) == 0
    assert rig.calls[0][0] == "compare"
    assert rig.calls[0][1][-1] == "compare-original"
    capsys.readouterr()


def test_effectful_commands_require_explicit_confirmation_before_runtime(rig, tmp_path):
    path = tmp_path / "query.json"
    path.write_text("{}")
    commands = [
        [
            "need-create",
            "--episode",
            str(rig.episode),
            "--state",
            str(rig.state),
            "--input",
            str(path),
            "--key",
            "original",
        ],
        [
            "run",
            "--action",
            str(rig.action),
            "--policy-sha256",
            "a" * 64,
            "--key",
            "original",
            "--confirm",
        ],
        ["cancel", str(rig.run), "--key", "original"],
    ]
    for args in commands:
        with pytest.raises(SystemExit) as error:
            module.main([*rig.scope, *args])
        assert error.value.code == 2
    assert not rig.calls and not rig.credentials


def test_private_failure_details_do_not_enter_cli_output(rig, capsys):
    rig.failure[0] = True
    assert module.main([*rig.scope, "result", str(rig.run)]) == 1
    text = capsys.readouterr().out
    assert "private-controller-secret" not in text
    assert json.loads(text)["error"]


@pytest.mark.parametrize("explicit_path", [False, True])
def test_native_worker_retains_a_live_policy_reader_not_a_frozen_file_snapshot(
    monkeypatch, tmp_path, capsys, explicit_path
):
    from dataclasses import replace

    from potbot.api import jobs_cli
    from potbot.controller import research_external_worker, research_workbench
    from potbot.research.source_policy import ExternalResearchPolicy, ResearchProviderRule

    actor = WorkspaceContext(uuid4(), uuid4())
    policy = ExternalResearchPolicy(True, (ResearchProviderRule("brave", 0, 0),), max_documents=0)
    changed = replace(policy, max_results=2)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy.to_dict()))
    database, store = object(), object()
    observed = []

    class Service:
        def __init__(self, actual, *, policy=None):
            assert actual is database
            self.fixed = policy

        def runs(self, context):
            assert context == actor
            return store

        def policy(self):
            return self.fixed if self.fixed is not None else ExternalResearchPolicy.read(path)

    class Worker:
        def __init__(self, actual, selected, *, policy_provider):
            assert actual is store and selected.fingerprint == policy.fingerprint
            self.read = policy_provider

        def run_once(self, context, worker_id):
            assert context == actor and worker_id == "native-reader"
            observed.append(self.read().fingerprint)
            path.write_text(json.dumps(changed.to_dict()))
            observed.append(self.read().fingerprint)
            return {"state": "checked_only"}

    monkeypatch.setattr(jobs_cli, "database_context", lambda _: (database, actor))
    monkeypatch.setattr(research_workbench, "ResearchWorkbench", Service)
    monkeypatch.setattr(research_external_worker, "ExternalResearchWorker", Worker)
    args = ["--workspace-id", str(actor.workspace_id), "--principal-id", str(actor.principal_id)]
    if explicit_path:
        args.extend(["--policy", str(path)])
    assert module.main([*args, "worker-once", "--worker-id", "native-reader"]) == 0
    assert observed == [policy.fingerprint, changed.fingerprint]
    assert json.loads(capsys.readouterr().out) == {"state": "checked_only"}
