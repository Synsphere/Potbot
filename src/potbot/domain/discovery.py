"""Bounded, immutable registry observations. None of these records grants authority.

Registry assertions, operator admission and measured execution eligibility have
different owners. This module describes only the first of those boundaries.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import parse_type_ref

ADAPTERS = frozenset({"curated", "clawhub", "mcp_registry", "skills_sh"})
KINDS = frozenset({"skill", "mcp_server", "tool", "workflow", "plugin"})
STATUSES = frozenset({"active", "deprecated", "deleted", "unresolved", "unsupported"})
CAPABILITY_CLASSES = frozenset(
    {
        "repository_inspection",
        "code_search",
        "file_inspection",
        "text_search",
        "image_inspection",
        "document_analysis",
        "spreadsheet_analysis",
        "browser_inspection",
        "web_retrieval",
        "structured_api_read",
        "database_query",
        "satisfiability",
        "python_debugging",
        "remote_debugging",
        "hardware_debugging",
        "android_inspection",
        "apple_inspection",
        "profiling",
        "skill_authoring",
        "workflow_guidance",
    }
)
EFFECTS = frozenset({"read_only", "idempotent_write", "non_idempotent_write", "destructive"})
PLATFORMS = frozenset({"linux", "darwin", "windows", "android", "ios", "remote"})
REQUIREMENT_KINDS = frozenset(
    {
        "binary",
        "any_binary",
        "environment",
        "configuration",
        "operating_system",
        "tool",
        "compatibility",
    }
)
MAX_CANDIDATE_BYTES = 128 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 100
MAX_REQUIREMENTS = 128
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z", re.ASCII)
_GH_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", re.ASCII)
_CANDIDATE_FIELDS = frozenset(
    {
        "schema",
        "adapter",
        "canonicalId",
        "kind",
        "version",
        "name",
        "description",
        "publisher",
        "sourceUrl",
        "status",
        "capabilities",
        "requirements",
        "distribution",
        "manifest",
        "manifestSha256",
        "sourceSha256",
        "unsupported",
    }
)


def text_value(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InvalidInputError(f"{label} must be bounded plain text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InvalidInputError(f"{label} must be UTF-8 text") from None
    return value


def digest_value(value: Any, label: str = "Digest") -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError(f"{label} must be lowercase SHA-256")
    return value


def bounded_json(value: Any, *, maximum: int = MAX_CANDIDATE_BYTES) -> Any:
    """Reject ambiguous/oversized JSON and normalize integral floats for JSONB."""
    remaining = max(8192, maximum // 16)

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 20:
            raise InvalidInputError("Registry metadata exceeds its structural allowance")
        if item is None or type(item) is bool:
            return item
        if type(item) in (int, float):
            if abs(item) > 2**53 - 1 or not math.isfinite(item):
                raise InvalidInputError("Registry numbers must be finite JSON-safe values")
            return int(item) if int(item) == item else item
        if isinstance(item, str):
            if "\x00" in item:
                raise InvalidInputError("Registry JSON may not contain NUL")
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise InvalidInputError("Registry JSON must be UTF-8") from None
            return item
        if isinstance(item, (tuple, list)):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            return {visit(key, depth + 1): visit(child, depth + 1) for key, child in item.items()}
        raise InvalidInputError("Registry metadata must contain only JSON values")

    result = visit(value, 0)
    if len(canonical_json(result).encode("utf-8")) > maximum:
        raise InvalidInputError("Registry metadata exceeds its byte allowance")
    return result


def parse_json(raw: bytes, *, maximum: int = MAX_PAGE_BYTES) -> Any:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= maximum:
        raise InvalidInputError("Registry JSON exceeds its byte allowance")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise InvalidInputError("Registry JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
        return bounded_json(value, maximum=maximum)
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise InvalidInputError("Registry response is not bounded unambiguous JSON") from None


def github_repository(value: Any) -> str:
    value = text_value(value, "GitHub repository", 300)
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise InvalidInputError("Invalid public GitHub repository") from None
    parts = parsed.path.removeprefix("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or any(not _GH_PART.fullmatch(part) for part in parts)
        or parts[1].endswith(".git")
        or value != "https://github.com/" + "/".join(parts)
    ):
        raise InvalidInputError("An exact canonical public GitHub repository is required")
    return str(value)


def source_url(value: Any, *, local: bool = False) -> str:
    value = text_value(value, "Source URL", 2048)
    if any(char.isspace() for char in value) or "\\" in value:
        raise InvalidInputError("Source URL must not contain whitespace or backslashes")
    try:
        parsed = urlsplit(value)
        if local and parsed.scheme == "potbot" and parsed.netloc == "curated":
            if not parsed.query and not parsed.fragment and parsed.path.startswith("/"):
                return str(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and not parsed.fragment
        ):
            return str(value)
    except ValueError:
        pass
    raise InvalidInputError("Source metadata requires an HTTPS URL without credentials")


def _strings(value: Any, label: str, *, maximum: int = 64, size: int = 256) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise InvalidInputError(f"{label} must be a bounded list")
    result = [text_value(item, label, size) for item in value]
    if len(result) != len(set(result)):
        raise InvalidInputError(f"{label} must not contain duplicates")
    return result


def validate_requirements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_REQUIREMENTS:
        raise InvalidInputError("Registry requirements must be bounded")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) not in (
            {"kind", "name"},
            {"kind", "name", "group"},
        ):
            raise InvalidInputError("Registry requirement fields are invalid")
        if not isinstance(item["kind"], str) or item["kind"] not in REQUIREMENT_KINDS:
            raise InvalidInputError("Registry requirement kind is unsupported")
        text_value(item["name"], "Requirement name", 512)
        if "group" in item:
            if (
                item["kind"] not in {"any_binary", "operating_system"}
                or item["group"] != item["kind"]
            ):
                raise InvalidInputError(
                    "Only canonical binary and operating-system OR groups are supported"
                )
        identity = (item["kind"], item["name"])
        if identity in seen:
            raise InvalidInputError("Registry requirements must not contain duplicate clauses")
        seen.add(identity)
        result.append(dict(item))
    if len(canonical_json(result).encode("utf-8")) > 65536:
        raise InvalidInputError("Registry requirements exceed their byte allowance")
    return result


def _inventory(value: Any) -> None:
    from potbot.context.skill_packages import validate_resource_path

    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise InvalidInputError("An exact bounded skill file inventory is required")
    paths = set()
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise InvalidInputError("Skill file inventory fields are invalid")
        path = validate_resource_path(item["path"])
        if path.casefold() in paths:
            raise InvalidInputError("Skill file inventory contains colliding paths")
        paths.add(path.casefold())
        if type(item["size"]) is not int or not 0 <= item["size"] <= 1024 * 1024:
            raise InvalidInputError("Skill file inventory size exceeds its allowance")
        total += item["size"]
        digest_value(item["sha256"])
    if not any(item["path"] == "SKILL.md" for item in value) or total > 8 * 1024 * 1024:
        raise InvalidInputError("Skill inventory lacks SKILL.md or exceeds its allowance")


def _distribution(value: Any, adapter: str, kind: str, version: Any) -> None:
    if not isinstance(value, dict):
        raise InvalidInputError("Registry distribution must be an object")
    distribution = value.get("kind")
    if distribution == "inspect_only":
        if set(value) != {"kind"}:
            raise InvalidInputError("Inspect-only distribution has no executable fields")
        return
    if version is None:
        raise InvalidInputError("Acquisition requires an exact resolved version")
    if distribution in ("github_skill", "tool_recipe"):
        required = (
            {"kind", "repository", "commit", "path"}
            if distribution == "github_skill"
            else {"kind", "repository", "commit", "recipeId"}
        )
        if set(value) != required or adapter != "curated":
            raise InvalidInputError(
                "Only curated exact Git or existing-recipe targets are supported"
            )
        github_repository(value["repository"])
        if not isinstance(value["commit"], str) or not _COMMIT.fullmatch(value["commit"]):
            raise InvalidInputError("Git distributions require an exact 40-character commit")
        if distribution == "github_skill":
            from potbot.context.skill_packages import validate_resource_path

            if kind != "skill" or not isinstance(value["path"], str):
                raise InvalidInputError("Git skill distribution fields are invalid")
            if value["path"]:
                validate_resource_path(value["path"])
        else:
            try:
                if str(UUID(value["recipeId"])) != value["recipeId"] or kind != "tool":
                    raise ValueError
            except (ValueError, AttributeError, TypeError):
                raise InvalidInputError(
                    "Tool distribution requires an existing recipe UUID"
                ) from None
        return
    if distribution == "clawhub_skill":
        if (
            adapter != "clawhub"
            or kind != "skill"
            or set(value) != {"kind", "owner", "slug", "version", "files"}
        ):
            raise InvalidInputError("ClawHub distribution identity is invalid")
        for key in ("owner", "slug"):
            if not isinstance(value[key], str) or not _IDENTIFIER.fullmatch(value[key]):
                raise InvalidInputError("ClawHub distribution requires owner-qualified identity")
        if value["version"] != version:
            raise InvalidInputError("ClawHub distribution version differs from the candidate")
        _inventory(value["files"])
        return
    if distribution == "skills_sh_snapshot":
        if (
            adapter != "skills_sh"
            or kind != "skill"
            or set(value) != {"kind", "id", "files", "responseSha256"}
        ):
            raise InvalidInputError("skills.sh snapshot distribution is invalid")
        text_value(value["id"], "skills.sh identity", 320)
        digest_value(value["responseSha256"])
        if version != "snapshot:" + value["responseSha256"]:
            raise InvalidInputError("skills.sh snapshot has no exact observed content identity")
        _inventory(value["files"])
        return
    raise InvalidInputError("Unsupported registry distribution")


@dataclass(frozen=True, slots=True)
class RegistryCandidate:
    """Immutable canonical representation; nested accessors return detached copies."""

    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.payload_json, str):
            raise InvalidInputError("Candidate must be canonical JSON")
        try:
            encoded = self.payload_json.encode("utf-8")
        except UnicodeError:
            raise InvalidInputError("Candidate JSON must be valid UTF-8") from None
        value = parse_json(encoded, maximum=MAX_CANDIDATE_BYTES)
        if not isinstance(value, dict) or set(value) != _CANDIDATE_FIELDS:
            raise InvalidInputError("Registry candidate fields do not match v1")
        if canonical_json(value) != self.payload_json:
            raise InvalidInputError("Candidate must use canonical JSON")
        if (
            value["schema"] != "potbot-registry-candidate/v1"
            or not isinstance(value["adapter"], str)
            or value["adapter"] not in ADAPTERS
        ):
            raise InvalidInputError("Registry candidate schema or adapter is unsupported")
        if (
            not isinstance(value["kind"], str)
            or not isinstance(value["status"], str)
            or value["kind"] not in KINDS
            or value["status"] not in STATUSES
        ):
            raise InvalidInputError("Registry candidate kind or status is unsupported")
        text_value(value["canonicalId"], "Registry identity", 320)
        text_value(value["name"], "Registry name", 128)
        text_value(value["description"], "Registry description", 4096, empty=True)
        if value["version"] is not None:
            text_value(value["version"], "Registry version", 128)
            if value["version"].lower() in {"latest", "main", "master", "head", "*"}:
                raise InvalidInputError("Registry version must not be a moving selector")
        publisher = value["publisher"]
        if not isinstance(publisher, dict) or set(publisher) != {"id", "evidence"}:
            raise InvalidInputError("Registry publisher evidence is invalid")
        text_value(publisher["id"], "Publisher identity", 320)
        if not isinstance(publisher["evidence"], str) or publisher["evidence"] not in {
            "registry_claim",
            "source_owner_claim",
            "operator_claim",
        }:
            raise InvalidInputError("Registry publisher evidence grants no verification authority")
        source_url(value["sourceUrl"], local=value["adapter"] == "curated")
        capability = value["capabilities"]
        if not isinstance(capability, dict) or set(capability) != {
            "classes",
            "inputTypes",
            "outputTypes",
            "effects",
            "scopes",
            "platforms",
        }:
            raise InvalidInputError("Registry capability fields are invalid")
        for key, permitted in (
            ("classes", CAPABILITY_CLASSES),
            ("effects", EFFECTS),
            ("platforms", PLATFORMS),
        ):
            if not set(_strings(capability[key], key)) <= permitted:
                raise InvalidInputError(
                    "Registry capability contains an unpublished classification"
                )
        for key in ("inputTypes", "outputTypes"):
            for reference in _strings(capability[key], key):
                parse_type_ref(reference)
        for scope in _strings(capability["scopes"], "Declared scopes", size=128):
            if not _IDENTIFIER.fullmatch(scope):
                raise InvalidInputError("Declared scopes must be bounded identifiers")
        validate_requirements(value["requirements"])
        _strings(value["unsupported"], "Unsupported reasons", maximum=128, size=256)
        if not isinstance(value["manifest"], dict):
            raise InvalidInputError("Registry manifest must be an object")
        if content_hash(value["manifest"]) != digest_value(value["manifestSha256"]):
            raise InvalidInputError("Registry manifest digest does not match")
        digest_value(value["sourceSha256"])
        _distribution(value["distribution"], value["adapter"], value["kind"], value["version"])
        target = value["distribution"]
        if target["kind"] == "clawhub_skill" and (
            value["canonicalId"] != target["owner"] + "/" + target["slug"]
            or publisher["id"] != target["owner"]
        ):
            raise InvalidInputError("ClawHub candidate and distribution publisher differ")
        if target["kind"] == "skills_sh_snapshot" and (
            value["canonicalId"] != target["id"]
            or publisher["id"] != target["id"].rsplit("/", 1)[0]
        ):
            raise InvalidInputError("skills.sh candidate and snapshot identity differ")

    @classmethod
    def from_dict(cls, raw: Any) -> Self:
        return cls(canonical_json(bounded_json(raw)))

    def to_dict(self) -> dict[str, Any]:
        return dict(json.loads(self.payload_json))

    @property
    def sha256(self) -> str:
        return content_hash(self.to_dict())

    def _field(self, key: str) -> Any:
        return self.to_dict()[key]

    @property
    def adapter(self) -> str:
        return str(self._field("adapter"))

    @property
    def canonical_id(self) -> str:
        return str(self._field("canonicalId"))

    @property
    def kind(self) -> str:
        return str(self._field("kind"))

    @property
    def version(self) -> str | None:
        value = self._field("version")
        return str(value) if value is not None else None

    @property
    def name(self) -> str:
        return str(self._field("name"))

    @property
    def description(self) -> str:
        return str(self._field("description"))

    @property
    def status(self) -> str:
        return str(self._field("status"))

    @property
    def publisher(self) -> dict[str, Any]:
        return dict(self._field("publisher"))

    @property
    def capabilities(self) -> dict[str, Any]:
        return dict(self._field("capabilities"))

    @property
    def requirements(self) -> list[dict[str, Any]]:
        return list(self._field("requirements"))

    @property
    def distribution(self) -> dict[str, Any]:
        return dict(self._field("distribution"))

    @property
    def manifest(self) -> dict[str, Any]:
        return dict(self._field("manifest"))

    @property
    def manifest_sha256(self) -> str:
        return str(self._field("manifestSha256"))

    @property
    def source_sha256(self) -> str:
        return str(self._field("sourceSha256"))

    @property
    def unsupported(self) -> tuple[str, ...]:
        return tuple(self._field("unsupported"))


@dataclass(frozen=True, slots=True)
class RegistryPage:
    adapter: str
    query: str
    candidates: tuple[RegistryCandidate, ...]
    next_cursor: str | None
    fetched_at: str
    expires_at: str
    etag: str | None
    source_sha256: str
    status: str = "ok"
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, str) or self.adapter not in ADAPTERS:
            raise InvalidInputError("Registry adapter is unsupported")
        text_value(self.query, "Registry query", 256, empty=True)
        if not isinstance(self.candidates, tuple) or len(self.candidates) > MAX_CANDIDATES:
            raise InvalidInputError("Registry page candidates exceed allowance")
        if any(
            not isinstance(c, RegistryCandidate) or c.adapter != self.adapter
            for c in self.candidates
        ):
            raise InvalidInputError("Registry page adapter does not match its candidates")
        identities = [(c.canonical_id, c.version) for c in self.candidates]
        if len(identities) != len(set(identities)):
            raise InvalidInputError("Registry page contains duplicate exact identities")
        if self.next_cursor is not None:
            text_value(self.next_cursor, "Registry cursor", 2048)
        if self.etag is not None:
            text_value(self.etag, "Registry ETag", 512)
        try:
            fetched = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if (
                fetched.tzinfo is None
                or expires.tzinfo is None
                or not 0 < (expires - fetched).total_seconds() <= 86400
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise InvalidInputError("Registry cache timestamps are invalid") from None
        digest_value(self.source_sha256)
        if not isinstance(self.status, str) or self.status not in {
            "ok",
            "authentication_required",
            "rate_limited",
            "unavailable",
            "not_found",
            "gone",
        }:
            raise InvalidInputError("Registry page status is invalid")
        if self.status != "ok" and (self.candidates or self.next_cursor):
            raise InvalidInputError("Unavailable registry page may not carry stale candidates")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) is not int or not 0 <= self.retry_after_seconds <= 86400
        ):
            raise InvalidInputError("Registry retry allowance is invalid")
        if len(canonical_json(self.to_dict()).encode("utf-8")) > MAX_PAGE_BYTES:
            raise InvalidInputError("Registry page exceeds its byte allowance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "potbot-registry-page/v1",
            "adapter": self.adapter,
            "query": self.query,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "nextCursor": self.next_cursor,
            "fetchedAt": self.fetched_at,
            "expiresAt": self.expires_at,
            "etag": self.etag,
            "sourceSha256": self.source_sha256,
            "status": self.status,
            "retryAfterSeconds": self.retry_after_seconds,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Self:
        fields = {
            "schema",
            "adapter",
            "query",
            "candidates",
            "nextCursor",
            "fetchedAt",
            "expiresAt",
            "etag",
            "sourceSha256",
            "status",
            "retryAfterSeconds",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != fields
            or raw["schema"] != "potbot-registry-page/v1"
            or not isinstance(raw["candidates"], list)
        ):
            raise InvalidInputError("Registry page fields do not match v1")
        return cls(
            raw["adapter"],
            raw["query"],
            tuple(RegistryCandidate.from_dict(item) for item in raw["candidates"]),
            raw["nextCursor"],
            raw["fetchedAt"],
            raw["expiresAt"],
            raw["etag"],
            raw["sourceSha256"],
            raw["status"],
            raw["retryAfterSeconds"],
        )
