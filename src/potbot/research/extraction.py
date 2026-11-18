"""Deterministic bounded source extraction with exact UTF-8 citation locations.

HTML is parsed as data; scripts, CSS, links, embedded documents and archives are
never executed or fetched. JSON values retain JSON Pointer locations. Optional
PDF text comes only from the explicit isolated extractor. Extraction proves the
retained text and location mapping, not the truth or licence of a source.
"""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.research.pdf_extraction import IsolatedPdfExtractor
from potbot.research.providers import _published, _unique_object

MAX_EXTRACTED_BYTES = 262144
MAX_EXTRACTION_ENVELOPE_BYTES = 1000000
MAX_SEGMENTS = 4096
MAX_LOCATOR_BYTES = 2048
_KINDS = {"text", "html_element", "json_pointer", "pdf_page"}
_STATUSES = {"extracted", "unsupported", "invalid", "over_limit", "extractor_unavailable"}
_SKIP = {"script", "style", "template", "noscript", "svg", "canvas", "iframe", "object"}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BLOCK = {
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}


class _ExtractionLimit(ValueError):
    pass


def _normal_text(value: str) -> str:
    result = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(ord(char) < 32 and char not in "\n\t" for char in result):
        raise ValueError("unsupported source text controls")
    result.encode("utf-8")
    return result


class _Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.segments: list[dict[str, Any]] = []
        self.size = 0

    def add(self, value: str, kind: str, locator: str, page: int | None = None) -> None:
        value = _normal_text(value)
        if not value:
            return
        size = len(value.encode("utf-8"))
        separator = 1 if self.parts else 0
        if (
            self.size + separator + size > MAX_EXTRACTED_BYTES
            or len(self.segments) >= MAX_SEGMENTS
            or len(locator.encode("utf-8")) > MAX_LOCATOR_BYTES
        ):
            raise _ExtractionLimit
        self.size += separator
        self.parts.append(value)
        self.segments.append(
            {
                "startByte": self.size,
                "endByte": self.size + size,
                "kind": kind,
                "locator": locator,
                "page": page,
            }
        )
        self.size += size

    @property
    def text(self) -> str:
        return "\n".join(self.parts)


class _HtmlText(HTMLParser):
    def __init__(self, builder: _Builder) -> None:
        super().__init__(convert_charrefs=True)
        self.builder = builder
        self.stack: list[tuple[str, int]] = []
        self.counts: list[dict[str, int]] = [{}]
        self.buffer: list[str] = []
        self.locator = "/"
        self.preserve = False
        self.title: list[str] = []
        self.published: str | None = None
        self.elements = 0

    def _path(self) -> str:
        return "".join(f"/{tag}[{index}]" for tag, index in self.stack) or "/"

    def _flush(self) -> None:
        text = "".join(self.buffer)
        self.buffer = []
        if not self.preserve:
            text = " ".join(text.split())
        self.builder.add(text, "html_element", self.locator)
        self.preserve = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements += 1
        if self.elements > 16384 or len(self.stack) >= 64:
            raise _ExtractionLimit
        if tag in _BLOCK and not any(
            entry[0] in _SKIP or entry[0] == "head" for entry in self.stack
        ):
            self._flush()
        previous = self.counts[-1].get(tag, 0) + 1
        self.counts[-1][tag] = previous
        if tag not in _VOID:
            self.stack.append((tag, previous))
            self.counts.append({})
        if tag == "meta":
            attributes = dict(attrs)
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            ).lower()
            if key in {"article:published_time", "datepublished", "date"}:
                self.published = self.published or _published(attributes.get("content"))
        if tag == "br" and not any(entry[0] in _SKIP for entry in self.stack):
            self.buffer.append("\n" if self.preserve else " ")
        if tag in _BLOCK:
            self.locator = self._path()
            self.preserve = tag == "pre" or any(entry[0] == "pre" for entry in self.stack)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK and not any(
            entry[0] in _SKIP or entry[0] == "head" for entry in self.stack
        ):
            self._flush()
        indices = [index for index, (entry, _) in enumerate(self.stack) if entry == tag]
        if indices:
            self.stack = self.stack[: indices[-1]]
            self.counts = self.counts[: indices[-1] + 1]
        self.locator = self._path()
        self.preserve = any(entry[0] == "pre" for entry in self.stack)

    def handle_data(self, data: str) -> None:
        tags = [entry[0] for entry in self.stack]
        if "title" in tags:
            self.title.append(data)
            return
        if any(tag in _SKIP or tag == "head" for tag in tags):
            return
        if not self.buffer:
            self.locator = self._path()
        self.buffer.append(data)

    def finish(self) -> dict[str, Any]:
        self.close()
        self._flush()
        title = _normal_text(" ".join("".join(self.title).split()))
        if len(title.encode("utf-8")) > 4096:
            raise _ExtractionLimit
        return {
            "title": title or None,
            "publishedAt": self.published,
            "publicationProvenance": "source_reported" if self.published else None,
        }


