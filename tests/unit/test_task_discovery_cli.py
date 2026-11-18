"""Explicit offline refinement, exact replay and retained CLI read boundaries."""

from __future__ import annotations

import builtins
import copy
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot.api import task_discovery_cli as module
from potbot.domain.auth import WorkspaceContext
from potbot.domain.embeddings import SOURCE_TEXT_INPUT_FORMAT
from potbot.domain.errors import (
    InvalidInputError,
    ModelUnavailableError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.memory.embedding_profile import EmbeddingProfile

ROOT = Path(__file__).resolve().parents[2]


def creation():
    return {
        "episodeId": str(uuid4()),
        "stateId": str(uuid4()),
        "contextSha256": "a" * 64,
        "query": "Find a database reader",
        "constraints": {},
        "mode": "lexical",
        "limit": 5,
        "embedding": None,
        "confirm": True,
    }


@pytest.mark.parametrize(
    "command",
    [
        None,
        "context",
        "searches",
        "search",
        "result",
        "selection",
        "inspect",
        "create",
        "cancel",
        "select",
        "refine",
    ],
)
def test_help_needs_no_optional_packages_database_or_native_runtime(command):
    args = [sys.executable, "-S", "-m", "potbot", "task-discovery"]
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


@pytest.mark.parametrize(
    "status", ["running", "completed", "selected", "failed", "cancelled", "indeterminate"]
)
def test_exact_replay_precedes_native_imports_and_file_inspection(monkeypatch, status):
    args = SimpleNamespace(identity=uuid4(), revision=1, digest="a" * 64, key="native-once")
    receipt = {"search": {"id": str(args.identity), "status": status}}
    calls = []

    def replay(*values, **options):
        calls.append((values, options))
        return receipt

    original_import = builtins.__import__

    def no_native(name, *values, **options):
        if name.startswith(
            (
                "potbot.memory",
                "potbot.workspace_sandbox",
                "potbot.dependencies",
                "torch",
                "transformers",
            )
        ):
            raise AssertionError("Exact replay must not import or configure native work")
        return original_import(name, *values, **options)

    monkeypatch.setattr(builtins, "__import__", no_native)
    service, context = SimpleNamespace(execution_replay=replay), object()
    assert module._execute(service, object(), context, args, "private-token") == receipt
    assert calls == [
        (
            (context, args.identity, args.revision, args.digest, args.key),
            {"setup_token": "private-token"},
        )
    ]


@pytest.fixture
def routing(monkeypatch):
    from potbot.api import jobs_cli, operations_cli

    context = WorkspaceContext(uuid4(), uuid4())
    database, calls, credential_reads = object(), [], []
    token = "private-task-discovery-cli-canary-" + "x" * 40

    class Service:
        def __getattr__(self, name):
            def invoke(actor, *args, **kwargs):
                assert actor is context
                calls.append((name, args, kwargs))
                return {"schema": "potbot-task-discovery/v1", "command": name}

            return invoke

    service = Service()

    def setup_token():
        credential_reads.append(True)
        return token

    monkeypatch.setattr(jobs_cli, "database_context", lambda args: (database, context))
    monkeypatch.setattr(operations_cli, "_setup_token", setup_token)
    monkeypatch.setattr(
        module, "_service", lambda selected: service if selected is database else None
    )
    scope = [
        "--workspace-id",
        str(context.workspace_id),
        "--principal-id",
        str(context.principal_id),
    ]
    return SimpleNamespace(**locals())


@pytest.mark.parametrize(
    "command", ["context", "searches", "search", "result", "selection", "inspect"]
)
def test_retained_reads_never_resolve_setup_or_native_inference(
    routing, monkeypatch, capsys, command
):
    r = routing
    identity, state = uuid4(), uuid4()
    tail = [command]
    if command == "context":
        tail += ["--episode", str(identity), "--state", str(state)]
    elif command == "searches":
        tail += ["--episode", str(identity), "--limit", "12", "--after", str(state)]
    else:
        tail += [str(identity)]
        if command == "inspect":
            tail += ["b" * 64]
    original_import = builtins.__import__

    def no_native(name, *args, **kwargs):
        if name.startswith(("potbot.memory", "potbot.workspace_sandbox", "torch", "transformers")):
            raise AssertionError("A retained read cannot configure a native embedding owner")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_native)
    assert module.main([*r.scope, *tail]) == 0
    assert r.credential_reads == []
    assert [call[0] for call in r.calls] == [
        "inspect_candidate" if command == "inspect" else command
    ]
    assert r.token not in capsys.readouterr().out


