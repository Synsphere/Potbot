"""Reject incomplete or misleading profiler evidence without qualifying a host."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.tools.execution_contracts import ExecutionPolicy
from potbot.tools.python_profile_worker import summarize
from scripts import qualify_python_profile_cog as gate


def policy() -> dict:
    return {
        "profile": gate.SCHEMA,
        **{name: str(uuid4()) for name in gate._IDS},
        **{name: {"path": "/private/" + name, "sha256": "a" * 64} for name in gate._FILES},
        "source_file": "python_profile_workload.py",
        "source_sha256": "b" * 64,
        "interpreter_sha256": "c" * 64,
        "execution_policy": ExecutionPolicy("d" * 64, "e" * 64, "f" * 64, "test").to_dict(),
        "repetitions": 2,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"source_file": "../escape.py"},
        {"source_file": "/home/work.py"},
        {"repetitions": True},
        {"repetitions": 99},
        {"agent_id": "arbitrary-agent"},
        {"development_syn_fixture": True},
        {"argv": ["/bin/sh", "-c", "true"]},
        {"cosign": {"path": "/private/../cosign", "sha256": "a" * 64}},
        {"cosign": {"path": "/private/cosign", "sha256": "0"}},
    ],
)
def test_policy_cannot_expand_the_installed_qualification_scope(change):
    with pytest.raises((ValueError, InvalidInputError)):
        gate.parse_policy({**policy(), **change})


def test_policy_requires_exact_execution_limits_and_an_independent_copy():
    original = policy()
    parsed = gate.parse_policy(original)
    parsed["execution_policy"]["max_wall_ms"] = 60_000
    assert original["execution_policy"]["max_wall_ms"] == 30_000
    original["execution_policy"]["max_wall_ms"] = 3_600_000
    with pytest.raises(ValueError, match="bounded installed"):
        gate.parse_policy(original)


def smoke() -> tuple[dict, dict]:
    workspace = {"image_digest": "a" * 64, "qualified_smoke": True, "host_check": "recorded"}
    qualification = hashlib.sha256(canonical_json(workspace).encode()).hexdigest()
    evidence = {
        "schema": "potbot.published-image-hosted-smoke/1",
        "qualified_smoke": True,
        "image_digest": "a" * 64,
        "consumer_commit": "b" * 40,
        "kernel_release": "qualified-kernel",
        "operator_gate": {"qualified": True},
        "taskstats": {"qualified": True},
        "workspace_smoke": {**workspace, "receipt_sha256": qualification},
    }
    arguments = {
        "image_digest": "a" * 64,
        "qualification": qualification,
        "consumer": "b" * 40,
        "kernel": "qualified-kernel",
    }
    return evidence, arguments


@pytest.mark.parametrize(
    "change",
    [
        {"qualified_smoke": 1},
        {"image_digest": "c" * 64},
        {"consumer_commit": "c" * 40},
        {"kernel_release": "another-kernel"},
        {"operator_gate": {"qualified": False}},
        {"taskstats": {"qualified": False}},
    ],
)
def test_stale_or_incomplete_image_smoke_cannot_qualify_a_profiler(change):
    evidence, arguments = smoke()
    gate.check_image_smoke(evidence, **arguments)
    with pytest.raises(ValueError, match="does not qualify"):
        gate.check_image_smoke({**evidence, **change}, **arguments)


def test_tampered_workspace_receipt_is_not_accepted_by_its_old_hash():
    evidence, arguments = smoke()
    evidence["workspace_smoke"]["host_check"] = "changed"
    with pytest.raises(ValueError, match="receipt differs"):
        gate.check_image_smoke(evidence, **arguments)


def comparison() -> dict:
    expected = {
        "uid": 555,
        "gid": 69,
        "total": sum((i * i) % 101 for i in range(200_000)),
        "network_denied": True,
        "root_write_denied": True,
    }
    result_pin = hashlib.sha256(canonical_json(expected).encode()).hexdigest()
    baseline = {
        "python": "3.12.13",
        "wall_ns": 200,
        "cpu_ns": 100,
        "result_sha256": result_pin,
        "functions": [],
        "function_count": 0,
    }
    profiled = {
        **baseline,
        "wall_ns": 400,
        "cpu_ns": 250,
        "function_count": 1,
        "functions": [
            {
                "function": "item_cost",
                "line": 27,
                "calls": 200_000,
                "primitive_calls": 200_000,
                "self_ns": 30,
                "cumulative_ns": 40,
            }
        ],
    }
    return summarize(
        {
            "baseline": [copy.deepcopy(baseline) for _ in range(2)],
            "profile": [copy.deepcopy(profiled) for _ in range(2)],
        },
        "b" * 64,
        "c" * 64,
        2,
    )


def check(value):
    gate.check_comparison(
        value,
        source_sha256="b" * 64,
        interpreter_sha256="c" * 64,
        uid=555,
        gid=69,
        repetitions=2,
    )


def test_profiler_evidence_requires_registered_identity_and_equal_outputs():
    value = comparison()
    check(value)
    with pytest.raises(ValueError, match="workload or identity"):
        gate.check_comparison(
            value,
            source_sha256="b" * 64,
            interpreter_sha256="c" * 64,
            uid=556,
            gid=69,
            repetitions=2,
        )
    value["samples"]["profile"][0]["result_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="outputs differ"):
        check(value)


@pytest.mark.parametrize(
    "case", ["missing_sample", "missing_hotspot", "wrong_calls", "false_median"]
)
def test_comparison_does_not_accept_a_plausible_but_incomplete_report(case):
    value = comparison()
    if case == "missing_sample":
        value["samples"]["profile"].pop()
    elif case == "missing_hotspot":
        value["samples"]["profile"][0]["functions"][0]["function"] = "unrelated"
    elif case == "wrong_calls":
        value["samples"]["profile"][0]["functions"][0]["calls"] = 200_001
    else:
        value["median_ns"]["baseline"]["cpu_ns"] += 1
    with pytest.raises(ValueError):
        check(value)


def test_image_verification_pins_the_official_publisher_and_does_not_leak_dsn(monkeypatch):
    calls = []
    evidence = [{"critical": {"image": {"docker-manifest-digest": "sha256:" + "a" * 64}}}]

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(evidence).encode(), stderr=b"")

    monkeypatch.setattr(gate.subprocess, "run", run)
    monkeypatch.setattr(gate.os, "access", lambda *_: True)
    monkeypatch.setenv("POTBOT_PYTHON_PROFILE_DSN", "postgresql://private-password")
    gate.verify_image_signature(Path("/pinned/cosign"), "a" * 64)
    command, options = calls[0]
    assert command == [
        "/pinned/cosign",
        "verify",
        "--certificate-identity",
        gate.SIGNER,
        "--certificate-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        gate.IMAGE + "@sha256:" + "a" * 64,
    ]
    assert "POTBOT_PYTHON_PROFILE_DSN" not in options["env"]
    evidence[0]["critical"]["image"]["docker-manifest-digest"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="admitted digest"):
        gate.verify_image_signature(Path("/pinned/cosign"), "a" * 64)


def test_signature_verifier_failure_cannot_become_qualification(monkeypatch):
    monkeypatch.setattr(gate.os, "access", lambda *_: True)
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"[]",
            stderr=b"private path or network diagnostic",
        ),
    )
    with pytest.raises(ValueError, match="could not be verified"):
        gate.verify_image_signature(Path("/pinned/cosign"), "a" * 64)


def test_source_pins_reject_dirty_qualification_code(tmp_path, monkeypatch):
    name = "scripts/probe.py"
    source = tmp_path / name
    source.parent.mkdir()
    source.write_text("print('committed')\n")
    for arguments in (
        ["init", "--quiet"],
        ["add", name],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "pin fixture",
        ],
    ):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setattr(gate, "SOURCES", (name,))
    head, hashes = gate.committed_sources(tmp_path)
    assert len(head) == 40 and hashes[name] == hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_text("print('unreviewed')\n")
    with pytest.raises(ValueError, match="committed consumer"):
        gate.committed_sources(tmp_path)


@pytest.mark.parametrize("change", ["dirty_authority", "untracked_module", "alternate_import"])
def test_consumer_pin_covers_unlisted_authority_code_and_import_origins(
    tmp_path, monkeypatch, change
):
    source = tmp_path / "scripts/probe.py"
    source.parent.mkdir()
    source.write_text("print('qualification')\n")
    package = tmp_path / "src/potbot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Fixture package."""\n')
    authority = package / "authority.py"
    authority.write_text("ALLOW = False\n")
    for arguments in (
        ["init", "--quiet"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "pin package",
        ],
    ):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)
    package_module = SimpleNamespace(__file__=str(package / "__init__.py"))
    modules = {
        "potbot": package_module,
        "potbot.authority": SimpleNamespace(__file__=str(authority)),
    }
    monkeypatch.setattr(gate, "SOURCES", ("scripts/probe.py",))
    monkeypatch.setattr(gate, "importlib", SimpleNamespace(import_module=lambda _: package_module))
    monkeypatch.setattr(gate, "sys", SimpleNamespace(modules=modules))
    _, hashes = gate.committed_sources(tmp_path)
    assert len(hashes["package_tree_sha256"]) == 64
    if change == "dirty_authority":
        authority.write_text("ALLOW = True\n")
    elif change == "untracked_module":
        (package / "shadow.py").write_text("ALLOW = True\n")
    else:
        outside = tmp_path / "authority.py"
        outside.write_bytes(authority.read_bytes())
        modules["potbot.authority"] = SimpleNamespace(__file__=str(outside))
    with pytest.raises(ValueError):
        gate.committed_sources(tmp_path)