def _json_text(value: Any, builder: _Builder) -> dict[str, Any]:
    nodes = 0

    def visit(item: Any, pointer: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 4096 or depth > 64:
            raise _ExtractionLimit
        if isinstance(item, dict):
            for key in sorted(item):
                visit(
                    item[key], pointer + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1
                )
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, pointer + "/" + str(index), depth + 1)
        else:
            scalar = json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            builder.add((pointer or "/") + " = " + scalar, "json_pointer", pointer)

    visit(value, "", 0)
    title = value.get("title") if isinstance(value, dict) else None
    if not isinstance(title, str) or len(title.encode("utf-8")) > 4096:
        title = None
    elif title is not None:
        title = _normal_text(title)
    published = _published(value.get("datePublished")) if isinstance(value, dict) else None
    return {
        "title": title,
        "publishedAt": published,
        "publicationProvenance": "source_reported" if published else None,
    }


def extractor_fingerprint(
    media_type: str, pdf_extractor: IsolatedPdfExtractor | None = None
) -> str:
    module_hashes = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ("extraction.py", "providers.py", "pdf_extraction.py")
    }
    return content_hash(
        {
            "schema": "potbot-research-extractor/v1",
            "mediaType": media_type,
            "modules": module_hashes,
            "pdfProfileSha256": pdf_extractor.fingerprint
            if media_type == "application/pdf" and pdf_extractor is not None
            else None,
        }
    )


