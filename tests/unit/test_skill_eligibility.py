"""Real registry/pin/lease owners with explicit process fixtures; not live qualification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context.skill_eligibility import (
    SkillEligibilityEvaluator,
    configured_skill_eligibility,
    parse_eligibility_report,
    parse_requirements,
    requirements_digest,
)
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    OperationCancelledError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_tool_release_markers import write_hold
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.skill_dependency_probe import PROBE_PROGRAM, PROBE_SCHEMA
from potbot.tools.supervisor import ProcessResult, ProcessSupervisor
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger


class FixtureSupervisor(ProcessSupervisor):
    def __init__(self, backend):
        super().__init__(backend)
        self.calls = []
        self.available = {"python3", "curl"}
        self.environment = {"PATH"}
        self.during = lambda: None
        self.result_changes = {}
        self.observation_change = lambda _value: None

    def run(self, request, *, cancelled=None, redactor=None):
        assert cancelled is not None and not cancelled()
        self.calls.append(request)
        self.during()
        selected = json.loads(request.stdin)
        observed = {
            "schema": PROBE_SCHEMA,
            "operatingSystem": "linux",
            "uid": request.guest_uid,
            "gid": request.guest_gid,
            "binaries": [
                {
                    "name": name,
                    "state": "present" if name in self.available else "missing",
                    "sha256": "f" * 64 if name in self.available else None,
                }
                for name in selected["binaries"]
            ],
            "environment": [
                {"name": name, "present": name in self.environment}
                for name in selected["environment"]
            ],
        }
        self.observation_change(observed)
        result = ProcessResult(
            "completed",
            0,
            json.dumps(observed).encode(),
            b"",
            5,
            False,
            False,
            True,
            "d" * 64,
            processes_stopped=True,
        )
        return replace(result, **self.result_changes)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv("POTBOT_OPERATOR_SOCKET", raising=False)
    monkeypatch.delenv("POTBOT_OPERATOR_REQUIRED", raising=False)
    private, home, rootfs, tools, staging, models, config, history = [
        tmp_path / name
        for name in ("private", "home", "rootfs", "tools", "staging", "models", "config", "history")
    ]
    for path in (private, home, rootfs, tools, staging, models, config, history):
        path.mkdir()
    interpreter = rootfs / "usr/bin/python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"explicit non-executed image interpreter fixture")
    interpreter.chmod(0o755)
    context = WorkspaceContext(uuid4(), uuid4())
    agent_id = uuid4()
    ledger = WorkspaceLeaseLedger(private / "ledger.sqlite", workspace_home=home)
    ledger.pin_image(
        context.workspace_id,
        digest="a" * 64,
        image_ref="ghcr.io/test/image@sha256:" + "a" * 64,
        build_commit="b" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256="c" * 64,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="fixture")
    homes.provision(context.workspace_id, agent_id)
    runtime = SharedWorkspaceRuntime(
        "a" * 64,
        rootfs,
        home,
        "c" * 64,
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
        ai_config_dir=config,
        ai_history_dir=history,
    )
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    backend.operator_attachment = SimpleNamespace(
        require_healthy=lambda: {"epoch": "fixture-operator-epoch"}, healthy=lambda: True
    )
    supervisor = FixtureSupervisor(backend)
    now = datetime(2026, 11, 18, tzinfo=UTC)
    evaluator = SkillEligibilityEvaluator(
        runtime=runtime,
        ledger=ledger,
        homes=homes,
        supervisor=supervisor,
        probe_executable_sha256=hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        clock=lambda: now,
    )
    return SimpleNamespace(**locals())


def evaluate(rig, requirements=(), **changes):
    arguments = {
        "check_id": uuid4(),
        "agent_id": rig.agent_id,
        "candidate_sha256": "1" * 64,
        "source_binding_sha256": "2" * 64,
        "requirements": list(requirements),
        "authorize_attempt": lambda: None,
    }
    arguments.update(changes)
    return rig.evaluator.evaluate(rig.context, **arguments)


def test_measurement_binds_actual_agent_runtime_and_literal_fixed_probe(rig):
    calls = []

    def authorize():
        active = rig.ledger.active(rig.context.workspace_id)
        assert len(active) == 1 and active[0]["operation"] == "skill.eligibility"
        calls.append(active[0]["generation"])

    requirements = [{"kind": "binary", "name": "python3"}, {"kind": "environment", "name": "PATH"}]
    report = evaluate(rig, requirements, authorize_attempt=authorize)
    assert report["eligible"] is True and len(report["checks"]) == 3
    assert calls == [calls[0], calls[0], calls[0]]
    request = rig.supervisor.calls[0]
    assert request.argv == ("/usr/bin/python3", "-I", "-S", "-B", "-c", PROBE_PROGRAM)
    assert request.guest_uid == 420 and request.guest_gid == 69
    assert request.environment == {} and request.timeout_ms == 5000
    assert request.telemetry_identity_kind == "agent"
    assert report["requirementsSha256"] == requirements_digest(requirements)
    assert report["bindings"]["workspaceId"] == str(rig.context.workspace_id)
    assert report["bindings"]["principalId"] == str(rig.context.principal_id)
    assert report["bindings"]["agentHomeRevision"] == 1
    assert report["expiresAt"] == (rig.now + timedelta(seconds=120)).isoformat()
    assert rig.ledger.active(rig.context.workspace_id) == []
    assert str(rig.rootfs) not in json.dumps(report)


def test_any_binary_and_os_are_or_clauses_not_all_required(rig):
    report = evaluate(
        rig,
        [
            {"kind": "any_binary", "name": "unavailable", "group": "any_binary"},
            {"kind": "any_binary", "name": "curl", "group": "any_binary"},
            {"kind": "operating_system", "name": "darwin"},
            {"kind": "operating_system", "name": "linux"},
        ],
    )
    assert report["eligible"] is True
    assert report["checks"][1:] == [
        {
            "kind": "any_binary",
            "name": "any declared binary",
            "status": "passed",
            "reasonCode": "any_binary_available",
        },
        {
            "kind": "operating_system",
            "name": "any declared operating system",
            "status": "passed",
            "reasonCode": "operating_system_matches",
        },
    ]


@pytest.mark.parametrize(
    "requirements,reason",
    [
        ([{"kind": "binary", "name": "unavailable"}], "binary_missing"),
        ([{"kind": "any_binary", "name": "unavailable"}], "any_binary_missing"),
        ([{"kind": "any_binary", "name": "$(nope)"}], "any_binary_unresolved"),
        ([{"kind": "operating_system", "name": "darwin"}], "operating_system_mismatch"),
        ([{"kind": "operating_system", "name": "somewhere"}], "operating_system_unsupported"),
        ([{"kind": "environment", "name": "HOME"}], "environment_missing"),
        ([{"kind": "environment", "name": "OPENAI_API_KEY"}], "environment_injection_unsupported"),
        ([{"kind": "tool", "name": "Bash(python:*)"}], "capability_alias_unsupported"),
        ([{"kind": "configuration", "name": "channels.slack"}], "configuration_gate_unsupported"),
        (
            [{"kind": "compatibility", "name": "Requires vendor runtime"}],
            "compatibility_text_unsupported",
        ),
    ],
)
def test_missing_and_unsupported_prerequisites_never_pass(rig, monkeypatch, requirements, reason):
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-guests-or-reports")
    report = evaluate(rig, requirements)
    assert report["eligible"] is False
    assert report["checks"][-1]["reasonCode"] == reason
    assert "not-for-guests-or-reports" not in json.dumps(report)
    assert "OPENAI_API_KEY" not in json.loads(rig.supervisor.calls[0].stdin)["environment"]


@pytest.mark.parametrize("after_dispatch", [False, True])
def test_fenced_authorization_stops_measurement_and_does_not_invent_uncertain_process(
    rig, after_dispatch
):
    def authorize():
        if not after_dispatch or rig.supervisor.calls:
            raise ScopeDeniedError("fenced check")

    with pytest.raises(ScopeDeniedError, match="fenced"):
        evaluate(rig, authorize_attempt=authorize)
    assert len(rig.supervisor.calls) == int(after_dispatch)
    assert rig.ledger.active(rig.context.workspace_id) == []


def test_authorization_is_rechecked_after_runtime_readback_before_dispatch(rig):
    calls = []

    def authorize():
        calls.append(True)
        if len(calls) == 2:
            raise ScopeDeniedError("fenced during runtime readback")

    with pytest.raises(ScopeDeniedError, match="readback"):
        evaluate(rig, authorize_attempt=authorize)
    assert rig.supervisor.calls == []
    assert rig.ledger.active(rig.context.workspace_id) == []


@pytest.mark.parametrize("after_dispatch", [False, True])
def test_cancelled_measurement_never_publishes_a_report(rig, after_dispatch):
    def cancelled():
        return bool(rig.supervisor.calls) if after_dispatch else True

    with pytest.raises(OperationCancelledError):
        evaluate(rig, cancelled=cancelled)
    assert len(rig.supervisor.calls) == int(after_dispatch)
    assert rig.ledger.active(rig.context.workspace_id) == []


@pytest.mark.parametrize("stopped", [True, False])
def test_failed_or_unconfirmed_probe_does_not_create_eligible_evidence(rig, stopped):
    rig.supervisor.result_changes = {
        "outcome": "timeout",
        "exit_code": None,
        "processes_stopped": stopped,
    }
    with pytest.raises(CapabilityUnavailableError if stopped else IndeterminateExecutionError):
        evaluate(rig)
    active = rig.ledger.active(rig.context.workspace_id)
    assert (
        (len(active) == 1 and active[0]["state"] == "indeterminate")
        if not stopped
        else active == []
    )


def test_changed_interpreter_during_measurement_invalidates_the_whole_report(rig):
    rig.supervisor.during = lambda: rig.interpreter.write_bytes(b"changed executable")
    with pytest.raises(StateMismatchError, match="interpreter"):
        evaluate(rig)
    assert rig.ledger.active(rig.context.workspace_id) == []


def test_unregistered_agent_and_changed_runtime_pin_never_dispatch(rig):
    with pytest.raises(ScopeDeniedError):
        evaluate(rig, agent_id=uuid4())
    rig.interpreter.write_bytes(b"unexpected executable")
    with pytest.raises(StateMismatchError, match="interpreter"):
        evaluate(rig)
    assert rig.supervisor.calls == []
    assert rig.ledger.active(rig.context.workspace_id) == []


def test_source_metadata_cannot_become_code_or_probe_environment(rig):
    report = evaluate(
        rig,
        [
            {"kind": "binary", "name": "python3; touch /tmp/must-not-exist"},
            {"kind": "binary", "name": "/home/agent/arbitrary-script"},
        ],
    )
    assert all(row["status"] == "unsupported" for row in report["checks"][1:])
    assert json.loads(rig.supervisor.calls[0].stdin)["binaries"] == []


def test_a_check_uuid_cannot_be_replayed_after_a_completed_probe(rig):
    identity = uuid4()
    evaluate(rig, check_id=identity)
    with pytest.raises(StateMismatchError):
        evaluate(rig, check_id=identity)
    assert len(rig.supervisor.calls) == 1


def test_shared_tool_requires_exact_runtime_pin_and_current_release_hold(rig):
    executable = "/usr/local/tools/example/static/bin"
    report = evaluate(rig, [{"kind": "binary", "name": executable}])
    assert report["checks"][-1]["reasonCode"] == "shared_tool_not_admitted"
    runtime = replace(rig.runtime, tool_entrypoints=((executable, "f" * 64),))
    rig.evaluator.runtime = runtime
    rig.evaluator.backend.runtime = runtime
    rig.supervisor.available.add(executable)
    assert evaluate(rig, [{"kind": "binary", "name": executable}])["eligible"] is True
    hold = {
        "schema": "potbot-tool-release-hold/1",
        "slug": "example",
        "workspace_id": str(rig.context.workspace_id),
        "agent_id": str(rig.agent_id),
        "operation_id": str(uuid4()),
        "direction": "activate",
        "commit": "a" * 40,
        "definition_sha256": "1" * 64,
        "identity_sha256": "2" * 64,
        "image_digest": runtime.image_digest,
        "qualification_sha256": runtime.qualification_sha256,
        "test_evidence_sha256": "3" * 64,
        "suite_sha256": "4" * 64,
        "entrypoints": {executable: "f" * 64},
    }
    write_hold(rig.tools, "example", hold)
    report = evaluate(rig, [{"kind": "binary", "name": executable}])
    assert report["checks"][-1]["reasonCode"] == "shared_tool_held"
    assert json.loads(rig.supervisor.calls[-1].stdin)["binaries"] == []


def test_shared_tool_hash_mismatch_is_blocked_even_when_path_is_visible(rig):
    executable = "/usr/local/tools/example/static/bin"
    runtime = replace(rig.runtime, tool_entrypoints=((executable, "e" * 64),))
    rig.evaluator.runtime = runtime
    rig.evaluator.backend.runtime = runtime
    rig.supervisor.available.add(executable)
    report = evaluate(rig, [{"kind": "binary", "name": executable}])
    assert report["checks"][-1]["reasonCode"] == "shared_tool_pin_mismatch"
    assert report["eligible"] is False


@pytest.mark.parametrize(
    "mutation", ["identity", "secret", "hash", "clauses", "expiry", "eligible", "extra"]
)
def test_public_report_parser_rejects_malformed_or_secret_fields(rig, mutation):
    report = evaluate(rig)
    if mutation == "identity":
        report["agentId"] = "not-an-id"
    elif mutation == "secret":
        report["bindings"]["credential"] = "must-not-be-returned"
    elif mutation == "hash":
        report["bindings"]["runtimeSha256"] = "stale"
    elif mutation == "clauses":
        report["checks"][0]["status"] = []
    elif mutation == "expiry":
        report["expiresAt"] = (rig.now + timedelta(seconds=121)).isoformat()
    elif mutation == "eligible":
        report["eligible"] = False
    else:
        report["stdout"] = "untrusted bytes"
    with pytest.raises(InvalidInputError):
        parse_eligibility_report(report)


def test_requirement_digest_preserves_semantics_without_promoting_unverified_labels():
    plain = [{"kind": "any_binary", "name": "python3"}]
    legacy = [{"kind": "any_binary", "name": "python3", "status": "unverified"}]
    assert requirements_digest(plain) == requirements_digest(legacy)
    assert parse_requirements(plain)[0]["group"] == "any_binary"
    with pytest.raises(InvalidInputError):
        parse_requirements([{"kind": "binary", "name": "python3", "status": "passed"}])
    with pytest.raises(InvalidInputError):
        parse_requirements(
            [{"kind": "any_binary", "name": "python3", "group": "untrusted-alternative-group"}]
        )
    with pytest.raises(InvalidInputError):
        parse_requirements(plain * 129)
    with pytest.raises(InvalidInputError, match="64 KiB"):
        parse_requirements(
            [{"kind": "compatibility", "name": str(index) + "🧪" * 500} for index in range(128)]
        )


def test_configured_factory_requires_existing_scoped_operator_not_a_new_owner(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("POTBOT_OPERATOR_REQUIRED", raising=False)
    with pytest.raises(ScopeDeniedError, match="installed scoped"):
        configured_skill_eligibility(
            None,
            WorkspaceContext(uuid4(), uuid4()),
            manifest=tmp_path / "absent.json",
            workspace_name="fixture",
            probe_executable_sha256="a" * 64,
        )
