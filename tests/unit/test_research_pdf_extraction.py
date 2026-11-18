from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, StateMismatchError
from potbot.research.extraction import (
    extract_document,
    validate_retained_extraction,
    verify_retained_pdf,
)
from potbot.research.pdf_extraction import IsolatedPdfExtractor, PdfExtractorProfile
from potbot.tools.supervisor import ProcessResult
from potbot.workspace_sandbox import WorkspaceSandbox

RAW = b"%PDF-1.7\nsynthetic isolated-adapter input fixture\n%%EOF"


class RecordingSupervisor:
    """Trusted synthetic process outcome; this test does not qualify a live host."""

    def __init__(self, result=None):
        self.requests = []
        self.programs = []
        self.result = result or ProcessResult(
            "completed",
            0,
            "Page one café\n\fPage two 🔒\n\f".encode(),
            b"",
            12,
            False,
            False,
            True,
            "a" * 64,
            processes_stopped=True,
        )

    def run(self, request):
        self.requests.append(request)
        self.programs.append(Path(request.argv[0]).read_bytes())
        assert request.stdin == RAW and request.ipc_profile == "none"
        assert request.read_only_roots == (Path(request.argv[0]),)
        assert not request.environment and not request.writable_roots
        assert request.timeout_ms == 5000 and request.cpu_seconds == 4
        assert request.memory_bytes == 256 * 1024 * 1024 and request.max_output_bytes == 262144
        return self.result


def fixture_extractor(tmp_path, result=None):
    executable = tmp_path / "pinned-pdftotext"
    executable.write_bytes(b"synthetic-pinned-executable-fixture")
    executable.chmod(0o500)
    profile = PdfExtractorProfile(executable, hashlib.sha256(executable.read_bytes()).hexdigest())
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    supervisor = RecordingSupervisor(result)
    return IsolatedPdfExtractor(profile, sandbox, supervisor=supervisor), supervisor


def test_pdf_page_spans_use_actual_bounded_isolation_receipt_and_exact_executable_bytes(
    tmp_path,
) -> None:
    extractor, supervisor = fixture_extractor(tmp_path)
    result = extract_document(RAW, "application/pdf", pdf_extractor=extractor)
    assert result["complete"] and result["status"] == "extracted"
    assert [segment["page"] for segment in result["segments"]] == [1, 2]
    assert result["execution"] == {
        "profileSha256": extractor.fingerprint,
        "executableSha256": extractor.profile.executable_sha256,
        "isolationSha256": "a" * 64,
        "elapsedMs": 12,
        "processesStopped": True,
    }
    assert supervisor.programs == [extractor.profile.executable.read_bytes()]
    assert not Path(supervisor.requests[0].argv[0]).exists()
    validate_retained_extraction(result)


def test_pdf_artifact_audit_never_spends_another_process_or_rewrites_original_metrics(
    tmp_path,
) -> None:
    extractor, supervisor = fixture_extractor(tmp_path)
    retained = extract_document(RAW, "application/pdf", pdf_extractor=extractor)
    before = copy.deepcopy(retained)
    assert verify_retained_pdf(RAW, retained, pdf_extractor=extractor) == before
    assert retained == before and len(supervisor.requests) == 1
    with pytest.raises(CapabilityUnavailableError):
        verify_retained_pdf(RAW, retained)
    assert len(supervisor.requests) == 1


def test_changed_pdf_profile_or_source_fails_readonly_audit_without_parse(tmp_path) -> None:
    extractor, supervisor = fixture_extractor(tmp_path)
    retained = extract_document(RAW, "application/pdf", pdf_extractor=extractor)
    with pytest.raises(StateMismatchError):
        verify_retained_pdf(RAW + b"changed", retained, pdf_extractor=extractor)
    extractor.profile.executable.chmod(0o700)
    extractor.profile.executable.write_bytes(b"replaced executable")
    with pytest.raises(CapabilityUnavailableError):
        verify_retained_pdf(RAW, retained, pdf_extractor=extractor)
    assert len(supervisor.requests) == 1