def extract_document(
    raw: bytes,
    media_type: str,
    *,
    pdf_extractor: IsolatedPdfExtractor | None = None,
) -> dict[str, Any]:
    if type(raw) is not bytes or not isinstance(media_type, str) or len(media_type) > 256:
        raise InvalidInputError("Source extraction requires bounded typed bytes and media type")
    mime = media_type.split(";", 1)[0].strip().lower()
    identity = {
        "text/html": "potbot.html_text/v1",
        "application/json": "potbot.json_pointer/v1",
        "text/plain": "potbot.utf8_text/v1",
        "application/pdf": "potbot.isolated_pdftotext/v1",
    }.get(mime, "potbot.unsupported_source/v1")
    if mime.endswith("+json"):
        identity = "potbot.json_pointer/v1"
    result: dict[str, Any] = {
        "schema": "potbot-extracted-source/v1",
        "status": "unsupported",
        "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "mediaType": media_type.strip().lower(),
        "extractorId": identity,
        "extractorSha256": extractor_fingerprint(mime, pdf_extractor),
        "text": "",
        "textSha256": hashlib.sha256(b"").hexdigest(),
        "segments": [],
        "metadata": {"title": None, "publishedAt": None, "publicationProvenance": None},
        "complete": False,
        "license": "unknown",
        "reasonCodes": [],
        "execution": None,
    }
    if len(raw) > MAX_EXTRACTED_BYTES:
        return {**result, "status": "over_limit", "reasonCodes": ["source_byte_limit"]}
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b")):
        return {**result, "reasonCodes": ["archive_or_compressed_source_unsupported"]}
    builder = _Builder()
    try:
        if mime == "application/pdf":
            if pdf_extractor is None:
                return {
                    **result,
                    "status": "extractor_unavailable",
                    "reasonCodes": ["isolated_pdf_extractor_unconfigured"],
                }
            parsed = pdf_extractor.extract(raw)
            result["execution"] = parsed.execution
            if parsed.status != "extracted":
                return {**result, "status": parsed.status, "reasonCodes": list(parsed.reason_codes)}
            for page, text in enumerate(parsed.pages, 1):
                builder.add(text, "pdf_page", f"page:{page}", page)
        elif mime in {"text/html", "text/plain", "application/json"} or mime.endswith("+json"):
            # This supported profile is explicitly UTF-8; no silent replacement
            # decoding can change a citation or conceal a malformed source.
            if ";" in media_type:
                for parameter in media_type.split(";")[1:]:
                    key, _, value = parameter.strip().partition("=")
                    if key.lower() == "charset" and value.strip("\"' ").lower() not in {
                        "utf-8",
                        "utf8",
                        "us-ascii",
                    }:
                        return {**result, "reasonCodes": ["source_encoding_unsupported"]}
            decoded = raw.decode("utf-8-sig")
            if mime == "text/html":
                parser = _HtmlText(builder)
                parser.feed(decoded)
                result["metadata"] = parser.finish()
            elif mime == "text/plain":
                builder.add(decoded, "text", "document")
            else:
                value = json.loads(
                    decoded,
                    object_pairs_hook=_unique_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
                )
                result["metadata"] = _json_text(value, builder)
        else:
            return {**result, "reasonCodes": ["source_media_type_unsupported"]}
    except _ExtractionLimit:
        return {**result, "status": "over_limit", "reasonCodes": ["extraction_resource_limit"]}
    except CapabilityUnavailableError:
        return {
            **result,
            "status": "extractor_unavailable",
            "reasonCodes": ["source_extractor_unavailable"],
        }
    except (DomainError, ValueError, TypeError, UnicodeError, RecursionError):
        return {**result, "status": "invalid", "reasonCodes": ["source_extraction_invalid"]}
    result.update(
        {
            "status": "extracted",
            "text": builder.text,
            "textSha256": hashlib.sha256(builder.text.encode("utf-8")).hexdigest(),
            "segments": builder.segments,
            "complete": True,
            "reasonCodes": [] if builder.text else ["source_has_no_authored_text"],
        }
    )
    if len(canonical_json(result).encode("utf-8")) > MAX_EXTRACTION_ENVELOPE_BYTES:
        result.update(
            status="over_limit",
            text="",
            textSha256=hashlib.sha256(b"").hexdigest(),
            segments=[],
            complete=False,
            reasonCodes=["extraction_envelope_limit"],
        )
    return result


def verify_extraction(
    raw: bytes,
    retained: object,
    *,
    pdf_extractor: IsolatedPdfExtractor | None = None,
) -> dict[str, Any]:
    """Audit already authenticated artifacts without starting any subprocess.

    Pure parsers are recomputed. For PDF, the caller must first authenticate the
    extraction's native completion/Cog/output receipt, as documented by
    ``verify_retained_pdf``. This function never spends an unreserved parse call.
    """
    value = validate_retained_extraction(retained)
    if value["mediaType"].split(";", 1)[0].strip().lower() == "application/pdf":
        return verify_retained_pdf(raw, value, pdf_extractor=pdf_extractor)
    observed = extract_document(raw, value["mediaType"])
    if value != observed:
        raise StateMismatchError("Retained source text or locations do not match its bytes")
    return observed


