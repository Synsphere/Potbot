from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from potbot.research.extraction import extract_document, verify_retained_pdf
from potbot.research.pdf_extraction import IsolatedPdfExtractor, PdfExtractorProfile
from potbot.workspace_sandbox import WorkspaceSandbox
from tests.unit.test_research_pdf_extraction import authored_pdf


@pytest.mark.sandbox
def test_real_isolated_pdf_extracts_authored_table_and_page_spans(tmp_path) -> None:
    selected = shutil.which("pdftotext")
    assert selected is not None, "This mandatory profile gate requires installed poppler-utils"
    executable = Path(selected).resolve(strict=True)
    profile = PdfExtractorProfile(executable, hashlib.sha256(executable.read_bytes()).hexdigest())
    extractor = IsolatedPdfExtractor(profile, WorkspaceSandbox.prepare(tmp_path / "workspace"))
    raw = authored_pdf()
    result = extract_document(raw, "application/pdf", pdf_extractor=extractor)
    assert result["status"] == "extracted", result["reasonCodes"]
    assert result["complete"] and result["execution"]["processesStopped"]
    assert len(result["execution"]["isolationSha256"]) == 64
    assert "Mode" in result["text"] and "safe" in result["text"] and "10" in result["text"]
    assert "café" in result["text"]
    assert [segment["page"] for segment in result["segments"]] == [1, 2]
    text = result["text"].encode("utf-8")
    last = result["segments"][1]
    assert b"The supported limit is 5." in text[last["startByte"] : last["endByte"]]
    assert verify_retained_pdf(raw, result, pdf_extractor=extractor) == result
