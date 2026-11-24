"""Integration checks for report identity, strict preflight and derived-judge stops."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.evaluation.agentic_atomic_bench import validated_atomic_observations
from potbot.evaluation.agentic_model_bench import Generation
from potbot.models import preflight

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "deployment_test_helpers", ROOT / "tests/unit/test_agentic_deployments.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
cli = helpers.cli


def configuration(tmp_path):
    models, tasks = [helpers.target()], helpers.suite()
    for name, document in (
        ("models", {"schema_version": 1, "models": [asdict(m) for m in models]}),
        ("suite", {"schema_version": 1, "id": tasks.id, "cases": [asdict(c) for c in tasks.cases]}),
    ):
        (tmp_path / (name + ".json")).write_text(json.dumps(document))
    return ["--models", str(tmp_path / "models.json"), "--suite", str(tmp_path / "suite.json")]


def test_complete_target_configuration_is_bound_and_endpoint_drift_is_refused(monkeypatch):
    class Transport:
        def __init__(self, **_kwargs):
            pass

        def request(self, target, _packet):
            return Generation('{"status":"passed"}', 2, returned_model=target.model)

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    tasks, models = helpers.suite(), [helpers.target()]
    report = cli._execute(helpers.args(), tasks, models, ["generic"], None)
    assert report["model_targets_sha256"] == content_hash([asdict(m) for m in models])
    validated_atomic_observations(report, tasks, models, variants=("generic",), repeat=1)
    changed = [replace(models[0], base_url="http://127.0.0.1:12345/v1")]
    with pytest.raises(InvalidInputError, match="configuration identity"):
        validated_atomic_observations(report, tasks, changed, variants=("generic",), repeat=1)


def test_duplicate_keys_in_bindings_produce_sanitized_cli_failure(tmp_path, monkeypatch, capsys):
    flags = configuration(tmp_path)
    path = tmp_path / "binding.json"
    path.write_text('{"schema":"private-fixture-value","schema":"duplicate","bindings":[]}')
    monkeypatch.setattr(cli, "BenchmarkTransport", helpers.forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "preflight",
                *flags,
                "--deployment-bindings",
                str(path),
                "--project-root",
                str(tmp_path),
            ]
        )
    assert error.value.code == 2
    assert "private-fixture-value" not in capsys.readouterr().err


def test_bound_preflight_exports_observed_origin_and_blocks_drift_without_dispatch(
    tmp_path, monkeypatch, capsys
):
    flags = configuration(tmp_path)
    profile = helpers.fixtures.host_profile(tmp_path)
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(helpers.bindings(profile)))
    monkeypatch.setattr(cli, "BenchmarkTransport", helpers.forbidden)
    monkeypatch.setattr(preflight, "inspect_local_host", lambda _: helpers.fixtures.observation())
    command = [
        "preflight",
        *flags,
        "--deployment-bindings",
        str(path),
        "--project-root",
        str(tmp_path),
    ]
    assert cli.main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["network_started"] is False
    inspection = report["deployment_inspection"]
    assert inspection["status"] == "ready_for_load_check"
    assert inspection["targets"][0]["preflight"]["host_observation"]["origin"] == "fixture"
    (tmp_path / profile.model_directory / "tokenizer.json").write_text("changed")
    assert cli.main(command) == 2
    assert json.loads(capsys.readouterr().out)["deployment_inspection"]["status"] == "blocked"


def test_derived_judge_identity_mismatch_stops_before_reverse_or_next_judge(monkeypatch):
    calls = []

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def request(self, target, _packet):
            calls.append(target.id)
            return Generation(
                '{"status":"passed"}' if target.id == "local" else '{"status":"failed"}',
                2,
                billed_microunits=3,
                returned_model="substitute" if "judge" in target.roles else target.model,
            )

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    models = [
        helpers.target(),
        helpers.target("wrong", model="wrong/model"),
        helpers.target("judge", model="judge/model", roles=("judge",)),
        helpers.target("next-judge", model="next/model", roles=("judge",)),
    ]
    report = cli._execute(
        helpers.args(judge_derived=True), helpers.suite(1), models, ["generic"], None
    )
    assert len(calls) == 3 and calls[-1] in {"judge", "next-judge"}
    assert len(report["results"]) == 3
    assert report["results"][-1]["status"] == "identity_mismatch"
    assert report["results"][-1]["billed_microunits"] == 3
    assert report["execution"]["status"] == "stopped_model_identity_mismatch"
