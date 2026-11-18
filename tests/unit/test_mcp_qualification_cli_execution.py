"""Native entry-point preflight, claim fencing and receipt-only recovery selection.

Explicit host/service fixtures test CLI sequencing. Actual gateway executions,
artifacts and private stopped receipts are covered by the PostgreSQL acceptance.
"""

import asyncio
import copy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api.mcp_qualification_cli import _execute
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)


@pytest.fixture
def native_command(monkeypatch):
    from potbot.controller import mcp_discovery
    from potbot.tools import mcp_discovery_host, mcp_qualification_host

    binding, agent, attempt, suite_id, worker = (uuid4() for _ in range(5))
    args = SimpleNamespace(
        command="run",
        identity=attempt,
        revision=1,
        digest="a" * 64,
        key="native-once",
        profile=Path("/operator/mcp.json"),
        suite=Path("/operator/cases.json"),
        manifest=Path("/operator/runtime.json"),
        workspace_name="fixture-workspace",
        artifacts_root=Path("/operator/artifacts"),
        receipts_root=Path("/operator/receipts"),
    )
    selected = {
        "status": "requested",
        "revision": 1,
        "bindingSha256": args.digest,
        "bindingId": str(binding),
        "suiteId": str(suite_id),
        "agentId": str(agent),
        "configurationSha256": "b" * 64,
        "suiteSha256": "d" * 64,
        "recoveryAvailable": False,
    }
    profile = SimpleNamespace(
        installed=SimpleNamespace(installation=SimpleNamespace(fingerprint="c" * 64)),
        agent_id=agent,
        configuration_sha256="b" * 64,
        private_paths=(args.profile,),
    )
    suite = SimpleNamespace(path=args.suite, sha256="d" * 64, file_sha256="e" * 64)
    reference = {"bindingId": str(binding), "snapshotId": str(uuid4())}
    config = {
        "configurationSha256": "b" * 64,
        "installationSha256": "c" * 64,
        "suiteSha256": "d" * 64,
        "suiteFileSha256": "e" * 64,
        "consumerSha256": "f" * 64,
        "taskBinding": {"episodeId": str(uuid4())},
    }
    admitted = dict(config)
    report = {"schema": "explicit-cli-host-fixture", "attemptId": str(attempt)}
    result = {"attempt": {"status": "succeeded", "id": str(attempt)}}
    rig = SimpleNamespace(
        args=args,
        selected=selected,
        profile=profile,
        suite=suite,
        config=config,
        admitted=admitted,
        reference=reference,
        report=report,
        result=result,
        calls=[],
        file_reads=[],
        validations=[],
        events={},
        worker=worker,
        binding=binding,
        source_hash="f" * 64,
        profile_readback=profile,
        suite_readback=suite,
        replay=None,
        claim_error=None,
        native_error=None,
        runtime_error=None,
        private_error=None,
        complete_error=None,
        fail_error=None,
        fenced=False,
        before_native=lambda options: None,
        after_native=lambda options: None,
        context=object(),
        database=object(),
    )

    def read_profile(context, path, *, binding_id):
        assert (context, path, binding_id) == (rig.context, args.profile, binding)
        rig.file_reads.append("profile")
        if isinstance(rig.profile_readback, BaseException):
            raise rig.profile_readback
        return rig.profile_readback

    def read_suite(path):
        assert path == args.suite
        rig.file_reads.append("suite")
        if isinstance(rig.suite_readback, BaseException):
            raise rig.suite_readback
        return rig.suite_readback

    def private(paths):
        assert paths == (*profile.private_paths, suite.path)
        rig.calls.append("private-paths")
        if rig.private_error:
            raise rig.private_error

    async def execute(kind, context, **options):
        assert context is rig.context
        rig.calls.append(kind)
        assert options["attempt_id"] == attempt
        assert options["profile"] is profile and options["suite"] is suite
        assert (
            options["reference"] == reference and options["task_binding"] == config["taskBinding"]
        )
        options["authorize_attempt"]()
        assert options["cancelled"]() is False
        rig.before_native(options)
        if rig.native_error:
            raise rig.native_error
        assert options["load_case"]("fixture-case") is None
        prepared = {"phase": "prepared", "caseId": "fixture-case"}
        options["record_case"](prepared)
        assert options["load_case"]("fixture-case") == prepared
        assert options["load_case"]("fixture-case") is not rig.events["fixture-case"]
        options["record_case"]({**prepared, "phase": "finished"})
        rig.after_native(options)
        return report

    async def run(context, **options):
        return await execute("run", context, **options)

    async def recover(context, **options):
        return await execute("recover", context, **options)

    @contextmanager
    def host(database, context, **options):
        assert (database, context) == (rig.database, rig.context)
        assert options == {
            "manifest": args.manifest,
            "workspace_name": args.workspace_name,
            "artifacts_root": args.artifacts_root,
            "receipts_root": args.receipts_root,
        }
        rig.calls.append("host")
        if rig.runtime_error:
            raise rig.runtime_error
        try:
            yield SimpleNamespace(
                native=SimpleNamespace(validate_private_paths=private), run=run, recover=recover
            )
        finally:
            rig.calls.append("closed")

    def claim(context, identity, revision, digest, key, **options):
        assert (context, identity, revision, digest, key) == (
            rig.context,
            attempt,
            1,
            args.digest,
            args.key,
        )
        assert options == {
            "setup_token": "private-token",
            "recovery": args.command == "recover",
            "fresh_only": True,
        }
        assert rig.calls[-1] == "private-paths"
        rig.calls.append("claim")
        if rig.claim_error:
            raise rig.claim_error
        return (
            {"replay": rig.replay}
            if rig.replay is not None
            else {"workerToken": str(worker), "reference": reference, "config": config}
        )

    def validate(context, identity, claimed_worker):
        assert (context, identity, claimed_worker) == (rig.context, attempt, worker)
        rig.validations.append(True)
        if rig.fenced:
            raise ScopeDeniedError("private worker authority changed canary")
        return {"config": config}

    def complete(context, identity, claimed_worker, evidence):
        assert (context, identity, claimed_worker) == (rig.context, attempt, worker)
        assert evidence is report
        rig.calls.append("complete")
        if rig.complete_error:
            raise rig.complete_error
        return result

    def fail(context, identity, claimed_worker, reason):
        assert (context, identity, claimed_worker) == (rig.context, attempt, worker)
        rig.calls.append(("failed", reason))
        if rig.fail_error:
            raise rig.fail_error

    def record(context, identity, claimed_worker, event):
        assert (context, identity, claimed_worker) == (rig.context, attempt, worker)
        rig.calls.append(event["phase"])
        rig.events[event["caseId"]] = copy.deepcopy(event)

    def case(context, identity, claimed_worker, case_id):
        assert (context, identity, claimed_worker) == (rig.context, attempt, worker)
        return rig.events.get(case_id)

    monkeypatch.setattr(mcp_discovery_host, "read_mcp_profile", read_profile)
    monkeypatch.setattr(mcp_qualification_host, "read_mcp_qualification_suite", read_suite)
    monkeypatch.setattr(
        mcp_qualification_host, "qualification_consumer_sha256", lambda: rig.source_hash
    )
    monkeypatch.setattr(mcp_qualification_host, "configured_mcp_qualification_host", host)
    monkeypatch.setattr(
        mcp_discovery,
        "installed_identity",
        lambda installed, context: {"installationSha256": installed.installation.fingerprint},
    )
    rig.service = SimpleNamespace(
        execution_replay=lambda *args, **kwargs: None,
        attempt=lambda *args: {"attempt": selected},
        suite=lambda *args: {"suite": admitted},
        claim=claim,
        validate_claim=validate,
        complete=complete,
        fail=fail,
        record_case=record,
        case=case,
    )
    return rig


