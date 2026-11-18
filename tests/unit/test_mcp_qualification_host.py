"""Private qualification profiles and native orchestration with explicit fixtures.

The existing native-owner fixture supplies actual Agent/SQLite ownership. Marked
gateway/database results exercise controller sequencing, not deployed authority;
the real PostgreSQL gateway acceptance is in test_mcp_qualification.py.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import (
    DomainError,
    IndeterminateExecutionError,
    InvalidInputError,
    OperationCancelledError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools import mcp_qualification_host as module
from potbot.tools.mcp_discovery_host import read_mcp_profile
from potbot.tools.repository import Lease
from potbot.tools.workspace_supervision import WorkspaceExecutorIdentity, WorkspaceLeaseGuard
from tests.unit import test_mcp_discovery_host as native_fixtures

runtime_rig = native_fixtures.runtime_rig
operator_directory = native_fixtures.operator_directory
profile = native_fixtures.profile
native = native_fixtures.native


def suite_document():
    return {
        "schema": module.SUITE_SCHEMA,
        "name": "Known document lookup",
        "description": "Explicit fixture expectation; not vendor qualification",
        "cases": [
            {
                "id": "known-document",
                "title": "Find an independently selected document",
                "toolName": "search",
                "arguments": {"query": "fixture-private-query"},
                "oracle": {
                    "schema": "potbot-mcp-result-oracle/v1",
                    "assertions": [
                        {"op": "equals", "pointer": "/structuredContent/matches", "value": 1}
                    ],
                },
            }
        ],
    }


def write_suite(path, value=None):
    path.write_text(canonical_json(suite_document() if value is None else value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_private_suite_binds_exact_bytes_and_detaches_case_arguments(operator_directory):
    path = write_suite(operator_directory / "suite.json")
    suite = module.read_mcp_qualification_suite(path)
    first = suite.cases[0].arguments
    first["query"] = "mutated-client-view"
    assert suite.cases[0].arguments == {"query": "fixture-private-query"}
    summary = suite.summary()
    assert summary["cases"][0]["assertionCount"] == 1
    assert summary["cases"][0]["caseSha256"] == content_hash(suite.cases[0].to_dict())
    assert "fixture-private-query" not in canonical_json(summary)
    path.write_text(json.dumps(suite_document(), indent=2), encoding="utf-8")
    reformatted = module.read_mcp_qualification_suite(path)
    assert reformatted.sha256 == suite.sha256
    assert reformatted.file_sha256 != suite.file_sha256


@pytest.mark.parametrize(
    "fault",
    [
        "schema",
        "extra",
        "empty",
        "too-many",
        "duplicate",
        "id-slash",
        "id-control",
        "id-long",
        "id-uppercase",
        "id-dot",
        "id-number",
        "title-long",
        "title-control",
        "tool-long",
        "name-long",
        "description-long",
        "arguments-array",
        "arguments-large",
        "arguments-depth",
        "case-extra",
        "oracle-empty",
    ],
)
def test_suite_rejects_ambiguous_unbounded_and_executable_oracles(operator_directory, fault):
    value = suite_document()
    case = value["cases"][0]
    if fault == "schema":
        value["schema"] = "future"
    elif fault == "extra":
        value["command"] = "execute this"
    elif fault == "empty":
        value["cases"] = []
    elif fault == "too-many":
        value["cases"] = [dict(case, id=f"case-{index}") for index in range(9)]
    elif fault == "duplicate":
        value["cases"].append(copy.deepcopy(case))
    elif fault == "id-slash":
        case["id"] = "../escape"
    elif fault == "id-control":
        case["id"] = "case\n"
    elif fault == "id-long":
        case["id"] = "x" * 65
    elif fault == "id-uppercase":
        case["id"] = "Search"
    elif fault == "id-dot":
        case["id"] = "search.document"
    elif fault == "id-number":
        case["id"] = "1-search"
    elif fault == "title-long":
        case["title"] = "x" * 129
    elif fault == "title-control":
        case["title"] = "title\0"
    elif fault == "tool-long":
        case["toolName"] = "x" * 129
    elif fault == "name-long":
        value["name"] = "x" * 97
    elif fault == "description-long":
        value["description"] = "x" * 2049
    elif fault == "arguments-array":
        case["arguments"] = []
    elif fault == "arguments-large":
        case["arguments"] = {"query": "x" * module.MAX_ARGUMENT_BYTES}
    elif fault == "arguments-depth":
        nested = {}
        for _ in range(35):
            nested = {"x": nested}
        case["arguments"] = nested
    elif fault == "case-extra":
        case["executeOracle"] = "program"
    else:
        case["oracle"]["assertions"] = []
    with pytest.raises(DomainError):
        module.read_mcp_qualification_suite(write_suite(operator_directory / "suite.json", value))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"one","schema":"two"}',
        b'{"cases":NaN}',
        b"\xff",
        b"[]",
        b" " * (module.MAX_SUITE_BYTES + 1),
    ],
)
def test_suite_rejects_duplicate_nonfinite_invalid_or_oversized_bytes(operator_directory, raw):
    path = operator_directory / "suite.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(DomainError):
        module.read_mcp_qualification_suite(path)


@pytest.mark.parametrize("fault", ["symlink", "ancestor", "public", "hardlink", "relative"])
def test_suite_private_file_boundaries(operator_directory, fault):
    path = write_suite(operator_directory / "suite.json")
    if fault == "symlink":
        link = operator_directory / "redirect.json"
        link.symlink_to(path)
        path = link
    elif fault == "ancestor":
        directory = operator_directory / "unsafe"
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        path = write_suite(directory / "suite.json")
    elif fault == "public":
        path.chmod(0o644)
    elif fault == "hardlink":
        os.link(path, operator_directory / "copy.json")
    else:
        path = Path("suite.json")
    with pytest.raises(DomainError):
        module.read_mcp_qualification_suite(path)


class FixtureConnection:
    def __init__(self, contract):
        self.contract = contract
        self.queries = []

    def execute(self, query, params):
        self.queries.append((query, params))
        assert "FOR UPDATE" not in query
        if "FROM contract_revisions" in query:
            return SimpleNamespace(fetchone=lambda: {"content": self.contract})
        raise AssertionError("Unexpected fixture query")


class FixtureDatabase:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self, context):
        yield self.connection


@pytest.fixture
def host_rig(native, operator_directory, monkeypatch):
    profile_path = native.profile.path
    binding = uuid4()
    loaded = read_mcp_profile(native.rig.context, profile_path, binding_id=binding)
    suite = module.read_mcp_qualification_suite(write_suite(operator_directory / "suite.json"))
    contract = {"capabilities": [loaded.installed.definition.capability]}
    task = {
        "episodeId": str(uuid4()),
        "contractRevisionId": str(uuid4()),
        "contractSha256": content_hash(contract),
        "repositoryStateId": str(uuid4()),
        "repositorySha256": "4" * 64,
        "semanticReleaseId": str(uuid4()),
        "semanticReleaseSha256": "5" * 64,
    }
    database = FixtureDatabase(FixtureConnection(contract))
    artifacts, receipts = operator_directory / "artifacts", operator_directory / "receipts"
    artifacts.mkdir(mode=0o700)
    receipts.mkdir(mode=0o700)
    host = module.McpQualificationHost(
        database,
        native.host,
        artifacts_root=artifacts,
        receipts_root=receipts,
        clock=lambda: native.rig.now,
    )
    monkeypatch.setattr(module, "qualification_consumer_sha256", lambda: "6" * 64)
    monkeypatch.setattr(module, "read_mcp_task_binding", lambda *args, **kwargs: dict(task))
    attempt = uuid4()
    reference = {
        "bindingId": str(binding),
        "installationSha256": loaded.installed.installation.fingerprint,
    }
    registered_agent = native.host.homes.lookup(native.rig.context.workspace_id, loaded.agent_id)
    native_pins = {
        "runtimeSha256": native.host._runtime(
            native.rig.context, registered_agent, loaded.installed, verify_bytes=True
        ),
        "imageSha256": native.host.runtime.image_digest.removeprefix("sha256:"),
        "qualificationSha256": native.host.runtime.qualification_sha256,
        "agentHomeRevision": registered_agent.revision,
        "consumerSha256": "6" * 64,
        "configurationSha256": loaded.configuration_sha256,
        "referenceSha256": content_hash(reference),
    }
    events, log = {}, []
    prepared = {
        "schema": module.CASE_SCHEMA,
        "phase": "prepared",
        "caseId": suite.cases[0].id,
        "caseSha256": suite.cases[0].sha256,
        "candidateId": str(uuid4()),
        "leaseOwner": f"mcp-qualification:{attempt}:{suite.cases[0].id}",
        "leaseGeneration": 1,
        "actionId": str(uuid4()),
        "reservationId": str(uuid4()),
        "inputArtifactId": str(uuid4()),
        "inputSha256": "7" * 64,
        "storageSha256": host._storage,
        "nativePins": native_pins,
    }
    check = {
        "schema": "potbot-mcp-result-check/v1",
        "oracleSha256": content_hash(suite.cases[0].oracle),
        "outputSha256": "8" * 64,
        "passed": True,
        "checks": [{"index": 0, "passed": True, "reasonCode": None}],
    }
    finished = {
        **prepared,
        "phase": "finished",
        "runId": str(uuid4()),
        "resultArtifactId": str(uuid4()),
        "resultSha256": "8" * 64,
        "receiptSha256": "9" * 64,
        "invocationSha256": "a" * 64,
        "resultCheck": check,
        "processesStopped": True,
        "isolationSha256": "b" * 64,
        "elapsedMs": 10,
        "reasonCode": None,
    }
    lease = Lease(uuid4(), prepared["leaseOwner"], 1, native.rig.now + timedelta(seconds=660))

    class FixtureGateway:
        def __init__(self, authorize):
            self.authorize = authorize
            self.dispatched = False
            self.unknown = False
            self.before = lambda: None
            self.candidates = SimpleNamespace(release=lambda *args: log.append("released"))
            self.store = SimpleNamespace(
                inspect=lambda *args: {
                    "run": {"id": finished["runId"]} if self.dispatched else None
                }
            )

        async def execute_mcp(self, *args):
            assert events.get(prepared["caseId"]) == prepared
            self.before()
            self.authorize()
            self.dispatched = True
            log.append("dispatched")

        def reconcile(self, *args):
            log.append("reconciled")
            return (
                {"status": "indeterminate"} if self.unknown else {"completion": {"fixture": True}}
            )

    gateway = FixtureGateway(lambda: None)

    def factory(*args, **kwargs):
        gateway.authorize = args[-1]
        return gateway

    monkeypatch.setattr(host, "_gateway", factory)
    monkeypatch.setattr(host, "_prepared", lambda *args: (dict(prepared), lease))
    monkeypatch.setattr(host, "_case_result", lambda *args: dict(finished))
    monkeypatch.setattr(host, "_release_unstarted", lambda *args: log.append("not-dispatched"))

    def record(event):
        log.append(event["phase"])
        events[event["caseId"]] = copy.deepcopy(event)

    options = {
        "attempt_id": attempt,
        "profile": loaded,
        "suite": suite,
        "reference": reference,
        "task_binding": task,
        "authorize_attempt": lambda: None,
        "record_case": record,
        "load_case": events.get,
    }
    return SimpleNamespace(**locals())


def test_native_orchestration_records_before_dispatch_and_pins_retained_case_evidence(host_rig):
    r = host_rig
    report = asyncio.run(r.host.run(r.native.rig.context, **r.options))
    assert r.log == ["prepared", "dispatched", "finished", "released"]
    assert report["schema"] == module.REPORT_SCHEMA
    assert report["suiteFileSha256"] == r.suite.file_sha256
    assert report["casesSha256"] == content_hash([r.finished])
    assert report["taskBinding"] == r.task
    assert "passed" not in report and "qualified" not in report
    assert "fixture-private-query" not in canonical_json(report)


def test_prepared_case_prevents_a_second_fresh_native_run(host_rig):
    r = host_rig
    r.events[r.prepared["caseId"]] = r.prepared
    with pytest.raises(IndeterminateExecutionError):
        asyncio.run(r.host.run(r.native.rig.context, **r.options))
    assert "dispatched" not in r.log


def test_lost_prepared_callback_response_does_not_dispatch(host_rig):
    r = host_rig

    def record(event):
        r.events[event["caseId"]] = event
        raise RuntimeError("explicit lost response fixture")

    with pytest.raises(RuntimeError):
        asyncio.run(r.host.run(r.native.rig.context, **dict(r.options, record_case=record)))
    assert r.events[r.prepared["caseId"]] == r.prepared
    assert "dispatched" not in r.log


def test_preparation_must_be_read_back_exactly_before_dispatch(host_rig):
    r = host_rig
    with pytest.raises(StateMismatchError):
        asyncio.run(
            r.host.run(r.native.rig.context, **dict(r.options, record_case=lambda event: None))
        )
    assert "dispatched" not in r.log


@pytest.mark.parametrize(
    "fault", ["suite", "profile", "source", "task", "storage", "authority", "cancel"]
)
def test_changed_authority_at_gateway_boundary_fences_dispatch(host_rig, monkeypatch, fault):
    r = host_rig
    cancelled = [False]

    def change():
        if fault == "suite":
            r.suite.path.write_text(json.dumps(suite_document(), indent=2), encoding="utf-8")
        elif fault == "profile":
            raw = json.loads(r.profile_path.read_text())
            r.profile_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        elif fault == "source":
            monkeypatch.setattr(module, "qualification_consumer_sha256", lambda: "c" * 64)
        elif fault == "task":
            monkeypatch.setattr(module, "read_mcp_task_binding", lambda *args, **kwargs: {})
        elif fault == "storage":
            r.artifacts.chmod(0o755)
        else:
            cancelled[0] = True

    def authority():
        if fault == "authority" and cancelled[0]:
            raise ScopeDeniedError("explicit revoked claim fixture")

    r.gateway.before = change
    options = dict(
        r.options, authorize_attempt=authority, cancelled=lambda: fault == "cancel" and cancelled[0]
    )
    with pytest.raises(DomainError):
        asyncio.run(r.host.run(r.native.rig.context, **options))
    assert "dispatched" not in r.log and "finished" not in r.log


def test_recovery_reconciles_only_recorded_action_and_never_dispatches(host_rig):
    r = host_rig
    r.events[r.prepared["caseId"]] = r.prepared
    r.gateway.dispatched = True
    report = asyncio.run(r.host.recover(r.native.rig.context, **r.options))
    assert r.log == ["reconciled", "finished"]
    assert report["casesSha256"] == content_hash([r.finished])


def test_recovery_with_no_prepared_cases_records_no_fabricated_case(host_rig):
    r = host_rig
    report = asyncio.run(r.host.recover(r.native.rig.context, **r.options))
    assert not r.log and report["casesSha256"] == content_hash([])


def test_recovery_proves_undispatched_case_without_running_it(host_rig):
    r = host_rig
    r.events[r.prepared["caseId"]] = r.prepared
    report = asyncio.run(r.host.recover(r.native.rig.context, **r.options))
    assert r.log == ["not-dispatched"]
    assert report["casesSha256"] == content_hash([r.prepared])


def test_unknown_recovery_never_invents_stopped_or_finished_evidence(host_rig):
    r = host_rig
    r.events[r.prepared["caseId"]] = r.prepared
    r.gateway.dispatched = r.gateway.unknown = True
    with pytest.raises(IndeterminateExecutionError):
        asyncio.run(r.host.recover(r.native.rig.context, **r.options))
    assert r.log == ["reconciled"]
    assert r.events[r.prepared["caseId"]]["phase"] == "prepared"


@pytest.mark.parametrize("field", ["storageSha256", "caseSha256", "leaseOwner", "schema", "extra"])
def test_recovery_rejects_substituted_case_journal_before_reconciliation(host_rig, field):
    r = host_rig
    r.events[r.prepared["caseId"]] = {**r.prepared, field: "substituted"}
    with pytest.raises(DomainError):
        asyncio.run(r.host.recover(r.native.rig.context, **r.options))
    assert not r.log


def test_storage_roots_must_remain_outside_worker_mounts_and_separate(host_rig):
    r = host_rig
    unsafe = r.native.rig.runtime.home / "private-looking"
    unsafe.mkdir(mode=0o700)
    with pytest.raises(ScopeDeniedError):
        module.McpQualificationHost(
            r.database, r.native.host, artifacts_root=unsafe, receipts_root=r.receipts
        )
    with pytest.raises(ScopeDeniedError):
        module.McpQualificationHost(
            r.database, r.native.host, artifacts_root=r.artifacts, receipts_root=r.artifacts
        )


def test_cancellation_before_preparation_never_starts_a_case(host_rig):
    r = host_rig
    with pytest.raises(OperationCancelledError):
        asyncio.run(r.host.run(r.native.rig.context, **dict(r.options, cancelled=lambda: True)))
    assert not r.log


def test_invalid_task_shape_and_clock_cannot_publish_a_report(host_rig):
    r = host_rig
    with pytest.raises(InvalidInputError):
        asyncio.run(
            r.host.run(r.native.rig.context, **dict(r.options, task_binding={**r.task, "extra": 1}))
        )
    r.host.clock = lambda: r.native.rig.now.replace(tzinfo=None)
    with pytest.raises(InvalidInputError):
        asyncio.run(r.host.run(r.native.rig.context, **r.options))
    assert not r.log


@pytest.mark.parametrize("fault", ["resultSha256", "receiptSha256", "resultCheck", "no-run"])
def test_recovery_never_rewrites_a_finished_case_to_hide_changed_evidence(host_rig, fault):
    r = host_rig
    saved = dict(r.finished)
    if fault != "no-run":
        saved[fault] = None if fault == "resultCheck" else "0" * 64
        r.gateway.dispatched = True
    r.events[r.prepared["caseId"]] = saved
    with pytest.raises(StateMismatchError):
        asyncio.run(r.host.recover(r.native.rig.context, **r.options))
    assert "finished" not in r.log and "not-dispatched" not in r.log


@pytest.mark.parametrize(
    "field",
    [
        "runtimeSha256",
        "imageSha256",
        "qualificationSha256",
        "agentHomeRevision",
        "consumerSha256",
        "configurationSha256",
        "referenceSha256",
    ],
)
def test_recovery_cannot_relabel_original_execution_under_a_different_native_owner(host_rig, field):
    r = host_rig
    saved = copy.deepcopy(r.prepared)
    saved["nativePins"][field] = 9 if field == "agentHomeRevision" else "0" * 64
    r.events[r.prepared["caseId"]] = saved
    r.gateway.dispatched = True
    with pytest.raises(StateMismatchError):
        asyncio.run(r.host.recover(r.native.rig.context, **r.options))
    assert "reconciled" not in r.log and "finished" not in r.log


def test_preparation_uses_existing_workspace_guard_and_releases_it_before_dispatch(
    host_rig, monkeypatch
):
    r = host_rig
    actor = r.native.rig.context
    observed = []
    factory, prepare = r.host._gateway, r.host._prepared

    def assert_guard():
        active = r.native.rig.ledger.active(actor.workspace_id)
        assert len(active) == 1
        assert active[0]["operation"] == "mcp.qualification.prepare"

    def gateway(*args, **kwargs):
        assert_guard()
        observed.append("candidate-owner")
        return factory(*args, **kwargs)

    def prepared(*args, **kwargs):
        assert_guard()
        observed.append("materialization")
        return prepare(*args, **kwargs)

    def before_dispatch():
        assert r.native.rig.ledger.active(actor.workspace_id) == []
        observed.append("dispatch")

    monkeypatch.setattr(r.host, "_gateway", gateway)
    monkeypatch.setattr(r.host, "_prepared", prepared)
    r.gateway.before = before_dispatch
    asyncio.run(r.host.run(actor, **r.options))
    assert observed == ["candidate-owner", "materialization", "dispatch"]
    assert r.native.rig.ledger.active(actor.workspace_id) == []


def test_active_shared_home_worker_prevents_qualification_materialization(host_rig):
    r = host_rig
    actor = r.native.rig.context
    guard = WorkspaceLeaseGuard.exclusive_home(
        r.native.rig.ledger,
        WorkspaceExecutorIdentity(
            actor.workspace_id, r.loaded.agent_id, r.native.rig.runtime.image_digest
        ),
        operation="fixture.concurrent-worker",
        idempotency_key="fixture-shared-home-worker",
    )
    guard.start()
    try:
        with pytest.raises(DomainError):
            asyncio.run(r.host.run(actor, **r.options))
        assert not r.log
    finally:
        guard.finish(evidence_sha256="0" * 64)
        guard.abandon()


def test_recovery_never_recreates_a_missing_candidate_ownership_marker(host_rig):
    r = host_rig
    context = r.native.rig.context
    agent = r.native.host.homes.lookup(context.workspace_id, r.loaded.agent_id)
    gateway = module.McpQualificationHost._gateway(r.host, context, r.loaded, agent, lambda: None)
    marker = gateway.candidates.root / ".potbot-candidate-root.json"
    marker.unlink()
    with pytest.raises(ScopeDeniedError):
        module.McpQualificationHost._gateway(
            r.host, context, r.loaded, agent, lambda: None, recover=True
        )
    assert not marker.exists()


@pytest.mark.parametrize("fault", ["symlink", "ancestor-link", "replacement", "public"])
def test_storage_identity_fences_redirection_and_inode_substitution(host_rig, fault):
    r = host_rig
    if fault == "public":
        r.artifacts.chmod(0o755)
    elif fault == "ancestor-link":
        link = r.operator_directory / "redirect"
        link.symlink_to(r.artifacts.parent, target_is_directory=True)
        r.host.artifacts_root = link / r.artifacts.name
    else:
        moved = r.artifacts.with_name("old-artifacts")
        r.artifacts.rename(moved)
        if fault == "symlink":
            r.artifacts.symlink_to(moved, target_is_directory=True)
        else:
            r.artifacts.mkdir(mode=0o700)
    with pytest.raises(DomainError):
        asyncio.run(r.host.run(r.native.rig.context, **r.options))
    assert not r.log


@pytest.mark.parametrize("returned_authority", [False, True, "permit"])
def test_actual_gateway_rejects_boolean_or_text_authority_before_admission(
    host_rig, monkeypatch, returned_authority
):
    r = host_rig
    context = r.native.rig.context
    agent = r.native.host.homes.lookup(context.workspace_id, r.loaded.agent_id)
    authorize = Mock(return_value=returned_authority)
    gateway = module.McpQualificationHost._gateway(r.host, context, r.loaded, agent, authorize)
    gateway.store = SimpleNamespace(
        inspect=Mock(
            return_value={
                "action": {
                    "kind": r.loaded.installed.definition.name,
                    "stdin_artifact_id": r.prepared["inputArtifactId"],
                },
                "run": None,
            }
        )
    )
    raw = canonical_json(
        {
            "installation_hash": r.loaded.installed.installation.fingerprint,
            "arguments": r.suite.cases[0].arguments,
            "mcp_reference": r.reference,
        }
    ).encode()
    monkeypatch.setattr(gateway.artifacts, "read", lambda *args: raw)
    admitted = Mock(side_effect=AssertionError("Authority must be checked before admission"))
    monkeypatch.setattr(gateway, "_admit", admitted)
    with pytest.raises(ScopeDeniedError, match="authority must raise"):
        asyncio.run(
            gateway.execute_mcp(
                context, UUID(r.prepared["actionId"]), r.lease, UUID(r.prepared["reservationId"])
            )
        )
    authorize.assert_called_once_with()
    admitted.assert_not_called()


@pytest.mark.parametrize(
    "fault", [None, "paused", "stale-contract", "contract-integrity", "foreign-repository", "pin"]
)
def test_task_binding_uses_current_existing_owners_without_row_locks(native, monkeypatch, fault):
    context = native.rig.context
    episode_id, repository_id, state_id, contract_id, release_id = (uuid4() for _ in range(5))
    content = {"capabilities": ["test"]}
    episode = {
        "status": "running",
        "repository_id": repository_id,
        "active_contract_id": contract_id,
    }
    contract = {"id": contract_id, "content": content, "content_hash": content_hash(content)}
    state = {"id": state_id, "repository_id": repository_id, "content_digest": "1" * 64}
    if fault == "paused":
        episode["status"] = "paused"
    elif fault == "stale-contract":
        episode["active_contract_id"] = uuid4()
    elif fault == "contract-integrity":
        contract["content_hash"] = "2" * 64
    elif fault == "foreign-repository":
        state["repository_id"] = uuid4()

    class BindingConnection:
        def execute(self, query, parameters):
            assert "FOR UPDATE" not in query
            assert parameters[0] == context.workspace_id
            if "FROM episodes" in query:
                row = episode
            elif "FROM contract_revisions" in query:
                row = contract
            elif "FROM repository_states" in query:
                row = state
            else:
                raise AssertionError("Task binding must use existing task owners")
            return SimpleNamespace(fetchone=lambda: row)

    connection = BindingConnection()

    def pin(conn, actor, selected_episode, *, required):
        assert (conn, actor, selected_episode, required) == (connection, context, episode_id, True)
        if fault == "pin":
            raise StateMismatchError("Explicit corrupt semantic pin fixture")
        return {"release_id": release_id, "content_hash": "3" * 64}

    monkeypatch.setattr(module, "read_task_pin", pin)
    database = FixtureDatabase(connection)

    def observe():
        return module.read_mcp_task_binding(
            database,
            context,
            episode_id=episode_id,
            repository_state_id=state_id,
            contract_revision_id=contract_id,
        )

    if fault is not None:
        with pytest.raises(DomainError):
            observe()
    else:
        assert observe() == {
            "episodeId": str(episode_id),
            "contractRevisionId": str(contract_id),
            "contractSha256": content_hash(content),
            "repositoryStateId": str(state_id),
            "repositorySha256": "1" * 64,
            "semanticReleaseId": str(release_id),
            "semanticReleaseSha256": "3" * 64,
        }
