#!/usr/bin/env python3
"""T01.1–T01.4 gate: explicit offline versus fully qualified outcomes.

An offline green result NEVER claims PostgreSQL, sandbox, custody, MCP, provider
or production verification. The qualified gate delegates to the established
Potbot scripts/check.py with mandatory PostgreSQL and sandbox suites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from scripts.tool_platform_inventory import build_inventory
else:
    try:
        from tool_platform_inventory import build_inventory
    except ImportError:
        from scripts.tool_platform_inventory import build_inventory

TEST_MODULES = {
    "T01.1": "tests/unit/test_tool_platform_inventory.py",
    "T01.2": "tests/unit/test_tool_capability_contracts.py",
    "T01.3": "tests/unit/test_tool_invocation_provenance.py",
    "T01.4": "tests/unit/test_tool_phase1_gate.py",
}


def _run(root: Path, args: list[str], *, timeout: int) -> int:
    try:
        result = subprocess.run(
            args,
            cwd=root,
            check=False,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        return 124


def _cases(path: Path) -> dict[str, int]:
    unavailable = {"collected": 0, "failed": 0, "errors": 1, "skipped": 0}

    def summarize(element: ET.Element) -> dict[str, int]:
        counts = {"collected": 0, "failed": 0, "errors": 0, "skipped": 0}
        for child in element:
            if child.tag in {"testsuite", "testsuites"}:
                for key, count in summarize(child).items():
                    counts[key] += count
            elif child.tag == "testcase":
                counts["collected"] += 1
                for key, tag in (
                    ("failed", "failure"),
                    ("errors", "error"),
                    ("skipped", "skipped"),
                ):
                    counts[key] += len(child.findall(tag))
            elif child.tag in {"failure", "error", "skipped"}:
                raise ValueError("Outcome outside a test case")
        for key, field in (
            ("collected", "tests"),
            ("failed", "failures"),
            ("errors", "errors"),
            ("skipped", "skipped"),
        ):
            value = element.get(field)
            if element.tag == "testsuites" and value is None:
                continue  # pytest's outer container need not duplicate suite totals.
            if (
                value is None
                or not value.isascii()
                or not value.isdecimal()
                or int(value) != counts[key]
            ):
                raise ValueError("Test suite counts disagree with actual cases")
        return counts

    try:
        root = ET.parse(path).getroot()
        if root.tag not in {"testsuite", "testsuites"}:
            return unavailable
        return summarize(root)
    except (OSError, ET.ParseError, ValueError):
        return unavailable


def _passed(counts: dict[str, int]) -> bool:
    return counts["collected"] > 0 and all(
        counts[key] == 0 for key in ("failed", "errors", "skipped")
    )


def _source_snapshot(root: Path) -> dict[str, str | None]:
    """Bind the actual working sources, including uncommitted additions.

    A Git HEAD alone would label dirty source as its previous commit. Generated
    caches/builds are excluded; no scripts or source files are imported here.
    """
    paths = {root / name for name in ("pyproject.toml", "uv.lock", "conftest.py")}
    excluded = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "target"}
    for directory in (
        "src",
        "scripts",
        "tests",
        "native",
        "sandbox",
        "vocab",
        "context_contract",
        ".github",
    ):
        if (root / directory).is_symlink():
            paths.add(root / directory)
            continue
        paths.update(
            path
            for path in (root / directory).rglob("*")
            if not excluded.intersection(path.relative_to(root).parts)
            and (path.is_symlink() or not path.is_dir())
        )
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_file() and not path.is_symlink()
        else None
        for path in sorted(paths)
    }


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    # Invalidate any previous green result before starting a new invocation,
    # and publish complete JSON atomically for readers of the retained report.
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_gate(root: Path, artifacts: Path, *, mode: str = "offline") -> dict[str, Any]:
    if mode not in ("offline", "qualified"):
        raise ValueError("Tool platform stage gate mode must be offline or qualified")
    root = root.resolve(strict=True)
    artifacts = artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    started_at = datetime.now(UTC).isoformat()
    evidence = artifacts / "runs" / run_id
    evidence.mkdir(parents=True, exist_ok=False)
    report_path = artifacts / "phase1-gate.json"
    _write_report(
        report_path,
        {
            "schema_version": 2,
            "run_id": run_id,
            "started_at": started_at,
            "status": "running",
            "mode": mode,
            "offline_contract_checks_passed": False,
            "fully_qualified": False,
            "production_runtime_qualified": False,
        },
    )
    inventory = build_inventory(root)
    (artifacts / "source-inventory.json").write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    sources = _source_snapshot(root)
    (evidence / "source-manifest.json").write_text(
        json.dumps(sources, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    stages: list[dict[str, Any]] = []
    for stage_id, selector in TEST_MODULES.items():
        junit = evidence / f"{stage_id.lower().replace('.', '-')}.xml"
        exit_code = _run(
            root,
            [sys.executable, "-m", "pytest", "-q", selector, f"--junitxml={junit}"],
            timeout=180,
        )
        summary = _cases(junit)
        stages.append(
            {
                "stage": stage_id,
                "test_selector": selector,
                "status": "offline_passed" if exit_code == 0 and _passed(summary) else "failed",
                "return_code": exit_code,
                "tests": summary,
                "junit": junit.relative_to(artifacts).as_posix(),
            }
        )
    if inventory["missing_required_anchors"]:
        stages[0]["status"] = "failed"
    offline_passed = all(row["status"] == "offline_passed" for row in stages)
    qualified = False
    qualification: dict[str, Any] = {
        "requested": mode == "qualified",
        "performed": False,
        "postgresql": "not_run",
        "sandbox": "not_run",
        "check_success": False,
    }
    if mode == "qualified" and offline_passed:
        # Tests + database migrations + sandbox + wheel checks executed by the
        # repository's existing trusted runner. No shadow DB runner is added.
        report_dir = evidence / "full-check"
        exit_code = _run(
            root,
            [
                sys.executable,
                str(root / "scripts" / "check.py"),
                "--with-postgres",
                "--with-sandbox",
                "--artifacts",
                str(report_dir),
            ],
            timeout=1800,
        )
        state_path = report_dir / "summary.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        checks = state.get("checks")
        if not isinstance(checks, list):
            checks = []
        database_tests = _cases(report_dir / "postgres.xml")
        sandbox_tests = _cases(report_dir / "sandbox.xml")
        qualified = bool(
            exit_code == 0
            and state.get("success") is True
            and state.get("postgres_requested") is True
            and state.get("sandbox_requested") is True
            and all(
                len(
                    matching := [
                        row for row in checks if isinstance(row, dict) and row.get("check") == label
                    ]
                )
                == 1
                and type(matching[0].get("exit_code")) is int
                and matching[0]["exit_code"] == 0
                for label in ("postgres", "sandbox")
            )
            and _passed(database_tests)
            and _passed(sandbox_tests)
        )
        qualification = {
            "requested": True,
            "performed": state_path.is_file(),
            "postgresql": "passed" if qualified else "failed_or_unavailable",
            "sandbox": "passed" if qualified else "failed_or_unavailable",
            "check_success": qualified,
            "return_code": exit_code,
            "postgresql_tests": database_tests,
            "sandbox_tests": sandbox_tests,
            "evidence_directory": report_dir.relative_to(artifacts).as_posix(),
        }
    sources_complete = bool(sources) and all(sources.values())
    sources_unchanged = (
        sources == _source_snapshot(root)
        and inventory["inventory_sha256"] == build_inventory(root)["inventory_sha256"]
    )
    qualified = qualified and sources_complete and sources_unchanged
    offline_passed = offline_passed and sources_complete and sources_unchanged
    report: dict[str, Any] = {
        "schema_version": 2,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": "passed" if (qualified if mode == "qualified" else offline_passed) else "failed",
        "programme": "potbot-universal-tool-mcp-integration",
        "phase": "T01",
        "source_inventory_hash": inventory["inventory_sha256"],
        "source_head": inventory["checkout_head"],
        "source_manifest_sha256": _digest(sources),
        "sources_complete": sources_complete,
        "sources_unchanged": sources_unchanged,
        "evidence_directory": evidence.relative_to(artifacts).as_posix(),
        "mode": mode,
        "offline_contract_checks_passed": offline_passed,
        "fully_qualified": qualified,
        "qualification_scope": "application_postgres_and_sandbox_tests",
        "production_runtime_qualified": False,
        "qualification": qualification,
        "stages": stages,
        "notes": [
            "This report identifies only the evidence collected for this run and source snapshot",
            "Qualified mode runs the existing application integration suites; offline mode runs contracts only",
            "A successful offline report does not qualify PostgreSQL or the Linux sandbox",
            "fully_qualified is the legacy field for this application-test profile, not all T01 requirements",
            "Native Syn, published signed worker images, live providers and specialist profiles need separate acceptance",
            "Every required database and sandbox test must run; missing, skipped or stale reports cannot qualify",
        ],
    }
    report["report_sha256"] = _digest(report)
    _write_report(evidence / "phase1-gate.json", report)
    _write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--mode", choices=("offline", "qualified"), default="offline")
    args = parser.parse_args()
    artifacts = args.artifacts or args.root / "artifacts" / "tool-platform-phase1"
    result = run_gate(args.root, artifacts, mode=args.mode)
    print(
        json.dumps(
            {
                "phase": result["phase"],
                "mode": result["mode"],
                "offline_contract_checks_passed": result["offline_contract_checks_passed"],
                "fully_qualified": result["fully_qualified"],
                "report": str(artifacts / "phase1-gate.json"),
            },
            sort_keys=True,
        )
    )
    if result["mode"] == "qualified":
        return 0 if result["fully_qualified"] else 1
    return 0 if result["offline_contract_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
