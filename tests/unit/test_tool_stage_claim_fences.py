"""S10 authorization fencing with real leases/files and explicit supervisor fixtures."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.tool_stage_s10 import (
    ToolBuildPlan,
    _SafeCopy,
    build_and_stage,
    command_digest,
    stage_name,
)
from tests.unit.test_shared_tool_upgrades_s9 import install
from tests.unit.test_shared_tool_upgrades_s9 import rig as rig
from tests.unit.test_tool_stage_s10 import passing


@pytest.mark.parametrize("after_stop", [False, True])
def test_stale_claim_cannot_publish_a_stage_or_leave_a_false_uncertain_process(rig, after_stop):
    r = rig
    home = r["homes"].lookup(r["workspace_id"], r["agent_id"])
    built = r["runtime"].home / home.directory / "build"
    definition = install(built, "v3")
    config, history, evidence = (r["private"] / name for name in ("config", "history", "receipts"))
    for directory in (config, history, evidence):
        directory.mkdir(mode=0o700)
    runtime = replace(r["runtime"], ai_config_dir=config, ai_history_dir=history)
    calls = []

    def run(request, *, cancelled):
        assert not cancelled()
        calls.append(request)
        return passing()

    operator = SimpleNamespace(
        workspace_id=r["workspace_id"],
        runtime=runtime,
        monitor=SimpleNamespace(require_active=lambda: None),
        supervisor=SimpleNamespace(run=run),
    )
    argv = ("/usr/bin/python3", "--version")
    plan = ToolBuildPlan(
        r["workspace_id"],
        r["agent_id"],
        uuid4(),
        "stemlab",
        argv,
        command_digest(argv),
        r["old"].commit,
        definition.commit,
    )

    def authorize():
        assert any(
            row["operation"] == "tool.stage_s10" for row in r["ledger"].active(r["workspace_id"])
        )
        if not after_stop or calls:
            raise ScopeDeniedError("stale acquisition claim")

    with pytest.raises(ScopeDeniedError, match="stale"):
        build_and_stage(
            plan=plan,
            operator=operator,
            store=r["store"],
            homes=r["homes"],
            evidence_dir=evidence,
            built_tree=built,
            authorize_attempt=authorize,
        )
    assert len(calls) == (1 if after_stop else 0)
    assert not (r["tools"] / stage_name("stemlab", plan.operation_id)).exists()
    assert not (r["tools"] / (".potbot-s10-incoming-" + plan.operation_id.hex)).exists()
    assert r["ledger"].active(r["workspace_id"]) == []


@pytest.mark.parametrize("wrong_path", [False, True])
def test_source_materialization_and_dispatch_share_the_same_writer_lease(rig, wrong_path):
    r = rig
    home = r["homes"].lookup(r["workspace_id"], r["agent_id"])
    built = r["runtime"].home / home.directory / "new-build"
    source = r["private"] / "source"
    definition = install(source, "v3")
    config, history, evidence = (r["private"] / name for name in ("config", "history", "receipts"))
    for directory in (config, history, evidence):
        directory.mkdir(mode=0o700)
    runtime = replace(r["runtime"], ai_config_dir=config, ai_history_dir=history)
    order = []
    generation = []

    def live_lease():
        active = r["ledger"].active(r["workspace_id"])
        assert len(active) == 1 and active[0]["operation"] == "tool.stage_s10"
        generation.append(active[0]["generation"])

    def materialize():
        live_lease()
        order.append("materialize")
        if wrong_path:
            return source
        assert not built.exists()
        built.mkdir(mode=0o700)
        _SafeCopy().copy(source, built)
        return built

    def run(request, *, cancelled):
        live_lease()
        order.append("dispatch")
        assert order == ["materialize", "dispatch"]
        assert request.cwd == built and not cancelled()
        return passing()

    operator = SimpleNamespace(
        workspace_id=r["workspace_id"],
        runtime=runtime,
        monitor=SimpleNamespace(require_active=lambda: None),
        supervisor=SimpleNamespace(run=run),
    )
    argv = ("/usr/bin/python3", "--version")
    plan = ToolBuildPlan(
        r["workspace_id"],
        r["agent_id"],
        uuid4(),
        "stemlab",
        argv,
        command_digest(argv),
        r["old"].commit,
        definition.commit,
    )
    arguments = dict(
        plan=plan,
        operator=operator,
        store=r["store"],
        homes=r["homes"],
        evidence_dir=evidence,
        built_tree=built,
        authorize_attempt=live_lease,
        prepare_tree=materialize,
    )
    if wrong_path:
        with pytest.raises(StateMismatchError, match="different build tree"):
            build_and_stage(**arguments)
        assert order == ["materialize"]
    else:
        receipt = build_and_stage(**arguments)
        assert receipt["stage_name"] == stage_name("stemlab", plan.operation_id)
        assert (r["tools"] / receipt["stage_name"] / "static" / "stemlab").read_bytes() == b"v3"
        assert (r["tools"] / receipt["stage_name"] / "checkout").stat().st_mode & 0o777 == 0o755
    assert len(set(generation)) == 1
    assert r["ledger"].active(r["workspace_id"]) == []