def test_consumer_changed_during_live_run_cannot_emit_success(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    selected = tmp_path / "policy.json"
    selected.write_text(json.dumps(policy()))
    selected.chmod(0o600)
    output = tmp_path / "result.json"
    pins = iter((("a" * 40, {}), ("b" * 40, {})))
    monkeypatch.setattr(gate, "committed_sources", lambda: next(pins))

    async def completed(*_args, **_kwargs):
        return {"qualified": True}

    monkeypatch.setattr(gate, "qualify", completed)
    assert gate.main(["--policy", str(selected), "--report", str(output), "--run-live"]) == 1
    evidence = json.loads(capsys.readouterr().err)
    assert evidence["qualified"] is False and evidence["phase"] == "consumer_readback"
    assert not output.exists()


def test_missing_opt_in_never_reads_policy_or_creates_success(tmp_path, capsys):
    output = tmp_path / "result.json"
    assert gate.main(["--policy", str(tmp_path / "absent.json"), "--report", str(output)]) == 1
    evidence = json.loads(capsys.readouterr().err)
    assert evidence["qualified"] is False and evidence["phase"] == "explicit_live_opt_in"
    assert not output.exists()


def test_missing_installed_host_is_a_failure_not_a_skipped_success(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    selected = tmp_path / "policy.json"
    selected.write_text(json.dumps(policy()))
    selected.chmod(0o600)
    output = tmp_path / "result.json"
    monkeypatch.delenv("POTBOT_OPERATOR_REQUIRED", raising=False)
    monkeypatch.delenv("POTBOT_OPERATOR_SCOPED_LAUNCHER", raising=False)
    monkeypatch.setattr(gate, "committed_sources", lambda: ("b" * 40, {}))
    assert (
        gate.main(
            [
                "--policy",
                str(selected),
                "--report",
                str(output),
                "--run-live",
            ]
        )
        == 1
    )
    evidence = json.loads(capsys.readouterr().err)
    assert evidence["qualified"] is False
    assert evidence["phase"] == "installed_profile_and_live_acceptance"
    assert not output.exists()


def test_pinned_files_refuse_changed_bytes_and_writable_configuration(tmp_path):
    candidate = tmp_path / "pinned.json"
    candidate.write_bytes(b"{}")
    candidate.chmod(0o600)
    selected = {"path": str(candidate), "sha256": hashlib.sha256(b"{}").hexdigest()}
    assert gate._pinned_file(selected) == candidate
    candidate.chmod(0o666)
    with pytest.raises(ValueError, match="owner-controlled"):
        gate._pinned_file(selected)
    candidate.chmod(0o600)
    candidate.write_bytes(b"[]")
    with pytest.raises(ValueError, match="changed"):
        gate._pinned_file(selected)
    candidate.write_bytes(b"{}")
    with pytest.raises(ValueError, match="owner-controlled"):
        gate._pinned_file(selected, owner=os.geteuid() + 1)
