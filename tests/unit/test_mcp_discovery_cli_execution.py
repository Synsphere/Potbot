"""Native command lifetime, preflight, lost-response and failure recovery."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api.mcp_discovery_cli import _reconcile


def pending_reconciliation(monkeypatch, *, host_error=None, replay=None):
    from potbot.controller import mcp_discovery
    from potbot.tools import mcp_discovery_host

    binding, agent, attempt, worker = uuid4(), uuid4(), uuid4(), uuid4()
    args = SimpleNamespace(
        identity=attempt,
        revision=1,
        digest="a" * 64,
        key="native-once",
        profile=Path("/operator/mcp.json"),
        manifest=Path("/operator/runtime.json"),
        workspace_name="fixture",
    )
    selected = {
        "status": "requested",
        "revision": 1,
        "bindingSha256": args.digest,
        "bindingId": str(binding),
        "agentId": str(agent),
        "configurationSha256": "b" * 64,
        "installationSha256": "c" * 64,
    }
    calls = []
    profile = SimpleNamespace(
        installed=SimpleNamespace(installation=SimpleNamespace(fingerprint="c" * 64)),
        agent_id=agent,
        configuration_sha256="b" * 64,
        private_paths=(args.profile,),
    )
    config = {
        "configurationSha256": "b" * 64,
        "installationSha256": "c" * 64,
        "watchMs": 0,
    }

    def private(paths):
        assert paths == profile.private_paths
        calls.append("private-paths")

    async def observe(context, **kwargs):
        calls.append("observe")
        kwargs["authorize_attempt"]()
        assert kwargs["attempt_id"] == attempt and kwargs["agent_id"] == agent
        if host_error:
            raise host_error
        return "catalogue", "observation"

    @contextmanager
    def host(*values, **options):
        calls.append("host")
        try:
            yield SimpleNamespace(validate_private_paths=private, observe=observe)
        finally:
            calls.append("closed")

    def claim(*values, **options):
        calls.append("claim")
        assert "private-paths" in calls and options["fresh_only"] is True
        return (
            {"replay": replay}
            if replay is not None
            else {"workerToken": str(worker), "config": config}
        )

    def complete(context, identity, claimed_worker, catalogue, observation):
        assert (identity, claimed_worker, catalogue, observation) == (
            attempt,
            worker,
            "catalogue",
            "observation",
        )
        calls.append("complete")
        return {"attempt": {"status": "succeeded"}}

    def fail(context, identity, claimed_worker, reason):
        assert (identity, claimed_worker) == (attempt, worker)
        calls.append(("failed", reason))

    monkeypatch.setattr(mcp_discovery_host, "configured_mcp_discovery_host", host)
    monkeypatch.setattr(mcp_discovery_host, "read_mcp_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(
        mcp_discovery, "installed_identity", lambda *args: {"installationSha256": "c" * 64}
    )
    service = SimpleNamespace(
        execution_replay=lambda *args, **kwargs: None,
        attempt=lambda *args: {"attempt": selected},
        claim=claim,
        validate_claim=lambda *args: {"config": config},
        complete=complete,
        fail=fail,
    )
    return service, args, selected, profile, calls


def test_native_checks_private_paths_before_claim_and_publishes_only_host_result(monkeypatch):
    service, args, _, _, calls = pending_reconciliation(monkeypatch)
    assert asyncio.run(_reconcile(service, object(), object(), args, "token")) == {
        "attempt": {"status": "succeeded"}
    }
    assert calls == ["host", "private-paths", "claim", "observe", "complete", "closed"]


def test_native_claim_race_returns_receipt_without_observing(monkeypatch):
    receipt = {"attempt": {"status": "running"}}
    service, args, _, _, calls = pending_reconciliation(monkeypatch, replay=receipt)
    assert asyncio.run(_reconcile(service, object(), object(), args, "token")) == receipt
    assert calls == ["host", "private-paths", "claim", "closed"]


def test_native_profile_edit_after_claim_fences_collection_and_publication(monkeypatch):
    from potbot.domain.errors import ScopeDeniedError
    from potbot.tools import mcp_discovery_host

    service, args, _, profile, calls = pending_reconciliation(monkeypatch)
    reads = 0

    def read_profile(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return profile
        return SimpleNamespace(**{**vars(profile), "configuration_sha256": "d" * 64})

    monkeypatch.setattr(mcp_discovery_host, "read_mcp_profile", read_profile)
    with pytest.raises(ScopeDeniedError):
        asyncio.run(_reconcile(service, object(), object(), args, "token"))
    assert "claim" in calls
    assert "observe" not in calls and "complete" not in calls
    assert ("failed", "authority_changed") in calls and calls[-1] == "closed"


@pytest.mark.parametrize("changed", ["review", "configuration", "agent", "installation"])
def test_native_preflight_denies_changed_review_or_owner_without_claim(monkeypatch, changed):
    from potbot.domain.errors import RevisionConflictError, ScopeDeniedError

    service, args, selected, profile, calls = pending_reconciliation(monkeypatch)
    if changed == "review":
        selected["revision"] += 1
    elif changed == "configuration":
        profile.configuration_sha256 = "d" * 64
    elif changed == "agent":
        profile.agent_id = uuid4()
    else:
        profile.installed.installation.fingerprint = "d" * 64
    with pytest.raises((RevisionConflictError, ScopeDeniedError)):
        asyncio.run(_reconcile(service, object(), object(), args, "token"))
    assert not calls


@pytest.mark.parametrize(
    "error_name,reason",
    [
        ("IndeterminateExecutionError", "reconciliation_indeterminate"),
        ("OperationCancelledError", "reconciliation_cancelled"),
        ("ScopeDeniedError", "authority_changed"),
        ("McpCatalogChangedError", "catalog_changed"),
        ("RuntimeError", "reconciliation_failed"),
    ],
)
def test_native_failure_records_safe_reason_without_publishing(monkeypatch, error_name, reason):
    from potbot.domain import errors
    from potbot.tools.mcp_protocol import McpCatalogChangedError

    error_class = {
        "McpCatalogChangedError": McpCatalogChangedError,
        "RuntimeError": RuntimeError,
    }.get(error_name, getattr(errors, error_name, RuntimeError))
    error = error_class("private native failure body")
    service, args, _, _, calls = pending_reconciliation(monkeypatch, host_error=error)
    with pytest.raises(error_class):
        asyncio.run(_reconcile(service, object(), object(), args, "token"))
    assert ("failed", reason) in calls and "complete" not in calls and calls[-1] == "closed"
