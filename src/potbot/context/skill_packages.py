"""Bounded portable skill acquisition. Package contents are untrusted data.

Neither source adapter extracts an archive, checks out a repository, invokes an
installer, follows symlinks, expands credentials, or executes package code. The
manifest identifies every retained byte. Source provenance is recorded separately
so the same package from two sources has one content identity.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 128
MAX_MANIFEST_BYTES = 128 * 1024
MAX_FRONTMATTER_BYTES = 32 * 1024
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_GITHUB_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
_SCRIPT_SUFFIXES = frozenset(
    {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".rb", ".pl", ".ps1", ".bat", ".exe"}
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def validate_resource_path(value: str) -> str:
    """Accept an exact portable relative path; never normalize a hostile spelling."""
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", errors="surrogatepass")) > 512
        or value != unicodedata.normalize("NFC", value)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in ("\\", ":", "\x00"))
    ):
        raise InvalidInputError("Invalid portable skill resource path")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InvalidInputError("Skill resource paths must be UTF-8") from None
    parts = value.split("/")
    if len(parts) > 12 or any(
        part in {"", ".", ".."}
        or part.casefold() == ".git"
        or len(part.encode("utf-8")) > 128
        or part.endswith((".", " "))
        or _DEVICE.fullmatch(part)
        for part in parts
    ):
        raise InvalidInputError("Skill resource path is not a portable relative file name")
    return value


@dataclass(frozen=True, slots=True)
class SkillPackage:
    name: str
    description: str
    content: str
    content_sha256: str
    release_sha256: str
    manifest: dict[str, Any]
    files: dict[str, bytes]
    source: dict[str, Any]


def _json_data(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        raise InvalidInputError("Skill metadata is too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and -(2**53 - 1) <= value <= 2**53 - 1:
        return value
    if isinstance(value, float):
        if math.isfinite(value) and -(2**53 - 1) <= value <= 2**53 - 1:
            # PostgreSQL JSONB normalizes integral decimals and negative zero.
            # Hash the same semantic number before and after persistence, while
            # retaining the original YAML spelling in immutable SKILL.md bytes.
            return int(value) if value.is_integer() else value
    elif isinstance(value, str):
        if "\x00" not in value and len(value) <= MAX_FRONTMATTER_BYTES:
            try:
                value.encode("utf-8")
                return value
            except UnicodeError:
                pass
    elif isinstance(value, list) and len(value) <= 256:
        return [_json_data(item, depth=depth + 1) for item in value]
    elif isinstance(value, dict) and len(value) <= 256:
        if all(
            isinstance(key, str) and 1 <= len(key) <= 128 and _json_data(key) == key
            for key in value
        ):
            return {key: _json_data(item, depth=depth + 1) for key, item in value.items()}
    raise InvalidInputError("Skill metadata must be bounded JSON-compatible YAML data")


def _frontmatter(content: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        raise CapabilityUnavailableError(
            "Portable skills require the potbot[skills] extra"
        ) from None

    # Support portable CRLF documents while retaining the original bytes verbatim.
    normalized = content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise InvalidInputError("SKILL.md requires YAML frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0 and normalized.endswith("\n---"):
        end = len(normalized) - 4
    if end < 0 or len(normalized[:end].encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise InvalidInputError("SKILL.md frontmatter is missing or exceeds its byte allowance")

    class Loader(yaml.SafeLoader):  # type: ignore[misc]
        nodes = 0
        depth = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise InvalidInputError("YAML aliases are not supported in portable skills")
            self.nodes += 1
            self.depth += 1
            if self.nodes > 2048 or self.depth > 20:
                raise InvalidInputError("Skill metadata exceeds its structure allowance")
            try:
                return super().compose_node(parent, index)
            finally:
                self.depth -= 1

        def construct_mapping(self, node: Any, deep: bool = False) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise InvalidInputError("Skill metadata keys must be unique strings")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        value = yaml.load(normalized[4:end], Loader=Loader)
    except InvalidInputError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        raise InvalidInputError("Invalid SKILL.md YAML frontmatter") from None
    if not isinstance(value, dict):
        raise InvalidInputError("Skill frontmatter must be a mapping")
    result: dict[str, Any] = _json_data(value)
    name, description = result.get("name"), result.get("description")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or _NAME.fullmatch(name) is None:
        raise InvalidInputError("Skill name requires 1–64 lowercase letters, digits and hyphens")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise InvalidInputError("Skill description requires 1–1024 characters")
    for key, maximum in (("license", 1024), ("compatibility", 500)):
        if key in result and (
            not isinstance(result[key], str) or not 1 <= len(result[key]) <= maximum
        ):
            raise InvalidInputError(f"Skill {key} must be bounded text")
    return result


def _requirements(metadata: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    requirements: list[dict[str, str]] = []
    unsupported: set[str] = set()

    def values(kind: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            value = [value]
        if (
            not isinstance(value, list)
            or len(value) > 128
            or any(not isinstance(item, str) or not 1 <= len(item) <= 512 for item in value)
        ):
            raise InvalidInputError("Skill dependency declarations must be bounded text lists")
        requirements.extend({"kind": kind, "name": item, "status": "unverified"} for item in value)

    if metadata.get("compatibility"):
        values("compatibility", metadata["compatibility"])
    tools = metadata.get("allowed-tools")
    if isinstance(tools, str):
        tools = tools.replace(",", " ").split()
    if tools is not None:
        values("tool", tools)
        unsupported.add("allowed-tools does not grant Potbot execution authority")
    extra = metadata.get("metadata", {})
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (ValueError, RecursionError):
            unsupported.add("Non-JSON embedded metadata is retained without interpretation")
            extra = {}
    if not isinstance(extra, dict):
        raise InvalidInputError("Skill metadata must be a mapping or JSON mapping string")
    claw = extra.get("openclaw", {})
    if claw and not isinstance(claw, dict):
        raise InvalidInputError("OpenClaw metadata must be a mapping")
    if isinstance(claw, dict):
        requires = claw.get("requires", {})
        if not isinstance(requires, dict):
            raise InvalidInputError("OpenClaw requirements must be a mapping")
        for field, kind in (
            ("bins", "binary"),
            ("anyBins", "any_binary"),
            ("env", "environment"),
            ("config", "configuration"),
        ):
            values(kind, requires.get(field))
        values("operating_system", claw.get("os"))
        values("environment", claw.get("primaryEnv"))
        if claw.get("install"):
            unsupported.add("OpenClaw dependency installers are retained but never executed")
        if requires or any(field in claw for field in ("os", "always", "primaryEnv")):
            unsupported.add("OpenClaw runtime gates and credential injection are not implemented")
        known = {
            "requires",
            "os",
            "primaryEnv",
            "install",
            "always",
            "emoji",
            "homepage",
            "skillKey",
        }
        unsupported.update(f"Uninterpreted OpenClaw field: {key}" for key in claw.keys() - known)
        unsupported.update(
            f"Uninterpreted OpenClaw requirement: {key}"
            for key in requires.keys() - {"bins", "anyBins", "env", "config"}
        )
    if "clawdbot" in extra:
        unsupported.add("Legacy clawdbot metadata is retained without interpretation")
    known_top = {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
        "homepage",
    }
    unsupported.update(
        f"Uninterpreted frontmatter field: {key}" for key in metadata.keys() - known_top
    )
    unique = {(entry["kind"], entry["name"]): entry for entry in requirements}
    return [unique[key] for key in sorted(unique)], sorted(unsupported)


def _media_type(path: str) -> str:
    return {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".json": "application/json",
        ".yaml": "text/plain",
        ".yml": "text/plain",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".pdf": "application/pdf",
    }.get(Path(path).suffix.lower(), "application/octet-stream")


def package_from_files(files: Mapping[str, bytes], *, source: dict[str, Any]) -> SkillPackage:
    if not isinstance(files, Mapping) or not 1 <= len(files) <= MAX_FILES:
        raise InvalidInputError("Skill package exceeds its file-count allowance")
    retained: dict[str, bytes] = {}
    folded: set[str] = set()
    total = 0
    for name, file_bytes in sorted(files.items()):
        validate_resource_path(name)
        if (
            name.casefold() in folded
            or not isinstance(file_bytes, bytes)
            or len(file_bytes) > MAX_FILE_BYTES
        ):
            raise InvalidInputError("Skill files must be unique, bounded regular-file bytes")
        folded.add(name.casefold())
        total += len(file_bytes)
        if total > MAX_PACKAGE_BYTES:
            raise InvalidInputError("Skill package exceeds its expanded byte allowance")
        retained[name] = file_bytes
    for name in retained:
        parts = name.split("/")
        if any("/".join(parts[:index]).casefold() in folded for index in range(1, len(parts))):
            raise InvalidInputError("Skill resource paths contain a file/directory collision")
    if "SKILL.md" not in retained:
        raise InvalidInputError("Skill package must contain an exact root SKILL.md")
    try:
        content = retained["SKILL.md"].decode("utf-8")
    except UnicodeError:
        raise InvalidInputError("SKILL.md must contain UTF-8 text") from None
    if "\x00" in content:
        raise InvalidInputError("SKILL.md cannot contain NUL characters")
    metadata = _frontmatter(content)
    requirements, unsupported = _requirements(metadata)
    entries = [
        {
            "path": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "media_type": _media_type(name),
            "executable_hint": name.split("/")[0] == "scripts"
            or Path(name).suffix.lower() in _SCRIPT_SUFFIXES
            or data.startswith(b"#!"),
        }
        for name, data in retained.items()
    ]
    findings = []
    if any(entry["executable_hint"] for entry in entries):
        findings.append(
            {
                "code": "scripts_data_only",
                "severity": "notice",
                "message": "Bundled scripts are preserved as data; installation and loading never execute them.",
            }
        )
    if any(name in retained for name in ("AGENTS.md", "MODEL.md", "MEMORY.md")):
        findings.append(
            {
                "code": "context_files_are_resources",
                "severity": "notice",
                "message": "Package context files remain untrusted resources, separate from Potbot's live Context documents.",
            }
        )
    if "license" not in metadata:
        findings.append(
            {
                "code": "license_unspecified",
                "severity": "warning",
                "message": "The package does not declare a license; review any bundled license before approving distribution.",
            }
        )
    if requirements:
        findings.append(
            {
                "code": "requirements_unverified",
                "severity": "warning",
                "message": "Declared dependencies have not been checked in an execution sandbox. Installation does not establish readiness.",
            }
        )
    if unsupported:
        findings.append(
            {
                "code": "unsupported_behavior",
                "severity": "warning",
                "message": "Vendor-specific behavior is retained for review and is not translated into Potbot permissions or runtime hooks.",
            }
        )
    content_digest = hashlib.sha256(retained["SKILL.md"]).hexdigest()
    manifest = {
        "schema": "potbot-skill-package/v1",
        "name": metadata["name"],
        "description": metadata["description"],
        "content_sha256": content_digest,
        "files": entries,
        "metadata": metadata,
        "requirements": requirements,
        "compatibility": {
            "status": "review_required" if unsupported or requirements else "portable",
            "unsupported": unsupported,
        },
        "findings": findings,
    }
    raw = canonical_json(manifest)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise InvalidInputError("Skill manifest exceeds its byte allowance")
    provenance: dict[str, Any] = _json_data(source)
    if not isinstance(provenance, dict) or len(canonical_json(provenance)) > 8192:
        raise InvalidInputError("Invalid skill source provenance")
    return SkillPackage(
        metadata["name"],
        metadata["description"],
        content,
        content_digest,
        hashlib.sha256(raw).hexdigest(),
        manifest,
        retained,
        provenance,
    )


def verify_package(
    manifest: dict[str, Any], files: Mapping[str, bytes], *, source: dict[str, Any]
) -> SkillPackage:
    package = package_from_files(files, source=source)
    if canonical_json(package.manifest) != canonical_json(manifest):
        raise InvalidInputError("Retained skill package differs from its immutable manifest")
    return package


def parse_skill_zip(
    raw: bytes, *, source: dict[str, Any] | None = None, subdirectory: str = ""
) -> SkillPackage:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_ARCHIVE_BYTES:
        raise InvalidInputError("Skill ZIP exceeds the 4 MiB compressed byte allowance")
    if subdirectory:
        validate_resource_path(subdirectory)
    provenance = dict(source or {"kind": "zip"})
    provenance["archive_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 4096:
                raise InvalidInputError("Skill ZIP contains too many directory entries")
            names: set[str] = set()
            for item in entries:
                # orig_filename preserves NULs that ZipInfo.filename truncates.
                name = item.orig_filename[:-1] if item.is_dir() else item.orig_filename
                validate_resource_path(name)
                folded = name.casefold()
                if folded in names:
                    raise InvalidInputError("Skill ZIP contains duplicate or colliding paths")
                names.add(folded)
                mode = item.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (
                    kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (kind == stat.S_IFDIR) != item.is_dir()
                    and kind != 0
                ):
                    raise InvalidInputError(
                        "Skill ZIP may contain only regular files and directories"
                    )
                if item.flag_bits & 1 or item.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    raise InvalidInputError(
                        "Encrypted or unsupported skill ZIP entries are not accepted"
                    )
            prefix = subdirectory + "/" if subdirectory else ""
            if not prefix and not any(item.filename == "SKILL.md" for item in entries):
                roots = {item.filename.split("/")[0] for item in entries}
                if len(roots) == 1:
                    prefix = next(iter(roots)) + "/"
            selected = [
                item for item in entries if not item.is_dir() and item.filename.startswith(prefix)
            ]
            if (
                not 1 <= len(selected) <= MAX_FILES
                or sum(item.file_size for item in selected) > MAX_PACKAGE_BYTES
            ):
                raise InvalidInputError("Selected skill ZIP exceeds its expanded package allowance")
            files: dict[str, bytes] = {}
            for item in selected:
                if item.file_size > MAX_FILE_BYTES:
                    raise InvalidInputError("A skill ZIP file exceeds the 1 MiB byte allowance")
                with archive.open(item) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != item.file_size or len(data) > MAX_FILE_BYTES:
                    raise InvalidInputError("Skill ZIP file size does not match its header")
                files[item.filename[len(prefix) :]] = data
    except InvalidInputError:
        raise
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, ValueError, zlib.error):
        raise InvalidInputError("Invalid or unsupported skill ZIP archive") from None
    provenance["path"] = prefix.rstrip("/")
    provenance["verification"] = "content-integrity-only"
    return package_from_files(files, source=provenance)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise InvalidInputError("Skill sources may not redirect to another location")


def fetch_github_skill(url: str, commit: str, path: str = "") -> SkillPackage:
    """Public GitHub only, exact commit, fixed host, no Git subprocess or credentials."""
    if (
        not isinstance(url, str)
        or len(url) > 300
        or not isinstance(commit, str)
        or not _COMMIT.fullmatch(commit)
    ):
        raise InvalidInputError(
            "GitHub acquisition requires a canonical repository and exact 40-character commit"
        )
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise InvalidInputError("Invalid GitHub source URL") from None
    parts = parsed.path.removeprefix("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or any(not _GITHUB_PART.fullmatch(part) for part in parts)
        or parts[1].endswith(".git")
        or url != "https://github.com/" + "/".join(parts)
    ):
        raise InvalidInputError(
            "Only canonical public https://github.com/owner/repository sources are supported"
        )
    if path:
        validate_resource_path(path)
    archive_url = f"https://codeload.github.com/{parts[0]}/{parts[1]}/zip/{commit}"
    request = Request(
        archive_url,
        headers={"Accept": "application/zip", "User-Agent": "potbot-skill-acquisition/1"},
    )
    # Disable ambient proxy configuration as well as redirects; never attach the
    # API bearer, setup capability or host Git credential helper to source fetches.
    from urllib.request import ProxyHandler

    try:
        deadline = time.monotonic() + 20
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=10) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_ARCHIVE_BYTES):
                raise InvalidInputError("GitHub skill archive exceeds its byte allowance")
            chunks = bytearray()
            while len(chunks) <= MAX_ARCHIVE_BYTES:
                if time.monotonic() > deadline:
                    raise CapabilityUnavailableError(
                        "Pinned skill source exceeded its time allowance"
                    )
                chunk = response.read1(min(65536, MAX_ARCHIVE_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise CapabilityUnavailableError(
            "Unable to retrieve the pinned public GitHub skill archive"
        ) from None
    root = f"{parts[1]}-{commit}"
    selected = root + ("/" + path if path else "")
    package = parse_skill_zip(
        raw,
        source={"kind": "github", "url": url, "commit": commit, "requested_path": path},
        subdirectory=selected,
    )
    return package


def read_skill_archive(path: Path) -> bytes:
    """Read an explicit CLI ZIP without following a link or waiting on a device."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise CapabilityUnavailableError("Local skill acquisition requires a no-follow file API")
    path = Path(path).absolute()
    if path.resolve() != path:
        raise InvalidInputError("Skill ZIP source may not contain linked path components")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 1 <= before.st_size <= MAX_ARCHIVE_BYTES
            ):
                raise InvalidInputError("Skill ZIP source must be a bounded unlinked regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_ARCHIVE_BYTES + 1)
            after = os.fstat(fd)
            if len(data) != before.st_size or (
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_size,
            ) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
                raise InvalidInputError("Skill ZIP source changed while reading")
            return data
        finally:
            os.close(fd)
    except OSError:
        raise InvalidInputError("Skill ZIP source is unavailable or unsafe") from None


def bundle_skill_directory(directory: Path) -> bytes:
    """CLI-only local read. No host path is accepted by the HTTP acquisition API."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fwalk"):
        raise CapabilityUnavailableError("Local skill acquisition requires a no-follow file API")
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
        raise InvalidInputError(
            "Local skill source must be a real directory without linked parents"
        )
    files: dict[str, bytes] = {}
    count = 0
    # Hold each parent by descriptor. Even replacing a directory with a symlink
    # during traversal cannot redirect a resource read outside the selected tree.
    root_fd = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in directory.parts[1:]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd
            )
            os.close(root_fd)
            root_fd = next_fd
        for parent, directories, names, parent_fd in os.fwalk(
            ".", dir_fd=root_fd, follow_symlinks=False
        ):
            count += len(directories) + len(names)
            if count > 1024:
                raise InvalidInputError("Local skill source contains too many entries")
            for name in directories:
                validate_resource_path((Path(parent) / name).as_posix())
                if not stat.S_ISDIR(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                    raise InvalidInputError("Local skill source may not contain symbolic links")
            for name in names:
                relative = validate_resource_path((Path(parent) / name).as_posix())
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
                try:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_size > MAX_FILE_BYTES
                    ):
                        raise InvalidInputError(
                            "Local skill resources must be bounded unlinked regular files"
                        )
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                    after = os.fstat(fd)
                    if len(data) != info.st_size or (
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        info.st_size,
                    ) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
                        raise InvalidInputError("Local skill resource changed while reading")
                    files[relative] = data
                    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_PACKAGE_BYTES:
                        raise InvalidInputError("Local skill source exceeds its package allowance")
                finally:
                    os.close(fd)
    except OSError:
        raise InvalidInputError(
            "Local skill source changed or contains an unsafe resource"
        ) from None
    finally:
        os.close(root_fd)
    package_from_files(files, source={"kind": "local"})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    raw = output.getvalue()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise InvalidInputError("Local skill ZIP exceeds its compressed byte allowance")
    return raw