def _checked_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def validate_retained_extraction(value: object) -> dict[str, Any]:
    """Validate a complete bounded extraction document without accepting authority."""
    fields = {
        "schema",
        "status",
        "sourceSha256",
        "mediaType",
        "extractorId",
        "extractorSha256",
        "text",
        "textSha256",
        "segments",
        "metadata",
        "complete",
        "license",
        "reasonCodes",
        "execution",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema"] != "potbot-extracted-source/v1"
        or not isinstance(value["status"], str)
        or value["status"] not in _STATUSES
        or not isinstance(value["mediaType"], str)
        or not 1 <= len(value["mediaType"]) <= 256
        or any(not 32 <= ord(char) <= 126 for char in value["mediaType"])
        or not isinstance(value["extractorId"], str)
        or not 1 <= len(value["extractorId"]) <= 128
        or not all(
            _checked_sha256(value[key]) for key in ("sourceSha256", "extractorSha256", "textSha256")
        )
        or not isinstance(value["text"], str)
        or len(value["text"]) > MAX_EXTRACTED_BYTES
        or type(value["complete"]) is not bool
        or value["complete"] != (value["status"] == "extracted")
        or value["license"] != "unknown"
        or not isinstance(value["segments"], list)
        or len(value["segments"]) > MAX_SEGMENTS
    ):
        raise StateMismatchError("Retained source extraction schema is invalid")
    try:
        text = _normal_text(value["text"])
        raw_text = text.encode("utf-8")
    except (UnicodeError, ValueError):
        raise StateMismatchError("Retained source text is not canonical UTF-8") from None
    if (
        text != value["text"]
        or len(raw_text) > MAX_EXTRACTED_BYTES
        or hashlib.sha256(raw_text).hexdigest() != value["textSha256"]
    ):
        raise StateMismatchError("Retained source text hash or bound is invalid")
    if not value["complete"] and (text or value["segments"]):
        raise StateMismatchError("Incomplete extraction cannot publish citable text")
    mime = value["mediaType"].split(";", 1)[0].strip().lower()
    expected_kind = {
        "text/plain": "text",
        "text/html": "html_element",
        "application/json": "json_pointer",
        "application/pdf": "pdf_page",
    }.get(mime)
    if mime.endswith("+json"):
        expected_kind = "json_pointer"
    end, last_page = 0, 0
    for index, segment in enumerate(value["segments"]):
        if (
            not isinstance(segment, dict)
            or set(segment) != {"startByte", "endByte", "kind", "locator", "page"}
            or type(segment["startByte"]) is not int
            or type(segment["endByte"]) is not int
            or segment["startByte"] != end + (1 if index else 0)
            or not segment["startByte"] < segment["endByte"] <= len(raw_text)
            or not isinstance(segment["kind"], str)
            or segment["kind"] not in _KINDS
            or segment["kind"] != expected_kind
            or not isinstance(segment["locator"], str)
            or len(segment["locator"]) > MAX_LOCATOR_BYTES
        ):
            raise StateMismatchError("Retained source location is invalid")
        try:
            location = segment["locator"].encode("utf-8")
            raw_text[segment["startByte"] : segment["endByte"]].decode("utf-8")
        except UnicodeError:
            raise StateMismatchError("Source location splits a UTF-8 character") from None
        if len(location) > MAX_LOCATOR_BYTES or (
            index and raw_text[end : segment["startByte"]] != b"\n"
        ):
            raise StateMismatchError("Retained source location mapping is incomplete")
        if segment["kind"] == "pdf_page":
            page = segment["page"]
            if (
                type(page) is not int
                or not last_page < page <= 64
                or segment["locator"] != f"page:{page}"
            ):
                raise StateMismatchError("Retained PDF page location is invalid")
            last_page = page
        elif segment["page"] is not None:
            raise StateMismatchError("Only PDF locations can claim page numbers")
        end = segment["endByte"]
    if end != len(raw_text):
        raise StateMismatchError("Retained source text lacks complete location mapping")
    metadata = value["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {
        "title",
        "publishedAt",
        "publicationProvenance",
    }:
        raise StateMismatchError("Retained source metadata is invalid")
    title = metadata["title"]
    if title is not None:
        try:
            if (
                not isinstance(title, str)
                or len(title) > 4096
                or len(title.encode("utf-8")) > 4096
                or _normal_text(title) != title
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            raise StateMismatchError("Retained source title is invalid") from None
    published = metadata["publishedAt"]
    if (published is None and metadata["publicationProvenance"] is not None) or (
        published is not None
        and (
            _published(published) != published
            or metadata["publicationProvenance"] != "source_reported"
        )
    ):
        raise StateMismatchError("Retained publication metadata invents an unsupported instant")
    codes = value["reasonCodes"]
    if (
        not isinstance(codes, list)
        or len(codes) > 16
        or any(
            not isinstance(code, str)
            or not 1 <= len(code) <= 96
            or code[0] not in "abcdefghijklmnopqrstuvwxyz"
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in code)
            for code in codes
        )
    ):
        raise StateMismatchError("Retained extraction reason codes are invalid")
    pdf = value["mediaType"].split(";", 1)[0].strip().lower() == "application/pdf"
    execution = value["execution"]
    if not pdf and execution is not None:
        raise StateMismatchError("Pure source extraction cannot claim process execution")
    if pdf and value["complete"] and execution is None:
        raise StateMismatchError("Citable PDF text requires an actual isolated execution receipt")
    if execution is not None:
        if (
            not isinstance(execution, dict)
            or set(execution)
            != {
                "profileSha256",
                "executableSha256",
                "isolationSha256",
                "elapsedMs",
                "processesStopped",
            }
            or type(execution.get("elapsedMs")) is not int
            or not 0 <= execution["elapsedMs"] <= 3600000
            or type(execution.get("processesStopped")) is not bool
            or (value["complete"] and not execution["processesStopped"])
        ):
            raise StateMismatchError("Retained PDF execution receipt is invalid")
        for key in ("profileSha256", "executableSha256", "isolationSha256"):
            digest = execution.get(key)
            if digest is None and key == "isolationSha256" and not value["complete"]:
                continue
            if not _checked_sha256(digest):
                raise StateMismatchError("Retained PDF execution receipt digest is invalid")
    if len(canonical_json(value).encode("utf-8")) > MAX_EXTRACTION_ENVELOPE_BYTES:
        raise StateMismatchError("Retained source extraction envelope exceeds its bound")
    return value