def test_create_reads_bounded_exact_json_and_preserves_original_command(routing, tmp_path, capsys):
    r = routing
    value = creation()
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    assert (
        module.main([*r.scope, "create", "--input", str(path), "--key", "create-once", "--confirm"])
        == 0
    )
    assert r.calls == [("create", (value, "create-once"), {"setup_token": r.token})]
    assert r.credential_reads == [True] and r.token not in capsys.readouterr().out


@pytest.mark.parametrize(
    "invalid", [b" " * 8193, b'{"confirm":true,"confirm":true}', b'{"confirm":true,"score":NaN}']
)
def test_ambiguous_or_unbounded_create_files_never_reach_controller(
    routing, tmp_path, capsys, invalid
):
    path = tmp_path / "request.json"
    path.write_bytes(invalid)
    assert (
        module.main(
            [*routing.scope, "create", "--input", str(path), "--key", "create-once", "--confirm"]
        )
        == 1
    )
    assert not routing.calls
    assert str(path) not in capsys.readouterr().out


def test_create_cannot_upload_scores(routing, tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps({**creation(), "scores": [{"id": "a" * 64, "scoreMicros": 1_000_000}]})
    )
    assert (
        module.main(
            [*routing.scope, "create", "--input", str(path), "--key", "upload", "--confirm"]
        )
        == 1
    )
    assert not routing.calls
    assert "scores" not in capsys.readouterr().out


@pytest.mark.parametrize("command", ["select", "cancel"])
def test_control_flags_keep_exact_command_identity_and_separate_setup(routing, capsys, command):
    r = routing
    identity = uuid4()
    tail = [
        command,
        str(identity),
        "--revision",
        "3",
        "--digest",
        "a" * 64,
        "--key",
        "review-once",
        "--confirm",
    ]
    if command == "select":
        tail += ["--candidate", "b" * 64]
    assert module.main([*r.scope, *tail]) == 0
    assert r.calls == [
        (
            command,
            (identity, 3, "a" * 64, *(("b" * 64,) if command == "select" else ()), "review-once"),
            {"setup_token": r.token},
        )
    ]
    assert r.token not in capsys.readouterr().out


def test_native_flags_require_exact_review_and_existing_offline_model(capsys):
    with pytest.raises(SystemExit) as stopped:
        module._parser().parse_args(
            ["--workspace-id", str(uuid4()), "--principal-id", str(uuid4()), "refine", str(uuid4())]
        )
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    for flag in ("--revision", "--digest", "--model-dir", "--key", "--confirm"):
        assert flag in error


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    from potbot import workspace_sandbox
    from potbot.memory.local_encoder import MANIFEST, MODEL_FILES

    sandbox = workspace_sandbox.WorkspaceSandbox.prepare(tmp_path / "workspace")
    profile = EmbeddingProfile(max_input_tokens=256, max_batch_size=2)
    embedding = {
        "spaceId": str(uuid4()),
        "embeddingName": "general_text",
        "profileSha256": profile.fingerprint,
        "maxCandidates": 4,
        "runtimeBudgetMs": 1000,
    }
    row = {
        "content": profile.to_dict(),
        "content_hash": profile.fingerprint,
        "input_format": SOURCE_TEXT_INPUT_FORMAT,
    }
    calls = []

    class Database:
        @contextmanager
        def transaction(self, context):
            calls.append(context)
            yield SimpleNamespace(execute=lambda *_args: SimpleNamespace(fetchone=lambda: row))

    database, context = Database(), WorkspaceContext(uuid4(), uuid4())
    monkeypatch.setattr(workspace_sandbox, "load_workspace_sandbox", lambda db, ctx: sandbox)
    model = sandbox.path("models") / "controlled-model"
    model.mkdir()
    for name in (*MODEL_FILES, MANIFEST):
        (model / name).write_text("explicit preflight presence fixture; not a usable model")
    args = SimpleNamespace(model_dir=model, artifacts_root=None)
    return SimpleNamespace(**locals())


