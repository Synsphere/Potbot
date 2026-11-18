"""CLI help is independent of optional runtime packages; file inputs stay bounded."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.api.discovery_cli import _manifest_bytes, _measure, _parser
from potbot.domain.errors import InvalidInputError

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "command",
    [
        None,
        "sources",
        "publishers",
        "checks",
        "search",
        "source",
        "candidate",
        "check",
        "add-source",
        "refresh",
        "source-control",
        "publisher-control",
        "prepare",
        "measure",
        "handoff",
        "cancel",
    ],
)
def test_discovery_help_needs_no_site_packages(command):
    args = [sys.executable, "-S", "-m", "potbot", "discovery"]
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
    assert "usage:" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_measure_requires_review_and_pinned_native_interpreter(capsys):
    with pytest.raises(SystemExit) as stopped:
        _parser().parse_args(
            [
                "--workspace-id",
                str(uuid4()),
                "--principal-id",
                str(uuid4()),
                "measure",
                str(uuid4()),
                "--confirm",
            ]
        )
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    assert "--probe-python-sha256" in error and "--digest" in error and "--key" in error


def test_manifest_reader_rejects_symlink_directory_and_oversize(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(b'{"candidate":true}')
    assert _manifest_bytes(source) == source.read_bytes()
    link = tmp_path / "link.json"
    link.symlink_to(source)
    for path in (link, tmp_path):
        with pytest.raises(InvalidInputError):
            _manifest_bytes(path)
    source.write_bytes(b"x" * 2_097_153)
    with pytest.raises(InvalidInputError):
        _manifest_bytes(source)


def measurement_arguments():
    return SimpleNamespace(
        identity=uuid4(),
        revision=1,
        digest="a" * 64,
        key="exact-native-attempt",
        manifest=Path("/operator/runtime.json"),
        workspace_name="fixture-workspace",
        probe_python="/usr/bin/python3",
        probe_python_sha256="b" * 64,
    )


@pytest.mark.parametrize("state", ["measuring", "measured", "failed", "cancelled", "handed_off"])
def test_measure_recovers_exact_prior_command_before_native_configuration(monkeypatch, state):
    args = measurement_arguments()
    receipt = {"check": {"id": str(args.identity), "status": state}}
    calls = []

    def replay(*values, **options):
        calls.append((values, options))
        return receipt

    def unexpected(*_args, **_kwargs):
        pytest.fail("An existing measurement attempt must not configure or dispatch a runtime")

    monkeypatch.setattr("potbot.context.skill_eligibility.configured_skill_eligibility", unexpected)
    service = SimpleNamespace(measurement_replay=replay, claim_measurement=unexpected)
    context = object()
    assert _measure(service, object(), context, args, "operator-token") == receipt
    assert calls == [
        (
            (context, args.identity, args.revision, args.digest, args.key),
            {"setup_token": "operator-token"},
        )
    ]


def test_measure_lost_claim_race_returns_current_check_without_second_dispatch(monkeypatch):
    args = measurement_arguments()
    receipt = {"check": {"id": str(args.identity), "status": "measuring"}}
    calls = []

    def unexpected(*_args, **_kwargs):
        pytest.fail("The losing claimant cannot dispatch or fail another worker's attempt")

    def claim(*_args, **options):
        assert options == {"setup_token": "operator-token", "fresh_only": True}
        return {"replay": receipt}

    def configure(*values, **options):
        calls.append((values, options))
        return SimpleNamespace(evaluate=unexpected)

    monkeypatch.setattr("potbot.context.skill_eligibility.configured_skill_eligibility", configure)
    service = SimpleNamespace(
        measurement_replay=lambda *_args, **_kwargs: None,
        claim_measurement=claim,
        fail_measurement=unexpected,
    )
    assert _measure(service, object(), object(), args, "operator-token") == receipt
    assert len(calls) == 1


def test_measure_configuration_failure_leaves_no_claim(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise InvalidInputError("fixture runtime unavailable")

    def unexpected(*_args, **_kwargs):
        pytest.fail("Invalid runtime configuration cannot consume the measurement attempt")

    monkeypatch.setattr(
        "potbot.context.skill_eligibility.configured_skill_eligibility", unavailable
    )
    service = SimpleNamespace(
        measurement_replay=lambda *_args, **_kwargs: None, claim_measurement=unexpected
    )
    with pytest.raises(InvalidInputError):
        _measure(service, object(), object(), measurement_arguments(), "operator-token")