def invoke(rig):
    return asyncio.run(_execute(rig.service, rig.database, rig.context, rig.args, "private-token"))


def test_native_preflight_precedes_claim_and_callbacks_keep_existing_durable_owner(native_command):
    r = native_command
    assert invoke(r) == r.result
    assert r.calls == [
        "host",
        "private-paths",
        "claim",
        "run",
        "prepared",
        "finished",
        "complete",
        "closed",
    ]
    assert len(r.validations) >= 3
    assert r.events == {"fixture-case": {"phase": "finished", "caseId": "fixture-case"}}


def test_recovery_selects_only_receipt_reconciliation_host_method(native_command):
    r = native_command
    r.args.command = "recover"
    r.selected.update(status="indeterminate", recoveryAvailable=True)
    assert invoke(r) == r.result
    assert "recover" in r.calls and "run" not in r.calls


@pytest.mark.parametrize("command", ["run", "recover"])
def test_claim_race_returns_exact_receipt_and_closes_host_without_dispatch(native_command, command):
    r = native_command
    r.args.command = command
    r.selected["recoveryAvailable"] = True
    r.replay = {"attempt": {"status": "running"}}
    assert invoke(r) == r.replay
    assert r.calls == ["host", "private-paths", "claim", "closed"]
    assert not r.validations and not r.events


@pytest.mark.parametrize("change", ["revision", "digest", "status", "recovery-unavailable"])
def test_changed_review_never_opens_private_files_or_configures_host(native_command, change):
    r = native_command
    if change == "revision":
        r.selected["revision"] += 1
    elif change == "digest":
        r.selected["bindingSha256"] = "0" * 64
    elif change == "status":
        r.selected["status"] = "running"
    else:
        r.args.command = "recover"
    with pytest.raises(RevisionConflictError):
        invoke(r)
    assert not r.calls and not r.file_reads


