"""Partition integrity and actual child-pytest completion, without a database."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.evaluation.postgres_test_shards import (
    SELECTOR,
    PostgresShard,
    collection_receipt,
    nodeids_digest,
    verify_postgres_shards,
)

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40


def complete_reports():
    nodes = [f"tests/integration/test_example.py::test_case_{index}" for index in range(80)]
    reports = []
    for index in range(4):
        row = collection_receipt(PostgresShard(index, 4), nodes, REVISION)
        row.update(
            completed=True,
            pytest_exit_code=0,
            terminal_outcomes=[
                {"nodeid": node, "outcome": "passed"} for node in row["selected_nodeids"]
            ],
        )
        reports.append(row)
    return reports


@pytest.mark.parametrize(
    "text", ["", "-1/4", "4/4", "0/0", "0/17", "00/4", "0/04", "1", "0/4 ", "True/4"]
)
def test_invalid_or_ambiguous_shard_specs_are_refused(text):
    with pytest.raises(ValueError):
        PostgresShard.parse(text)


def test_partitions_are_disjoint_complete_and_order_independent():
    nodes = [f"case-{index}" for index in range(500)]
    parts = [PostgresShard(index, 4).selected(nodes) for index in range(4)]
    assert set().union(*map(set, parts)) == set(nodes)
    assert sum(map(len, parts)) == len(nodes)
    assert all(
        part == PostgresShard(index, 4).selected(list(reversed(nodes)))
        for index, part in enumerate(parts)
    )
    result = verify_postgres_shards(complete_reports(), REVISION)
    assert result["coverage_complete"] is True
    assert result["collected_tests"] == result["accounted_tests"] == 80
    assert result["runtime_qualified"] is False


@pytest.mark.parametrize(
    "change",
    [
        "missing_partition",
        "duplicate_partition",
        "source",
        "selector",
        "collection",
        "incomplete",
        "failed",
        "missing_outcome",
        "duplicate_outcome",
        "failed_outcome",
        "selected_hash",
        "missing_node",
        "extra_field",
        "bool_exit",
        "wrong_assignment",
    ],
)
def test_coverage_gate_rejects_incomplete_foreign_or_falsified_receipts(change):
    reports = complete_reports()
    row = reports[0]
    if change == "missing_partition":
        reports.pop()
    elif change == "duplicate_partition":
        reports[-1] = copy.deepcopy(row)
    elif change == "source":
        row["source_commit"] = "b" * 40
    elif change == "selector":
        row["selector"] = "postgres"
    elif change == "collection":
        row["collection_sha256"] = "0" * 64
    elif change == "incomplete":
        row["completed"] = False
    elif change == "failed":
        row["pytest_exit_code"] = 1
    elif change == "missing_outcome":
        row["terminal_outcomes"].pop()
    elif change == "duplicate_outcome":
        row["terminal_outcomes"][-1] = copy.deepcopy(row["terminal_outcomes"][0])
    elif change == "failed_outcome":
        row["terminal_outcomes"][0]["outcome"] = "failed"
    elif change == "selected_hash":
        row["selected_sha256"] = "0" * 64
    elif change == "missing_node":
        node = row["selected_nodeids"].pop()
        row["selected_sha256"] = nodeids_digest(row["selected_nodeids"])
        row["terminal_outcomes"] = [
            item for item in row["terminal_outcomes"] if item["nodeid"] != node
        ]
    elif change == "extra_field":
        row["extra"] = "field"
    elif change == "bool_exit":
        row["pytest_exit_code"] = False
    else:
        row["selected_nodeids"].append(reports[1]["selected_nodeids"][0])
        row["selected_nodeids"].sort()
        row["selected_sha256"] = nodeids_digest(row["selected_nodeids"])
    with pytest.raises(ValueError):
        verify_postgres_shards(reports, REVISION)


def test_skip_is_disclosed_without_becoming_an_executed_pass():
    reports = complete_reports()
    reports[0]["terminal_outcomes"][0]["outcome"] = "skipped"
    result = verify_postgres_shards(reports, REVISION)
    assert result["terminal_outcomes"] == {"passed": 79, "skipped": 1}


def project(tmp_path, *, fail=False, abort=False):
    directory = tmp_path / "synthetic-project"
    directory.mkdir()
    (directory / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    postgres: fixture\n    sandbox: fixture\n"
    )
    code = "import pytest\n"
    if abort:
        code += "@pytest.fixture(autouse=True)\ndef abort_fixture():\n    pytest.exit('synthetic early stop', returncode=0)\n"
    for index in range(12):
        code += f"@pytest.mark.postgres\ndef test_pg_{index}():\n    assert {not (fail and index == 0)}\n"
    code += "@pytest.mark.postgres\n@pytest.mark.skip(reason='explicit synthetic skip')\ndef test_pg_skip():\n    assert False\n"
    code += "@pytest.mark.postgres\n@pytest.mark.sandbox\ndef test_sandbox_excluded():\n    assert False\n"
    code += "def test_unmarked_excluded():\n    assert False\n"
    (directory / "test_cases.py").write_text(code)
    return directory


def child(directory, index, *, extra=(), selector=SELECTOR):
    destination = directory / f"shard-{index}.json"
    environment = dict(os.environ)
    environment.update(
        {
            "GITHUB_SHA": REVISION,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": "",
            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
        }
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "scripts.postgres_shard_plugin",
        "-m",
        selector,
        "--postgres-shard",
        f"{index}/4",
        "--postgres-shard-report",
        str(destination),
        "--confcutdir",
        str(directory),
        "--basetemp",
        str(directory / f"temporary-{index}"),
        *extra,
    ]
    result = subprocess.run(
        command,
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result, destination


def test_actual_pytest_partitions_preserve_the_original_selector_and_all_terminal_cases(tmp_path):
    directory = project(tmp_path)
    receipts = []
    for index in range(4):
        result, path = child(directory, index)
        assert result.returncode == 0, result.stdout + result.stderr
        receipts.append(json.loads(path.read_text()))
    coverage = verify_postgres_shards(receipts, REVISION)
    assert coverage["collected_tests"] == 13
    assert coverage["terminal_outcomes"] == {"passed": 12, "skipped": 1}


@pytest.mark.parametrize("mode", ["failed", "aborted", "collection_only"])
def test_actual_incomplete_or_failed_pytest_run_cannot_supply_a_green_receipt(tmp_path, mode):
    directory = project(tmp_path, fail=mode == "failed", abort=mode == "aborted")
    index = next(
        index for index in range(4) if PostgresShard(index, 4).includes("test_cases.py::test_pg_0")
    )
    result, path = child(
        directory, index, extra=("--collect-only",) if mode == "collection_only" else ()
    )
    receipt = json.loads(path.read_text())
    if mode == "failed":
        assert result.returncode == 1
    else:
        assert result.returncode == 0
    nodes = [f"test_cases.py::test_pg_{number}" for number in range(12)] + [
        "test_cases.py::test_pg_skip"
    ]
    peers = []
    for peer_index in range(4):
        peer = collection_receipt(PostgresShard(peer_index, 4), nodes, REVISION)
        peer.update(
            completed=True,
            pytest_exit_code=0,
            terminal_outcomes=[
                {"nodeid": node, "outcome": "skipped" if node.endswith("_skip") else "passed"}
                for node in peer["selected_nodeids"]
            ],
        )
        peers.append(peer)
    peers[index] = receipt
    with pytest.raises(ValueError, match="complete successfully|terminal outcomes"):
        verify_postgres_shards(peers, REVISION)


def test_pytest_refuses_changed_selector_and_existing_receipt(tmp_path):
    directory = project(tmp_path)
    result, path = child(directory, 0, selector="postgres")
    assert result.returncode != 0 and not path.exists()
    path.write_text("foreign receipt")
    result, _ = child(directory, 0)
    assert result.returncode != 0 and path.read_text() == "foreign receipt"


def test_coverage_cli_accepts_complete_receipts_and_refuses_reuse_or_missing_partition(tmp_path):
    reports = complete_reports()
    files = []
    for row in reports:
        path = tmp_path / f"receipt-{row['index']}.json"
        path.write_text(json.dumps(row))
        files.append(str(path))
    output = tmp_path / "coverage.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/check_postgres_shards.py"),
        *files,
        "--source-commit",
        REVISION,
        "--output",
        str(output),
    ]
    first = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert first.returncode == 0, first.stderr
    assert json.loads(output.read_text())["accounted_tests"] == 80
    before = output.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert second.returncode == 1 and output.read_bytes() == before
    missing = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_postgres_shards.py"),
            *files[:-1],
            "--source-commit",
            REVISION,
            "--output",
            str(tmp_path / "missing.json"),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert missing.returncode == 1 and not (tmp_path / "missing.json").exists()
