"""T01.4 — offline stage passing never masquerades as a qualified release."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import tool_phase1_gate as gate
from scripts.tool_platform_inventory import ANCHORS


def seed(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "uv.lock", "conftest.py"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    for row in ANCHORS:
        if row.decision == "missing":
            continue
        target = tmp_path / row.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture", encoding="utf-8")


def mock_check_success(tmp_path: Path, args: list[str], *, timeout: int) -> int:
    for arg in args:
        if arg.startswith("--junitxml="):
            report = Path(arg.partition("=")[2])
            report.write_text(
                '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
                '<testcase name="one"/><testcase name="two"/></testsuite></testsuites>'
            )
            return 0
    if "--with-postgres" in args and "--with-sandbox" in args:
        artifacts = Path(args[args.index("--artifacts") + 1])
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "summary.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "postgres_requested": True,
                    "sandbox_requested": True,
                    "checks": [
                        {"check": "postgres", "exit_code": 0},
                        {"check": "sandbox", "exit_code": 0},
                    ],
                }
            )
        )
        for name in ("postgres", "sandbox"):
            (artifacts / f"{name}.xml").write_text(
                '<testsuite tests="3" failures="0" errors="0" skipped="0">'
                '<testcase name="one"/><testcase name="two"/><testcase name="three"/></testsuite>'
            )
    return 0


def test_offline_stage_report_explicitly_unqualified(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)
    monkeypatch.setattr(gate, "_run", mock_check_success)
    report = gate.run_gate(tmp_path, tmp_path / "stage-artifacts", mode="offline")
    assert report["offline_contract_checks_passed"] is True
    assert report["fully_qualified"] is False
    assert all(stage["status"] == "offline_passed" for stage in report["stages"])
    assert (tmp_path / "stage-artifacts" / "source-inventory.json").is_file()
    assert (tmp_path / "stage-artifacts" / "phase1-gate.json").is_file()


def test_qualified_release_requires_existing_db_and_sandbox_gates(
    tmp_path: Path, monkeypatch
) -> None:
    seed(tmp_path)
    monkeypatch.setattr(gate, "_run", mock_check_success)
    report = gate.run_gate(tmp_path, tmp_path / "qualified", mode="qualified")
    assert report["fully_qualified"] is True
    assert report["qualification"]["performed"] is True
    assert report["qualification_scope"] == "application_postgres_and_sandbox_tests"
    assert report["production_runtime_qualified"] is False
    assert report["sources_complete"] is True
    assert report["sources_unchanged"] is True


def test_failed_existing_check_never_qualifies(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)

    def fail_full(root: Path, args: list[str], *, timeout: int) -> int:
        if "--with-postgres" in args:
            return 124
        return mock_check_success(root, args, timeout=timeout)

    monkeypatch.setattr(gate, "_run", fail_full)
    report = gate.run_gate(tmp_path, tmp_path / "broken", mode="qualified")
    assert report["offline_contract_checks_passed"] is True
    assert report["fully_qualified"] is False


def test_skipped_stage_check_is_a_failure(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)

    def skipped(root: Path, args: list[str], *, timeout: int) -> int:
        for arg in args:
            if arg.startswith("--junitxml="):
                Path(arg.partition("=")[2]).write_text(
                    '<testsuite tests="2" failures="0" errors="0" skipped="1">'
                    '<testcase name="one"/><testcase name="two"><skipped/></testcase></testsuite>'
                )
                return 0
        return 0

    monkeypatch.setattr(gate, "_run", skipped)
    report = gate.run_gate(tmp_path, tmp_path / "skipped", mode="offline")
    assert report["offline_contract_checks_passed"] is False


@pytest.mark.parametrize("profile", ["postgres", "sandbox"])
@pytest.mark.parametrize("failure", ["skipped", "missing", "empty", "malformed"])
def test_full_summary_cannot_hide_missing_or_skipped_required_tests(
    tmp_path: Path, monkeypatch, profile: str, failure: str
) -> None:
    seed(tmp_path)

    def incomplete(root: Path, args: list[str], *, timeout: int) -> int:
        result = mock_check_success(root, args, timeout=timeout)
        if "--with-postgres" in args:
            path = Path(args[args.index("--artifacts") + 1]) / f"{profile}.xml"
            if failure == "missing":
                path.unlink()
            elif failure == "empty":
                path.write_text('<testsuite tests="0" failures="0" errors="0" skipped="0"/>')
            elif failure == "malformed":
                path.write_text('<testsuite tests="many" failures="0" errors="0" skipped="0"/>')
            else:
                path.write_text(
                    '<testsuite tests="3" failures="0" errors="0" skipped="1">'
                    '<testcase name="one"/><testcase name="two"/>'
                    '<testcase name="three"><skipped/></testcase></testsuite>'
                )
        return result

    monkeypatch.setattr(gate, "_run", incomplete)
    report = gate.run_gate(tmp_path, tmp_path / "artifacts", mode="qualified")
    assert report["offline_contract_checks_passed"] is True
    assert report["fully_qualified"] is False


def test_previous_green_reports_cannot_qualify_another_run(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(gate, "_run", mock_check_success)
    previous = gate.run_gate(tmp_path, artifacts, mode="qualified")
    assert previous["fully_qualified"] is True

    monkeypatch.setattr(gate, "_run", lambda *args, **kwargs: 0)
    current = gate.run_gate(tmp_path, artifacts, mode="qualified")
    assert current["run_id"] != previous["run_id"]
    assert current["offline_contract_checks_passed"] is False
    assert current["fully_qualified"] is False
    assert json.loads((artifacts / "phase1-gate.json").read_text()) == current
    assert (artifacts / previous["evidence_directory"] / "phase1-gate.json").exists()


def test_interrupted_run_invalidates_previous_published_success(
    tmp_path: Path, monkeypatch
) -> None:
    seed(tmp_path)
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(gate, "_run", mock_check_success)
    assert gate.run_gate(tmp_path, artifacts, mode="qualified")["fully_qualified"] is True

    def interrupted(*args, **kwargs) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(gate, "_run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        gate.run_gate(tmp_path, artifacts, mode="qualified")
    current = json.loads((artifacts / "phase1-gate.json").read_text())
    assert current["status"] == "running"
    assert current["fully_qualified"] is False


@pytest.mark.parametrize("state", [[], None, "malformed", {"checks": None}])
def test_invalid_full_summary_fails_closed(tmp_path: Path, monkeypatch, state: object) -> None:
    seed(tmp_path)

    def invalid(root: Path, args: list[str], *, timeout: int) -> int:
        result = mock_check_success(root, args, timeout=timeout)
        if "--with-postgres" in args:
            path = Path(args[args.index("--artifacts") + 1]) / "summary.json"
            path.write_text("{" if state == "malformed" else json.dumps(state))
        return result

    monkeypatch.setattr(gate, "_run", invalid)
    assert (
        gate.run_gate(tmp_path, tmp_path / "artifacts", mode="qualified")["fully_qualified"]
        is False
    )


def test_conflicting_duplicate_check_records_are_rejected(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)

    def conflict(root: Path, args: list[str], *, timeout: int) -> int:
        result = mock_check_success(root, args, timeout=timeout)
        if "--with-postgres" in args:
            path = Path(args[args.index("--artifacts") + 1]) / "summary.json"
            state = json.loads(path.read_text())
            state["checks"].append({"check": "postgres", "exit_code": 1})
            path.write_text(json.dumps(state))
        return result

    monkeypatch.setattr(gate, "_run", conflict)
    assert (
        gate.run_gate(tmp_path, tmp_path / "artifacts", mode="qualified")["fully_qualified"]
        is False
    )


def test_source_change_during_execution_invalidates_report(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)

    def changed(root: Path, args: list[str], *, timeout: int) -> int:
        result = mock_check_success(root, args, timeout=timeout)
        if "--with-postgres" in args:
            (root / "src" / "changed.py").write_text("changed runtime source\n")
        return result

    monkeypatch.setattr(gate, "_run", changed)
    report = gate.run_gate(tmp_path, tmp_path / "artifacts", mode="qualified")
    assert report["sources_unchanged"] is False
    assert report["status"] == "failed"
    assert report["fully_qualified"] is False


def test_missing_locked_environment_cannot_qualify(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)
    (tmp_path / "uv.lock").unlink()
    monkeypatch.setattr(gate, "_run", mock_check_success)
    report = gate.run_gate(tmp_path, tmp_path / "artifacts", mode="qualified")
    assert report["sources_complete"] is False
    assert report["fully_qualified"] is False


@pytest.mark.parametrize(
    "document",
    [
        '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        "<testcase><failure/></testcase></testsuite>",
        '<testsuite tests="1" failures="0" errors="1" skipped="0">'
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        "<testcase/></testsuite></testsuite>",
    ],
)
def test_inconsistent_junit_counters_never_establish_a_pass(tmp_path: Path, document: str) -> None:
    path = tmp_path / "cases.xml"
    path.write_text(document)
    assert gate._passed(gate._cases(path)) is False


def test_nested_suite_totals_are_validated_without_double_counting(tmp_path: Path) -> None:
    path = tmp_path / "cases.xml"
    path.write_text(
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase name="outer"/>'
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="inner"/></testsuite></testsuite></testsuites>'
    )
    assert gate._cases(path) == {"collected": 2, "failed": 0, "errors": 0, "skipped": 0}
