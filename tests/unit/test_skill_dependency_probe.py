"""Run the fixed probe program as a unit fixture, never as native qualification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.skill_dependency_probe import (
    PROBE_PROGRAM,
    PROBE_SCHEMA,
    PROBE_SHA256,
    binary_supported,
    parse_observation,
    probe_input,
)


def test_fixed_probe_reads_an_executable_without_running_its_code(tmp_path):
    marker = tmp_path / "must-not-be-created"
    executable = tmp_path / "dependency"
    executable.write_text(f"#!/bin/sh\ntouch {marker}\n")
    executable.chmod(0o755)
    # Running yes would produce unbounded output. Its successful hash plus the
    # rejected mutable /tmp fixture proves neither dependency was executed.
    selected = {
        "schema": PROBE_SCHEMA,
        "binaries": ["/usr/bin/yes", str(executable)],
        "environment": ["PATH"],
    }
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", PROBE_PROGRAM],
        input=json.dumps(selected).encode(),
        capture_output=True,
        check=True,
        timeout=5,
        env={"PATH": "never-emit-this-value"},
    )
    observed = json.loads(result.stdout)
    assert observed["binaries"] == [
        {
            "name": "/usr/bin/yes",
            "state": "present",
            "sha256": hashlib.sha256(Path("/usr/bin/yes").read_bytes()).hexdigest(),
        },
        {
            "name": str(executable),
            "state": "outside_scope",
            "sha256": None,
        },
    ]
    assert observed["environment"] == [{"name": "PATH", "present": True}]
    assert not marker.exists()
    assert b"never-emit-this-value" not in result.stdout
    assert result.stderr == b""
    assert PROBE_SHA256 == hashlib.sha256(PROBE_PROGRAM.encode()).hexdigest()


def test_fixed_probe_cannot_block_on_special_files(tmp_path):
    selected = tmp_path / "pipe"
    os.mkfifo(selected, 0o755)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", PROBE_PROGRAM],
        input=json.dumps(
            {"schema": PROBE_SCHEMA, "binaries": [str(selected)], "environment": []}
        ).encode(),
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert json.loads(result.stdout)["binaries"][0]["state"] == "outside_scope"


def test_image_binary_alias_to_a_mutable_mount_is_never_reported_available():
    # Explicit namespace fixture: imitate an image symlink into an agent home.
    # The production program itself is unchanged and must reject the target
    # before attempting to read it or emit an executable hash.
    wrapper = "import os\nos.path.realpath = lambda path: '/home/agent/mutable-code'\n"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", wrapper + PROBE_PROGRAM],
        input=probe_input(["python3"], []),
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert json.loads(result.stdout)["binaries"] == [
        {"name": "python3", "state": "outside_scope", "sha256": None}
    ]


@pytest.mark.parametrize(
    "name",
    [
        "python; echo nope",
        "$(id)",
        "../bin/python",
        "/etc/passwd",
        "/home/agent/bin/python",
        "/usr/local/tools/demo/static/../secret",
        "a\x00b",
        "a" * 129,
    ],
)
def test_untrusted_selectors_never_enter_probe_input(name):
    assert not binary_supported(name)
    with pytest.raises(InvalidInputError):
        probe_input([name], [])


def test_secret_environment_names_are_not_supported_by_base_runtime_probe():
    with pytest.raises(InvalidInputError):
        probe_input([], ["OPENAI_API_KEY"])
    with pytest.raises(InvalidInputError):
        probe_input([], [{}])


def observation():
    return {
        "schema": PROBE_SCHEMA,
        "operatingSystem": "linux",
        "uid": 420,
        "gid": 69,
        "binaries": [{"name": "python3", "state": "present", "sha256": "a" * 64}],
        "environment": [{"name": "PATH", "present": True}],
    }


@pytest.mark.parametrize(
    "mutation",
    ["extra", "selector", "identity", "secret", "hash", "missing", "duplicates", "large"],
)
def test_observations_must_match_exact_selectors_identity_and_value_blind_schema(mutation):
    value = observation()
    if mutation == "extra":
        value["unexpected"] = "untrusted"
    elif mutation == "selector":
        value["binaries"][0]["name"] = "curl"
    elif mutation == "identity":
        value["uid"] = 0
    elif mutation == "secret":
        value["environment"][0]["value"] = "secret"
    elif mutation == "hash":
        value["binaries"][0]["sha256"] = None
    elif mutation == "missing":
        value["binaries"] = []
    raw = json.dumps(value).encode()
    if mutation == "duplicates":
        raw = raw.replace(b'"uid": 420', b'"uid": 0, "uid": 420')
    if mutation == "large":
        raw += b" " * 131072
    with pytest.raises(StateMismatchError):
        parse_observation(raw, binaries=["python3"], environment=["PATH"], uid=420, gid=69)


def test_observation_preserves_only_requested_presence_and_binary_hash():
    value = observation()
    assert (
        parse_observation(
            json.dumps(value).encode(), binaries=["python3"], environment=["PATH"], uid=420, gid=69
        )
        == value
    )
