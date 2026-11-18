"""Explicit, pinned PDF text extraction through the existing isolation owner.

Downloaded PDFs never run in an unconfined host process. The operator selects an
exact pdftotext executable digest; ProcessSupervisor/Bubblewrap supplies private
filesystem/network/PID namespaces, seccomp and resource ceilings. Missing or
failed isolation yields unavailable, without an alternate parser or host retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.persistence.database import Database
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.workspace_sandbox import WorkspaceSandbox, load_workspace_sandbox

MAX_PDF_PAGES = 64
MAX_PDF_BYTES = 262144


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate extractor profile field")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class PdfExtractorProfile:
    executable: Path
    executable_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.executable, Path)
            or not self.executable.is_absolute()
            or not isinstance(self.executable_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.executable_sha256) is None
        ):
            raise InvalidInputError(
                "PDF extraction requires an absolute executable and exact SHA256"
            )

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "schema": "potbot-isolated-pdftotext/v1",
                "executableSha256": self.executable_sha256,
                "mode": "utf8_layout_page_spans",
                "maxInputBytes": MAX_PDF_BYTES,
                "maxOutputBytes": MAX_PDF_BYTES,
                "maxPages": MAX_PDF_PAGES,
                "timeoutMs": 5000,
                "memoryBytes": 256 * 1024 * 1024,
                "cpuSeconds": 4,
                "isolation": "bubblewrap_seccomp_no_network",
            }
        )

    @classmethod
    def read(cls, path: Path) -> PdfExtractorProfile:
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise ValueError
            with path.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                not isinstance(value, dict)
                or set(value) != {"schema", "executable", "executable_sha256"}
                or value["schema"] != "potbot-isolated-pdftotext/v1"
            ):
                raise ValueError
            return cls(Path(value["executable"]), value["executable_sha256"])
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise InvalidInputError(
                "The isolated PDF extractor profile is unreadable or invalid"
            ) from None

    def verify_executable(self) -> bytes:
        try:
            if (
                self.executable.resolve(strict=True) != self.executable
                or not self.executable.is_file()
                or not os.access(self.executable, os.X_OK)
            ):
                raise ValueError
            with self.executable.open("rb") as stream:
                raw = stream.read(64 * 1024 * 1024 + 1)
            if (
                len(raw) > 64 * 1024 * 1024
                or hashlib.sha256(raw).hexdigest() != self.executable_sha256
            ):
                raise ValueError
            return raw
        except (OSError, ValueError):
            raise CapabilityUnavailableError(
                "The configured PDF extractor executable is unavailable or changed"
            ) from None


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    status: str
    pages: tuple[str, ...]
    reason_codes: tuple[str, ...]
    execution: dict[str, Any] | None


class IsolatedPdfExtractor:
    def __init__(
        self,
        profile: PdfExtractorProfile,
        sandbox: WorkspaceSandbox,
        *,
        supervisor: ProcessSupervisor | None = None,
    ) -> None:
        if type(profile) is not PdfExtractorProfile or type(sandbox) is not WorkspaceSandbox:
            raise InvalidInputError("PDF extraction requires trusted profile and workspace owners")
        self.profile = profile
        self.sandbox = sandbox
        self.supervisor = supervisor or ProcessSupervisor()

    @property
    def fingerprint(self) -> str:
        return self.profile.fingerprint

    def extract(self, raw: bytes) -> PdfExtraction:
        if type(raw) is not bytes or len(raw) > MAX_PDF_BYTES:
            return PdfExtraction("over_limit", (), ("pdf_input_limit",), None)
        if not raw.startswith(b"%PDF-"):
            return PdfExtraction("invalid", (), ("pdf_header_invalid",), None)
        try:
            executable = self.profile.verify_executable()
            parent = self.sandbox.confine(self.sandbox.path("tmp"), area="tmp", must_exist=True)
            with tempfile.TemporaryDirectory(prefix="research-pdf-", dir=parent) as temporary:
                workspace = Path(temporary)
                # Execute the exact bytes already hashed, eliminating a path
                # replacement between source verification and process launch.
                program = workspace / "pdftotext"
                program.write_bytes(executable)
                program.chmod(0o500)
                request = ProcessRequest(
                    argv=(
                        str(program),
                        "-enc",
                        "UTF-8",
                        "-layout",
                        "-eol",
                        "unix",
                        "-f",
                        "1",
                        "-l",
                        str(MAX_PDF_PAGES + 1),
                        "-",
                        "-",
                    ),
                    cwd=workspace,
                    workspace_root=workspace,
                    sandbox_root=self.sandbox.root,
                    stdin=raw,
                    timeout_ms=5000,
                    max_output_bytes=MAX_PDF_BYTES,
                    read_only_roots=(program,),
                    memory_bytes=256 * 1024 * 1024,
                    cpu_seconds=4,
                    max_file_bytes=MAX_PDF_BYTES,
                    max_open_files=64,
                )
                result = self.supervisor.run(request)
        except (CapabilityUnavailableError, OSError, InvalidInputError):
            return PdfExtraction("extractor_unavailable", (), ("pdf_isolation_unavailable",), None)
        execution = {
            "profileSha256": self.fingerprint,
            "executableSha256": self.profile.executable_sha256,
            "isolationSha256": result.isolation_identity,
            "elapsedMs": result.elapsed_ms,
            "processesStopped": result.processes_stopped,
        }
        if result.stdout_truncated or result.stderr_truncated:
            return PdfExtraction("over_limit", (), ("pdf_output_limit",), execution)
        if result.outcome == "timeout":
            return PdfExtraction("over_limit", (), ("pdf_parser_deadline",), execution)
        if (
            result.outcome == "infrastructure_error"
            or not result.dispatched
            or result.isolation_identity is None
            or not result.processes_stopped
        ):
            return PdfExtraction(
                "extractor_unavailable", (), ("pdf_isolation_unavailable",), execution
            )
        if result.outcome != "completed" or result.exit_code != 0:
            return PdfExtraction("invalid", (), ("pdf_parser_failed",), execution)
        try:
            text = result.stdout.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            return PdfExtraction("invalid", (), ("pdf_output_not_utf8",), execution)
        pages = text.split("\f")
        if pages and not pages[-1]:
            pages.pop()
        if len(pages) > MAX_PDF_PAGES:
            return PdfExtraction("over_limit", (), ("pdf_page_limit",), execution)
        if not pages or not any(page.strip() for page in pages):
            return PdfExtraction(
                "unsupported", (), ("pdf_scanned_or_empty_ocr_unavailable",), execution
            )
        return PdfExtraction("extracted", tuple(pages), (), execution)


def configured_pdf_extractor(
    database: Database,
    context: WorkspaceContext,
) -> IsolatedPdfExtractor | None:
    """Read only the explicit operator profile; never discover an executable."""
    configured = os.environ.get("POTBOT_RESEARCH_PDF_PROFILE")
    if configured is None:
        return None
    if not configured or len(configured) > 4096:
        raise InvalidInputError("The isolated PDF extractor profile path is invalid")
    profile = PdfExtractorProfile.read(Path(configured))
    return IsolatedPdfExtractor(profile, load_workspace_sandbox(database, context))