@pytest.mark.parametrize(
    "outcome,expected,reason",
    [
        (
            {"outcome": "infrastructure_error", "dispatched": False, "isolation_identity": None},
            "extractor_unavailable",
            "pdf_isolation_unavailable",
        ),
        ({"outcome": "timeout"}, "over_limit", "pdf_parser_deadline"),
        ({"stdout_truncated": True}, "over_limit", "pdf_output_limit"),
        ({"outcome": "nonzero_exit", "exit_code": 2}, "invalid", "pdf_parser_failed"),
        ({"stdout": b"\xff"}, "invalid", "pdf_output_not_utf8"),
        ({"stdout": b"\f\f"}, "unsupported", "pdf_scanned_or_empty_ocr_unavailable"),
        ({"stdout": b"page\f" * 65}, "over_limit", "pdf_page_limit"),
        ({"processes_stopped": False}, "extractor_unavailable", "pdf_isolation_unavailable"),
    ],
)
def test_failed_pdf_parsers_never_fall_back_to_host_or_publish_partial_text(
    tmp_path, outcome, expected, reason
) -> None:
    actual = replace(RecordingSupervisor().result, **outcome)
    extractor, supervisor = fixture_extractor(tmp_path, actual)
    result = extract_document(RAW, "application/pdf", pdf_extractor=extractor)
    assert result["status"] == expected and result["reasonCodes"] == [reason]
    assert result["text"] == "" and not result["complete"] and len(supervisor.requests) == 1
    validate_retained_extraction(result)


def test_missing_or_changed_executable_and_invalid_input_never_reach_supervisor(tmp_path) -> None:
    extractor, supervisor = fixture_extractor(tmp_path)
    assert (
        extract_document(b"not a PDF", "application/pdf", pdf_extractor=extractor)["status"]
        == "invalid"
    )
    extractor.profile.executable.unlink()
    assert (
        extract_document(RAW, "application/pdf", pdf_extractor=extractor)["status"]
        == "extractor_unavailable"
    )
    assert supervisor.requests == []


def test_pdf_profile_digest_does_not_disclose_local_paths(tmp_path) -> None:
    extractor, _ = fixture_extractor(tmp_path)
    relocated = PdfExtractorProfile(
        Path("/other/operator/pdftotext"), extractor.profile.executable_sha256
    )
    assert relocated.fingerprint == extractor.profile.fingerprint


@pytest.mark.parametrize("field", ["schema", "executable", "executable_sha256"])
def test_pdf_profile_file_rejects_duplicate_pins_before_executable_or_process_use(
    tmp_path, field
) -> None:
    extractor, supervisor = fixture_extractor(tmp_path)
    selected = {
        "schema": "potbot-isolated-pdftotext/v1",
        "executable": str(extractor.profile.executable),
        "executable_sha256": extractor.profile.executable_sha256,
    }
    path = tmp_path / "pdf-profile.json"
    path.write_text(json.dumps(selected))
    assert PdfExtractorProfile.read(path) == extractor.profile
    # Ordinary json.loads would accept the final, valid pin and discard the
    # contradictory first occurrence in the operator-reviewed file.
    prefix = json.dumps({field: "contradictory-first-pin"})[:-1]
    path.write_text(prefix + "," + json.dumps(selected)[1:])
    with pytest.raises(InvalidInputError, match="unreadable or invalid"):
        PdfExtractorProfile.read(path)
    assert supervisor.requests == []


def authored_pdf() -> bytes:
    """A tiny authored two-page PDF; no downloaded or third-party document."""
    page_one = b"BT /F1 12 Tf 72 720 Td (Authored caf\xe9 table) Tj 0 -24 Td (Mode       Value) Tj 0 -20 Td (safe       10) Tj ET"
    page_two = b"BT /F1 12 Tf 72 720 Td (The supported limit is 5.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        b"<< /Length " + str(len(page_one)).encode() + b" >>\nstream\n" + page_one + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 7 0 R >>",
        b"<< /Length " + str(len(page_two)).encode() + b" >>\nstream\n" + page_two + b"\nendstream",
    ]
    raw = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(str(index).encode() + b" 0 obj\n" + body + b"\nendobj\n")
    xref = len(raw)
    raw.extend(b"xref\n0 8\n0000000000 65535 f \n")
    for offset in offsets:
        raw.extend(f"{offset:010d} 00000 n \n".encode())
    raw.extend(f"trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(raw)