def test_preflight_uses_retained_profile_and_offline_files_without_weight_load_or_download(
    preflight, monkeypatch
):
    r = preflight
    original_import = builtins.__import__

    def no_compute(name, *args, **kwargs):
        if name.startswith(
            ("torch", "transformers", "potbot.dependencies", "potbot.memory.supervised_encoder")
        ):
            raise AssertionError("Preflight cannot load weights or invoke setup/download")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_compute)
    native = module._prepare_native(r.database, r.context, r.args, r.embedding)
    assert native["profile"] == r.profile and native["model"] == r.model
    assert native["sandbox"] is r.sandbox
    assert not native["artifacts"].exists()


def test_missing_offline_model_never_claims_or_downloads(preflight, monkeypatch):
    r = preflight
    r.args.model_dir = r.sandbox.path("models") / "missing"
    r.args.identity, r.args.revision, r.args.digest, r.args.key = (
        uuid4(),
        1,
        "a" * 64,
        "refine-once",
    )
    selected = {
        "revision": 1,
        "bindingSha256": "a" * 64,
        "status": "requested",
        "mode": "hybrid",
        "approvalValid": True,
        "authorityCurrent": True,
        "embedding": r.embedding,
    }
    service = SimpleNamespace(
        execution_replay=lambda *_args, **_kwargs: None, search=lambda *_args: {"search": selected}
    )
    # There is deliberately no claim method: missing files must fail before admission.
    with pytest.raises(ModelUnavailableError):
        module._execute(service, r.database, r.context, r.args, "private-token")
    assert not r.args.model_dir.exists()


def test_preflight_rejects_model_symlink_escape_and_profile_mismatch(preflight, tmp_path):
    r = preflight
    outside = tmp_path / "outside-model"
    outside.mkdir()
    link = r.sandbox.path("models") / "redirected"
    link.symlink_to(outside, target_is_directory=True)
    r.args.model_dir = link
    with pytest.raises(ScopeDeniedError):
        module._prepare_native(r.database, r.context, r.args, r.embedding)
    r.args.model_dir = r.model
    with pytest.raises(InvalidInputError):
        module._prepare_native(
            r.database, r.context, r.args, {**r.embedding, "profileSha256": "0" * 64}
        )


@pytest.fixture
def native(monkeypatch, tmp_path):
    from potbot.memory import embeddings, supervised_encoder
    from potbot.persistence import artifacts

    context = WorkspaceContext(uuid4(), uuid4())
    database, provider, calls = object(), object(), []
    profile = EmbeddingProfile(max_input_tokens=256, max_batch_size=2)
    args = SimpleNamespace(
        identity=uuid4(),
        revision=1,
        digest="a" * 64,
        key="refine-once",
        model_dir=tmp_path / "model",
        artifacts_root=tmp_path / "artifacts",
    )
    embedding = {
        "spaceId": str(uuid4()),
        "embeddingName": "general_text",
        "profileSha256": profile.fingerprint,
        "maxCandidates": 4,
        "runtimeBudgetMs": 1000,
    }
    selected = {
        "revision": 1,
        "bindingSha256": "a" * 64,
        "status": "requested",
        "mode": "hybrid",
        "approvalValid": True,
        "authorityCurrent": True,
        "embedding": embedding,
    }
    claim = {
        "workerToken": str(uuid4()),
        "repositoryId": str(uuid4()),
        "context": {"taskBinding": {"episodeId": str(uuid4())}},
        "request": {**creation(), "mode": "hybrid", "embedding": embedding},
        "semanticCandidates": [
            {"id": "b" * 64, "name": "Second", "description": "Second exact document"},
            {"id": "a" * 64, "name": "First", "description": "First exact document"},
        ],
    }
    failure, drift = [None], [False]
    report, completed = (
        {"schema": "potbot-transient-ranking/v1"},
        {"search": {"status": "completed"}},
    )

    class Service:
        def execution_replay(self, *values, **options):
            calls.append(("replay", values, options))
            return None

        def search(self, *_args):
            return {"search": selected}

        def claim(self, *values, **options):
            calls.append(("claim", values, options))
            return copy.deepcopy(claim)

        def validate_claim(self, *values):
            calls.append(("validate", values, {}))
            current = copy.deepcopy(claim)
            if drift[0]:
                current["semanticCandidates"][0]["description"] = "changed"
            return current

        def complete(self, *values):
            calls.append(("complete", values, {}))
            return completed

        def fail(self, *values):
            calls.append(("fail", values, {}))

    class Embeddings:
        def rank_transient(self, *values, **options):
            calls.append(("rank", values, options))
            assert not options["cancelled"]()
            if failure[0] is not None:
                raise failure[0]
            return report

    def prepare(*values):
        calls.append(("preflight", values, {}))
        return {
            "profile": profile,
            "sandbox": SimpleNamespace(root=tmp_path),
            "model": args.model_dir,
            "artifacts": args.artifacts_root,
        }

    def encoder(*values, **options):
        calls.append(("supervised_provider", values, options))
        return provider

    def owner(db, store, configured_provider):
        assert db is database and configured_provider is provider
        return Embeddings()

    monkeypatch.setattr(module, "_prepare_native", prepare)
    monkeypatch.setattr(supervised_encoder, "SupervisedQwenEncoder", encoder)
    monkeypatch.setattr(embeddings, "EmbeddingService", owner)
    monkeypatch.setattr(artifacts, "ArtifactStore", lambda *_args: object())
    monkeypatch.setattr(artifacts, "LocalArtifactBackend", lambda *_args: object())
    service = Service()
    return SimpleNamespace(**locals())


