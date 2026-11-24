"""Sealed model profile and typed receipts using a deterministic engine fixture."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import TypedCogValue
from potbot.controller import native_cog_dispatch
from potbot.controller.native_cog_dispatch import (
    NativeCogDispatcher,
    installed_cog_operations,
    installed_model_deployment,
)
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.models.native_results import REQUEST, RESULT, parse_native_result
from potbot.models.registry import DeploymentSpec
from potbot.tools import native_model_worker as worker

PROFILE_SHA256 = "f" * 64
IMAGE_SHA256 = "d" * 64
REPO_SHA256 = "c" * 64


@pytest.fixture
def deployment():
    return DeploymentSpec(
        "fixture-model",
        "a" * 64,
        worker.BACKEND,
        worker.BACKEND_VERSION,
        "f16",
        "a" * 64,
        "b" * 64,
        worker.SCHEMA,
        "none",
        512,
        32,
        (("investigator", "model-aware-reasoning-v1"),),
        ("reasoning",),
        (RESULT,),
        cancellation="process_termination",
    )


@pytest.fixture
def installed(deployment):
    workspace = uuid4()
    operation = {
        "operation_ref": "syn://thing/native-model-fixture",
        "entrypoint": worker.EXECUTABLE,
        "input_type_ref": REQUEST,
        "output_type_ref": RESULT,
        "repository_digest": REPO_SHA256,
        "max_wall_ms": 2000,
        "model_deployment": {"deployment": deployment.to_dict(), "engine_sha256": "e" * 64},
    }
    profile = {
        "profile": "potbot.hosted-native-cog/3",
        "workspace_id": str(workspace),
        "image_digest": "sha256:" + IMAGE_SHA256,
        "qualification_sha256": "1" * 64,
        "operations": [operation],
    }
    paths = SimpleNamespace(
        workspace_id=workspace,
        image_digest=profile["image_digest"],
        qualification_sha256=profile["qualification_sha256"],
        tool_entrypoints=((worker.EXECUTABLE, "2" * 64),),
    )
    return profile, paths


def test_sealed_operation_requires_exact_installation(installed, deployment):
    profile, paths = installed
    (operation,) = installed_cog_operations(profile, PROFILE_SHA256, paths)
    assert installed_model_deployment(operation) == deployment
    assert operation.binding.kind == "model"
    assert operation.binding.effect_class == "read_only"
    assert operation.protocol == "context-rpc/v1"
    assert operation.argv[0] == worker.EXECUTABLE
    assert operation.argv[2] == "e" * 64

    profile["profile"] = "potbot.hosted-native-cog/2"
    with pytest.raises(ScopeDeniedError, match="sealed Context"):
        installed_cog_operations(profile, PROFILE_SHA256, paths)
    profile["profile"] = "potbot.hosted-native-cog/3"

    declaration = profile["operations"][0].pop("model_deployment")
    with pytest.raises(ScopeDeniedError, match="pinned deployment"):
        installed_cog_operations(profile, PROFILE_SHA256, paths)

    profile["operations"][0]["model_deployment"] = declaration
    declaration["deployment"] = replace(deployment, tokenizer_revision="3" * 40).to_dict()
    with pytest.raises(ScopeDeniedError, match="embedded tokenizer"):
        installed_cog_operations(profile, PROFILE_SHA256, paths)


@pytest.mark.skipif(os.name != "posix", reason="sealed guest uses Linux descriptor operations")
def test_same_immutable_asset_is_held_through_path_replacement(tmp_path):
    content = b"exact bytes"
    pin = hashlib.sha256(content).hexdigest()
    relative = worker.blob_path(pin)
    asset = tmp_path / relative
    asset.parent.mkdir(parents=True)
    asset.write_bytes(content)
    asset.chmod(0o444)
    with worker.verified_file(tmp_path, relative, pin, 32) as (descriptor, length):
        original = asset.with_name("original")
        asset.rename(original)
        asset.symlink_to(original)
        assert os.pread(descriptor, length, 0) == content
        with pytest.raises(OSError):
            with worker.verified_file(tmp_path, relative, pin, 32):
                pytest.fail("Symlink accepted")
    asset.unlink()
    original.rename(asset)
    with pytest.raises(ValueError, match="digest changed"):
        with worker.verified_file(tmp_path, relative, "3" * 64, 32):
            pytest.fail("Altered content pin accepted")
    asset.chmod(0o644)
    with pytest.raises(ValueError, match="immutable"):
        with worker.verified_file(tmp_path, relative, pin, 32):
            pytest.fail("Writable source accepted")


@pytest.mark.parametrize(
    "case", ["valid", "wrong-invocation", "injected-request-option", "unregistered", "alias"]
)
def test_only_exact_worker_result_reaches_artifact(monkeypatch, installed, deployment, case):
    profile, paths = installed
    (operation,) = installed_cog_operations(profile, PROFILE_SHA256, paths)
    actor = SimpleNamespace(workspace_id=paths.workspace_id)
    agent = uuid4()
    no_effects = []
    raw_input = {
        "model": deployment.canonical_model,
        "deployment_sha256": deployment.fingerprint,
        "system": "local instructions",
        "prompt": "Hello, Lin",
        "max_output_tokens": 8,
    }
    if case == "injected-request-option":
        raw_input["engine_path"] = "/unapproved"
    lease = SimpleNamespace(
        job_id=uuid4(),
        contract_revision_id=uuid4(),
        input={
            "agent_id": str(agent),
            "repository_state_id": str(uuid4()),
            "operation_ref": operation.binding.operation_ref,
            "profile_sha256": PROFILE_SHA256,
            "type_ref": REQUEST.replace("syn://", "synny://") if case == "alias" else REQUEST,
            "value": raw_input,
        },
    )

    def lookup(query, _params):
        if "model_deployments" in query:
            record = None if case == "unregistered" else {"content": deployment.to_dict()}
        else:
            record = {"content_digest": REPO_SHA256}
        return SimpleNamespace(fetchone=lambda: record)

    @contextmanager
    def transaction(selected_actor):
        assert selected_actor is actor
        yield SimpleNamespace(execute=lookup)

    def dispatch(selected_actor, selected_lease, **pins):
        assert selected_actor is actor
        assert selected_lease is lease
        assert pins["cog_model_deployment"] == deployment
        no_effects.append("dispatch")

    def run_inference(request, pinned_deployment, engine_pin):
        assert request == raw_input
        assert pinned_deployment == deployment.to_dict()
        assert engine_pin == operation.argv[2]
        no_effects.append("infer-once")
        return (
            "Bounded result, including café",
            "length",
            {
                "input_tokens": 12,
                "output_tokens": 8,
                "total_tokens": 20,
            },
        )

    async def execute(_operation, typed, cog, *_args, syn_authorization):
        assert syn_authorization == "grant"
        envelope = {
            "schema_version": 1,
            "invocation_id": str(uuid4() if case == "wrong-invocation" else cog.invocation_id),
            "operation_ref": _operation.binding.operation_ref,
            "input": {"type_ref": typed.type_ref, "value": typed.value},
        }
        results = []
        worker.run_work(
            SimpleNamespace(
                work_input=lambda: envelope,
                finish_work=lambda port, value, **_: results.append(TypedCogValue(port, value)),
            ),
            json.loads(operation.argv[1]),
            operation.argv[2],
        )
        (result,) = results
        return result

    def put(selected_actor, raw, media_type):
        assert selected_actor is actor
        assert media_type == "application/json"
        result = parse_native_result(
            raw, request=raw_input, job_id=lease.job_id, deployment=deployment
        )
        assert result["finish_reason"] == "length"
        assert result["request_sha256"] == content_hash(raw_input)
        no_effects.append("publish-once")
        return {"id": artifact_id}

    executor = SimpleNamespace(
        authorize_syn=lambda *_: "grant", execute=execute, _executable=lambda *_: None
    )
    jobs = SimpleNamespace(
        database=SimpleNamespace(transaction=transaction),
        cancelled=lambda *args: False,
        mark_dispatched=dispatch,
    )
    artifact_id = uuid4()
    monkeypatch.setattr(worker, "run_inference", run_inference)
    monkeypatch.setattr(native_cog_dispatch, "_verify_native_model_assets", lambda *_: None)
    monkeypatch.setattr(
        native_cog_dispatch,
        "load_workspace_sandbox",
        lambda *_: SimpleNamespace(path=lambda *_: ""),
    )
    monkeypatch.setattr(native_cog_dispatch, "LocalArtifactBackend", lambda *_: None)
    monkeypatch.setattr(native_cog_dispatch, "ArtifactStore", lambda *_: SimpleNamespace(put=put))
    dispatcher = NativeCogDispatcher(executor, (operation,), frozenset({agent}), PROFILE_SHA256)
    if case == "injected-request-option":
        with pytest.raises(InvalidInputError, match="unsupported fields"):
            dispatcher.run(jobs, actor, lease, threading.Event())
        assert no_effects == []
    elif case in {"unregistered", "alias"}:
        with pytest.raises(ScopeDeniedError, match="exact installed request port|not registered"):
            dispatcher.run(jobs, actor, lease, threading.Event())
        assert no_effects == []
    elif case == "wrong-invocation":
        with pytest.raises(StateMismatchError, match="different invocation"):
            dispatcher.run(jobs, actor, lease, threading.Event())
        assert no_effects == ["dispatch", "infer-once"]
    else:
        result = dispatcher.run(jobs, actor, lease, threading.Event())
        assert result == {
            "operation_id": str(lease.job_id),
            "artifact_id": str(artifact_id),
            "usage": {"input_tokens": 12, "output_tokens": 8, "tokens": 20},
        }
        assert no_effects == ["dispatch", "infer-once", "publish-once"]