def verify_retained_pdf(
    raw: bytes,
    retained: object,
    *,
    pdf_extractor: IsolatedPdfExtractor | None = None,
) -> dict[str, Any]:
    """Read/hash-only validation of an already authenticated PDF transformation.

    Caller MUST first verify the native completion/Cog/output MAC for these
    exact raw and extraction artifact hashes. Schema validation is not proof
    that a caller supplied transformation ran. This helper never invokes a
    parser or subprocess and never treats an unsigned document as evidence.
    """
    value = validate_retained_extraction(retained)
    if (
        type(raw) is not bytes
        or len(raw) > MAX_EXTRACTED_BYTES
        or hashlib.sha256(raw).hexdigest() != value["sourceSha256"]
        or value["mediaType"].split(";", 1)[0].strip().lower() != "application/pdf"
    ):
        raise StateMismatchError("Retained PDF source bytes do not match its transformation")
    if value["complete"] and pdf_extractor is None:
        raise CapabilityUnavailableError("The retained PDF extractor profile is unavailable")
    if value["extractorSha256"] != extractor_fingerprint("application/pdf", pdf_extractor):
        raise StateMismatchError("The retained PDF extractor implementation or profile changed")
    if value["complete"]:
        assert pdf_extractor is not None
        pdf_extractor.profile.verify_executable()
        execution = value["execution"]
        if (
            execution["profileSha256"] != pdf_extractor.fingerprint
            or execution["executableSha256"] != pdf_extractor.profile.executable_sha256
        ):
            raise StateMismatchError("The retained PDF executable/profile digest changed")
    return value