def test_refine_uses_only_existing_supervised_owner_and_exact_claim(native):
    from potbot.controller.task_discovery import refinement_claim_sha256

    r = native
    assert module._execute(r.service, r.database, r.context, r.args, "private-token") == r.completed
    names = [row[0] for row in r.calls]
    assert (
        names.index("replay")
        < names.index("preflight")
        < names.index("claim")
        < names.index("supervised_provider")
        < names.index("rank")
        < names.index("complete")
    )
    claim_call = next(row for row in r.calls if row[0] == "claim")
    assert claim_call[2] == {"setup_token": "private-token", "fresh_only": True}
    _name, values, options = next(row for row in r.calls if row[0] == "rank")
    assert str(values[1]) == r.claim["request"]["episodeId"]
    assert values[3:] == (
        r.claim["request"]["query"],
        (("a" * 64, "First exact document"), ("b" * 64, "Second exact document")),
    )
    assert str(options["repository_id"]) == r.claim["repositoryId"]
    assert options["runtime_budget_ms"] == 1000 and options["embedding_name"] == "general_text"
    assert options["request_binding_sha256"] == refinement_claim_sha256(
        r.context, r.args.identity, UUID(r.claim["workerToken"])
    )
    assert not any(name == "fail" for name in names)


@pytest.mark.parametrize(
    "state", ["running", "completed", "selected", "failed", "cancelled", "indeterminate"]
)
def test_old_claims_never_rerun_under_a_new_command_key(native, state):
    r = native
    r.selected["status"] = state
    with pytest.raises(RevisionConflictError):
        module._execute(r.service, r.database, r.context, r.args, "private-token")
    assert [row[0] for row in r.calls] == ["replay"]


@pytest.mark.parametrize(
    "uncertain,expected",
    [(True, "search_indeterminate"), (False, "search_failed"), (None, "search_indeterminate")],
)
def test_native_failure_preserves_embedding_usage_certainty(native, uncertain, expected):
    r = native
    error = (
        RuntimeError("private local provider diagnostic")
        if uncertain is None
        else InvalidInputError("provider response rejected")
    )
    if uncertain is not None:
        error.embedding_usage_indeterminate = uncertain
    r.failure[0] = error
    with pytest.raises(type(error)):
        module._execute(r.service, r.database, r.context, r.args, "private-token")
    assert next(row for row in r.calls if row[0] == "fail")[1][-1] == expected
    assert not any(row[0] == "complete" for row in r.calls)


def test_claim_drift_blocks_provider_creation_and_records_authority_change(native):
    r = native
    r.drift[0] = True
    with pytest.raises(ScopeDeniedError):
        module._execute(r.service, r.database, r.context, r.args, "private-token")
    assert next(row for row in r.calls if row[0] == "fail")[1][-1] == "authority_changed"
    assert not any(row[0] in {"rank", "supervised_provider", "complete"} for row in r.calls)
