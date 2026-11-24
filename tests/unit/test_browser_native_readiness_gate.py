"""Validate native-window fields without weakening existing rendered evidence."""

import importlib.util
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "native_gate_fixtures", ROOT / "tests/unit/test_browser_rendered_gate.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
gate = helpers.GATE


def native_report(value):
    report = helpers.passing_report()
    props = report.find("testsuite/testcase/properties")
    ET.SubElement(props, "property", name="native_window", value=json.dumps(value))
    return helpers.encode(report)


def test_mapped_native_evidence_is_retained_with_original_browser_identity():
    value = {"map_state": "IsViewable", "title_sha256": "a" * 64}
    result = gate.validate_report(native_report(value), "playwright")
    assert result["native_window"] == value
    assert result["browser_binary"]["source"] == "engine"


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"map_state": "IsUnMapped", "title_sha256": "a" * 64},
        {"map_state": "IsViewable", "title_sha256": None},
        {"map_state": "IsViewable", "title_sha256": "not-a-hash"},
        {"map_state": "IsViewable", "title_sha256": "a" * 64, "extra": True},
    ],
)
def test_false_or_malformed_native_readiness_is_refused(value):
    with pytest.raises(ValueError, match="native"):
        gate.validate_report(native_report(value), "playwright")


def test_legacy_reports_remain_inspectable_but_cannot_satisfy_required_native_gate(tmp_path):
    report = helpers.source_fixture(tmp_path)
    assert gate.validate_report(report.read_bytes(), "playwright")["native_window"] is None
    with pytest.raises(ValueError, match="requires native"):
        gate.verify(tmp_path, report, "playwright", require_native_window=True)


def test_native_source_files_are_bound_to_the_rendered_evidence():
    assert "scripts/browser_display_readiness.py" in gate.SOURCE_FILES
    assert "tests/unit/test_browser_display_readiness.py" in gate.SOURCE_FILES