@pytest.mark.parametrize(
    "change", ["agent", "configuration", "installation", "suite", "suite-bytes", "consumer"]
)
def test_changed_native_files_or_consumer_cannot_claim_a_reviewed_attempt(native_command, change):
    r = native_command
    if change == "agent":
        r.profile.agent_id = uuid4()
    elif change == "configuration":
        r.profile.configuration_sha256 = "0" * 64
    elif change == "installation":
        r.profile.installed.installation.fingerprint = "0" * 64
    elif change == "suite":
        r.suite.sha256 = "0" * 64
    elif change == "suite-bytes":
        r.suite.file_sha256 = "0" * 64
    else:
        r.source_hash = "0" * 64
    with pytest.raises(ScopeDeniedError):
        invoke(r)
    assert not r.calls and not r.validations


@pytest.mark.parametrize("failure", ["profile", "suite", "runtime", "private-path"])
def test_native_preflight_failures_leave_pending_attempt_unclaimed(native_command, failure):
    r = native_command
    error = InvalidInputError("private native file/runtime canary")
    if failure == "profile":
        r.profile_readback = error
    elif failure == "suite":
        r.suite_readback = error
    elif failure == "runtime":
        r.runtime_error = error
    else:
        r.private_error = error
    with pytest.raises(InvalidInputError):
        invoke(r)
    assert "claim" not in r.calls and "run" not in r.calls and "complete" not in r.calls
    assert not any(isinstance(call, tuple) and call[0] == "failed" for call in r.calls)
    if failure == "private-path":
        assert r.calls[-1] == "closed"


def test_changed_claim_race_is_not_overwritten_by_a_failure_command(native_command):
    r = native_command
    r.claim_error = RevisionConflictError("another native consumer already owns the attempt")
    with pytest.raises(RevisionConflictError):
        invoke(r)
    assert r.calls == ["host", "private-paths", "claim", "closed"]


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("change", ["profile", "suite-bytes", "consumer", "claim", "installation"])
def test_before_and_after_native_callbacks_fence_changed_authority(native_command, when, change):
    r = native_command

    def change_owner():
        if change == "profile":
            r.profile_readback = SimpleNamespace(
                **{**vars(r.profile), "configuration_sha256": "0" * 64}
            )
        elif change == "suite-bytes":
            r.suite_readback = SimpleNamespace(**{**vars(r.suite), "file_sha256": "0" * 64})
        elif change == "consumer":
            r.source_hash = "0" * 64
        elif change == "claim":
            r.fenced = True
        else:
            r.config["installationSha256"] = "0" * 64

    if when == "before":
        claim = r.service.claim

        def changed_claim(*args, **options):
            value = claim(*args, **options)
            change_owner()
            return value

        r.service.claim = changed_claim
    else:
        r.after_native = lambda options: change_owner()
    with pytest.raises(ScopeDeniedError):
        invoke(r)
    assert "complete" not in r.calls
    assert ("failed", "authority_changed") in r.calls and r.calls[-1] == "closed"
    if when == "before":
        assert "run" not in r.calls and not r.events
    else:
        assert "finished" in r.calls


def test_native_cancellation_predicate_checks_live_claim_authority(native_command):
    r = native_command

    def cancel(options):
        r.fenced = True
        assert options["cancelled"]() is True
        raise OperationCancelledError("private cancellation reason canary")

    r.before_native = cancel
    with pytest.raises(OperationCancelledError):
        invoke(r)
    assert ("failed", "qualification_cancelled") in r.calls
    assert "prepared" not in r.calls and "complete" not in r.calls


@pytest.mark.parametrize(
    "error,reason",
    [
        (
            IndeterminateExecutionError("private stopped-state canary"),
            "qualification_indeterminate",
        ),
        (asyncio.CancelledError("private cancelled task canary"), "qualification_indeterminate"),
        (OperationCancelledError("private cancellation canary"), "qualification_cancelled"),
        (ScopeDeniedError("private host authorization canary"), "authority_changed"),
        (StateMismatchError("private changed owner canary"), "authority_changed"),
        (RuntimeError("private native error body canary"), "qualification_failed"),
    ],
)
def test_native_failure_records_only_safe_reason_and_never_publishes(native_command, error, reason):
    r = native_command
    r.native_error = error
    with pytest.raises(type(error)):
        invoke(r)
    assert ("failed", reason) in r.calls
    assert "complete" not in r.calls and r.calls[-1] == "closed"
    assert not any("canary" in str(call) for call in r.calls)


def test_failure_recording_cannot_replace_a_newer_operator_fence(native_command):
    r = native_command
    r.native_error = IndeterminateExecutionError("private unknown native state")
    r.fail_error = ScopeDeniedError("newer claim already fenced this worker")
    with pytest.raises(IndeterminateExecutionError):
        invoke(r)
    assert r.calls[-1] == "closed" and "complete" not in r.calls


def test_lost_report_response_is_replayed_before_any_second_host_entry(native_command):
    r = native_command
    r.complete_error = IndeterminateExecutionError("explicit lost database commit response")
    with pytest.raises(IndeterminateExecutionError):
        invoke(r)
    assert r.calls[-2:] == [("failed", "qualification_indeterminate"), "closed"]
    before = list(r.calls)
    r.service.execution_replay = lambda *args, **kwargs: r.result
    assert invoke(r) == r.result
    assert r.calls == before
