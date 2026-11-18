"""Documented, bounded registry reads and exact data-only skill acquisition.

No registry response chooses an HTTP destination, shell command, credential or
installation owner. Source admission and retained refresh state belong to the
discovery service; this module supplies observations, never permissions.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from potbot.context.skill_packages import (
    MAX_ARCHIVE_BYTES,
    SkillPackage,
    fetch_github_skill,
    package_from_files,
    parse_skill_zip,
    validate_resource_path,
)
from potbot.domain.discovery import (
    ADAPTERS,
    MAX_CANDIDATE_BYTES,
    MAX_PAGE_BYTES,
    RegistryCandidate,
    RegistryPage,
    bounded_json,
    github_repository,
    parse_json,
    text_value,
)
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, StateMismatchError
from potbot.domain.serialization import content_hash

_HOSTS = frozenset({"clawhub.ai", "registry.modelcontextprotocol.io", "skills.sh"})
_PART = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z", re.ASCII)
_MCP_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII
)
_SKILLS_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}){1,2}\Z", re.ASCII
)
_EMPTY_CAPABILITIES: dict[str, list[str]] = {
    "classes": [],
    "inputTypes": [],
    "outputTypes": [],
    "effects": [],
    "scopes": [],
    "platforms": [],
}


@dataclass(frozen=True, slots=True)
class RegistryHTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class RegistryTransport(Protocol):
    def __call__(
        self, url: str, *, headers: dict[str, str], max_bytes: int
    ) -> RegistryHTTPResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise CapabilityUnavailableError("Registry redirects are unsupported")


def registry_http_get(url: str, *, headers: dict[str, str], max_bytes: int) -> RegistryHTTPResponse:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in _HOSTS or parsed.fragment:
        raise InvalidInputError("Registry reads require an explicitly supported fixed HTTPS host")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ARCHIVE_BYTES:
        raise InvalidInputError("Registry read byte allowance is invalid")
    if set(headers) - {"Accept", "User-Agent", "Authorization", "If-None-Match"}:
        raise InvalidInputError("Registry request headers are unsupported")
    if "Authorization" in headers and parsed.netloc != "skills.sh":
        raise InvalidInputError("Registry credential has no authority for this host")
    for key, value in headers.items():
        text_value(value, f"Registry {key} header", 8192)
    deadline = time.monotonic() + 20
    request = Request(url, headers=headers, method="GET")
    try:
        response = build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=10)
    except HTTPError as error:
        # An error body is untrusted and can contain secrets/diagnostics. Retain
        # only bounded control metadata; it is never reflected to the caller.
        with error:
            return RegistryHTTPResponse(error.code, dict(error.headers.items()), b"")
    except (URLError, TimeoutError, OSError):
        raise CapabilityUnavailableError("Registry source is unavailable") from None
    try:
        with response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                raise CapabilityUnavailableError("Registry response exceeds its byte allowance")
            data = bytearray()
            while len(data) <= max_bytes:
                if time.monotonic() > deadline:
                    raise CapabilityUnavailableError(
                        "Registry response exceeded its time allowance"
                    )
                chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > max_bytes:
                raise CapabilityUnavailableError("Registry response exceeds its byte allowance")
            return RegistryHTTPResponse(
                response.status, dict(response.headers.items()), bytes(data)
            )
    except (URLError, TimeoutError, OSError):
        raise CapabilityUnavailableError("Registry source is unavailable") from None


class RegistrySourceUnavailable(CapabilityUnavailableError):
    def __init__(self, status: str, retry_after_seconds: int | None = None) -> None:
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Registry source status: {status}")


def _record(
    *,
    adapter: str,
    identity: str,
    kind: str,
    version: str | None,
    name: str,
    description: str,
    publisher: str,
    source_url: str,
    manifest: dict[str, Any],
    source_sha256: str,
    status: str = "active",
    distribution: dict[str, Any] | None = None,
    requirements: list[dict[str, Any]] | None = None,
    unsupported: list[str] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> RegistryCandidate:
    manifest = bounded_json(manifest)
    return RegistryCandidate.from_dict(
        {
            "schema": "potbot-registry-candidate/v1",
            "adapter": adapter,
            "canonicalId": identity,
            "kind": kind,
            "version": version,
            "name": name,
            "description": description,
            "publisher": {
                "id": publisher,
                "evidence": "operator_claim" if adapter == "curated" else "registry_claim",
            },
            "sourceUrl": source_url,
            "status": status,
            "capabilities": capabilities or _EMPTY_CAPABILITIES,
            "requirements": requirements or [],
            "distribution": distribution or {"kind": "inspect_only"},
            "manifest": manifest,
            "manifestSha256": content_hash(manifest),
            "sourceSha256": source_sha256,
            "unsupported": unsupported or [],
        }
    )


def parse_curated_manifest(raw: bytes) -> tuple[RegistryCandidate, ...]:
    """Parse an explicitly supplied local manifest, never a file selected by metadata.

    Entries use the candidate wire shape without schema/adapter/digests; those
    fields are derived locally. The source owner separately admits the digest
    of the complete original manifest and the resulting immutable snapshot.
    """
    value = parse_json(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "entries"}
        or value["schema"] != "potbot-curated-registry/v1"
    ):
        raise InvalidInputError("Curated manifest fields do not match v1")
    entries = value["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
        raise InvalidInputError("Curated manifest requires 1 to 256 entries")
    candidates = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) & {
            "schema",
            "adapter",
            "sourceSha256",
            "manifestSha256",
        }:
            raise InvalidInputError("Curated entry may not supply derived identity fields")
        candidate = RegistryCandidate.from_dict(
            {
                **entry,
                "schema": "potbot-registry-candidate/v1",
                "adapter": "curated",
                "sourceSha256": hashlib.sha256(raw).hexdigest(),
                "manifestSha256": content_hash(entry.get("manifest")),
            }
        )
        if (candidate.canonical_id, candidate.version) in seen:
            raise InvalidInputError("Curated manifest contains duplicate exact identities")
        seen.add((candidate.canonical_id, candidate.version))
        candidates.append(candidate)
    return tuple(candidates)


def _plain(value: Any, maximum: int, *, default: str = "") -> str:
    if value is None:
        return default
    # Descriptions are display text; collapse whitespace without interpreting
    # markdown, escape sequences or embedded instructions as machine fields.
    if not isinstance(value, str):
        raise InvalidInputError("Registry display metadata must be text")
    return text_value(" ".join(value.split()), "Registry display metadata", maximum, empty=True)


def _claw_identity(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2 or any(not _PART.fullmatch(part) for part in parts):
        raise InvalidInputError("ClawHub requires the exact owner/slug identity")
    return parts[0], parts[1]


def _version(value: Any) -> str:
    value = text_value(value, "Exact registry version", 128)
    if value.lower() in {"latest", "main", "master", "head", "*"} or "/" in value or "\\" in value:
        raise InvalidInputError("Resolve requires an exact version, not a moving selector")
    return str(value)


def _files_inventory(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise InvalidInputError("Registry skill has no exact file inventory")
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            raise InvalidInputError("Registry file inventory entry is invalid")
        result.append({key: entry.get(key) for key in ("path", "size", "sha256")})
    return result


def _requirements(package: SkillPackage) -> list[dict[str, Any]]:
    result = []
    for item in package.manifest["requirements"]:
        entry = {"kind": item["kind"], "name": item["name"]}
        if item["kind"] in {"any_binary", "operating_system"}:
            entry["group"] = item["kind"]
        result.append(entry)
    return result


class RegistrySources:
    def __init__(
        self,
        *,
        curated: tuple[RegistryCandidate, ...] = (),
        transport: RegistryTransport = registry_http_get,
        skills_sh_token: Callable[[], str | None] | None = None,
        ttl_seconds: int = 900,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise InvalidInputError("Registry TTL must be 1 to 86400 seconds")
        if not isinstance(curated, tuple) or any(c.adapter != "curated" for c in curated):
            raise InvalidInputError("Curated source must contain validated curated candidates")
        identities = [(c.canonical_id, c.version) for c in curated]
        if len(identities) != len(set(identities)) or len(curated) > 256:
            raise InvalidInputError("Curated source has duplicate or excessive entries")
        self.curated = tuple(curated)
        self.transport = transport
        self.skills_sh_token = skills_sh_token
        self.ttl_seconds = ttl_seconds
        self.now = now or (lambda: datetime.now(UTC))

    def _headers(self, adapter: str, *, binary: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/zip, application/json" if binary else "application/json",
            "User-Agent": "potbot-registry-discovery/1",
        }
        if adapter == "skills_sh":
            token = self.skills_sh_token() if self.skills_sh_token else None
            if token is None:
                raise RegistrySourceUnavailable("authentication_required")
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 8192
                or any(not 33 <= ord(c) <= 126 for c in token)
            ):
                raise RegistrySourceUnavailable("authentication_required")
            headers["Authorization"] = "Bearer " + token
        return headers

    def _get(
        self, adapter: str, url: str, *, maximum: int = MAX_PAGE_BYTES, binary: bool = False
    ) -> RegistryHTTPResponse:
        response = self.transport(
            url, headers=self._headers(adapter, binary=binary), max_bytes=maximum
        )
        if (
            not isinstance(response, RegistryHTTPResponse)
            or type(response.status) is not int
            or not isinstance(response.body, bytes)
            or len(response.body) > maximum
        ):
            raise RegistrySourceUnavailable("unavailable")
        if response.status != 200:
            status = {
                401: "authentication_required",
                403: "unavailable",
                404: "not_found",
                410: "gone",
                429: "rate_limited",
            }.get(response.status, "unavailable")
            retry = None
            raw = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
            if isinstance(raw, str):
                try:
                    retry = (
                        int(raw)
                        if raw.isdecimal()
                        else int((parsedate_to_datetime(raw) - self.now()).total_seconds())
                    )
                    retry = max(0, min(retry, 86400))
                except (ValueError, TypeError, OverflowError):
                    retry = None
            raise RegistrySourceUnavailable(status, retry)
        return response

    def _json(self, adapter: str, url: str) -> tuple[dict[str, Any], RegistryHTTPResponse]:
        response = self._get(adapter, url)
        media = next((v for k, v in response.headers.items() if k.lower() == "content-type"), "")
        if media.split(";", 1)[0].strip().lower() != "application/json":
            raise RegistrySourceUnavailable("unavailable")
        result = parse_json(response.body)
        if not isinstance(result, dict):
            raise InvalidInputError("Registry response must be an object")
        return result, response

    def _page(
        self,
        adapter: str,
        query: str,
        candidates: tuple[RegistryCandidate, ...],
        cursor: str | None,
        response: RegistryHTTPResponse | None = None,
        failure: RegistrySourceUnavailable | None = None,
    ) -> RegistryPage:
        now = self.now().astimezone(UTC)
        etag = None
        if response:
            etag = next((v for k, v in response.headers.items() if k.lower() == "etag"), None)
            if etag is not None:
                text_value(etag, "Registry ETag", 512)
        digest = (
            hashlib.sha256(response.body).hexdigest()
            if response
            else content_hash([c.to_dict() for c in candidates])
        )
        return RegistryPage(
            adapter,
            query,
            candidates,
            cursor,
            now.isoformat(),
            (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            etag,
            digest,
            failure.status if failure else "ok",
            failure.retry_after_seconds if failure else None,
        )

    def search(
        self, adapter: str, query: str, cursor: str | None = None, limit: int = 25
    ) -> RegistryPage:
        if adapter not in ADAPTERS:
            raise InvalidInputError("Unsupported registry adapter")
        text_value(query, "Registry query", 256, empty=True)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("Registry page size must be 1 to 100")
        if cursor is not None:
            text_value(cursor, "Registry cursor", 2048)
        if adapter == "curated":
            selected = sorted(
                (
                    c
                    for c in self.curated
                    if not query or query.casefold() in (c.name + " " + c.description).casefold()
                ),
                key=lambda c: (c.canonical_id, c.version or ""),
            )
            try:
                offset = int(cursor or "0")
                if str(offset) != (cursor or "0") or not 0 <= offset <= len(selected):
                    raise ValueError
            except ValueError:
                raise InvalidInputError("Curated cursor is invalid") from None
            page = tuple(selected[offset : offset + limit])
            next_cursor = str(offset + len(page)) if offset + len(page) < len(selected) else None
            return self._page(adapter, query, page, next_cursor)
        try:
            if adapter == "clawhub":
                if query:
                    if cursor is not None:
                        raise InvalidInputError("ClawHub relevance search has no documented cursor")
                    raw, response = self._json(
                        adapter,
                        "https://clawhub.ai/api/v1/search?"
                        + urlencode({"q": query, "limit": limit}),
                    )
                    entries = raw.get("results")
                    next_cursor = None
                else:
                    params: dict[str, Any] = {"limit": limit, "sort": "name"}
                    if cursor:
                        params["cursor"] = cursor
                    raw, response = self._json(
                        adapter, "https://clawhub.ai/api/v1/skills?" + urlencode(params)
                    )
                    entries, next_cursor = raw.get("items"), raw.get("nextCursor")
                if not isinstance(entries, list) or len(entries) > limit:
                    raise InvalidInputError("ClawHub returned an excessive or invalid page")
                candidates = tuple(self._claw_summary(item) for item in entries)
            elif adapter == "mcp_registry":
                if query and len(query.strip()) < 3:
                    raise InvalidInputError(
                        "MCP Registry search requires at least three characters"
                    )
                params = {"limit": limit, "version": "latest", "include_deleted": "true"}
                if query:
                    params["search"] = query
                if cursor:
                    params["cursor"] = cursor
                raw, response = self._json(
                    adapter,
                    "https://registry.modelcontextprotocol.io/v0.1/servers?" + urlencode(params),
                )
                entries = raw.get("servers")
                if not isinstance(entries, list) or len(entries) > limit:
                    raise InvalidInputError("MCP Registry returned an excessive or invalid page")
                meta = raw.get("metadata", {})
                if not isinstance(meta, dict):
                    raise InvalidInputError("MCP Registry pagination metadata is invalid")
                next_cursor = meta.get("nextCursor") or None
                candidates = tuple(self._mcp(item) for item in entries)
            else:
                if query:
                    if len(query) < 2 or cursor is not None:
                        raise InvalidInputError(
                            "skills.sh search needs two characters and has no documented cursor"
                        )
                    raw, response = self._json(
                        adapter,
                        "https://skills.sh/api/v1/skills/search?"
                        + urlencode({"q": query, "limit": limit}),
                    )
                    next_cursor = None
                else:
                    try:
                        page_number = int(cursor or "0")
                        if not 0 <= page_number <= 100000 or str(page_number) != (cursor or "0"):
                            raise ValueError
                    except ValueError:
                        raise InvalidInputError("skills.sh page cursor is invalid") from None
                    raw, response = self._json(
                        adapter,
                        "https://skills.sh/api/v1/skills?"
                        + urlencode({"view": "all-time", "page": page_number, "per_page": limit}),
                    )
                    pagination = raw.get("pagination")
                    if (
                        not isinstance(pagination, dict)
                        or type(pagination.get("hasMore")) is not bool
                    ):
                        raise InvalidInputError("skills.sh pagination metadata is invalid")
                    next_cursor = str(page_number + 1) if pagination["hasMore"] else None
                entries = raw.get("data")
                if not isinstance(entries, list) or len(entries) > limit:
                    raise InvalidInputError("skills.sh returned an excessive or invalid page")
                candidates = tuple(self._skills_summary(item) for item in entries)
            if next_cursor == cursor and next_cursor is not None:
                raise InvalidInputError("Registry pagination did not advance")
            return self._page(adapter, query, candidates, next_cursor, response)
        except RegistrySourceUnavailable as error:
            return self._page(adapter, query, (), None, failure=error)
        except CapabilityUnavailableError:
            return self._page(
                adapter, query, (), None, failure=RegistrySourceUnavailable("unavailable")
            )

    def _claw_summary(self, raw: Any) -> RegistryCandidate:
        if not isinstance(raw, dict):
            raise InvalidInputError("ClawHub skill metadata is invalid")
        owner = raw.get("ownerHandle")
        if owner is None and isinstance(raw.get("owner"), dict):
            owner = raw["owner"].get("handle")
        slug = raw.get("slug")
        if not isinstance(owner, str) or not isinstance(slug, str):
            raise InvalidInputError("ClawHub response lacks owner-qualified identity")
        _claw_identity(owner + "/" + slug)
        latest = raw.get("latestVersion")
        version = raw.get("version") or (
            latest.get("version") if isinstance(latest, dict) else None
        )
        if version is not None:
            _version(version)
        # Keep semantic display metadata, not popularity as capability evidence.
        manifest = {
            key: raw[key]
            for key in (
                "slug",
                "displayName",
                "summary",
                "topics",
                "latestVersion",
                "metadata",
                "ownerHandle",
            )
            if key in raw
        }
        return _record(
            adapter="clawhub",
            identity=owner + "/" + slug,
            kind="skill",
            version=version,
            name=_plain(raw.get("displayName"), 128, default=slug),
            description=_plain(raw.get("summary"), 4096),
            publisher=owner,
            source_url=f"https://clawhub.ai/{owner}/skills/{slug}",
            manifest=manifest,
            source_sha256=content_hash(bounded_json(raw)),
            status="active" if version else "unresolved",
            unsupported=["exact_version_file_inventory_not_resolved"],
        )

    def _mcp(self, raw: Any) -> RegistryCandidate:
        if not isinstance(raw, dict) or not isinstance(raw.get("server"), dict):
            raise InvalidInputError("MCP Registry server record is invalid")
        server = raw["server"]
        identity = server.get("name")
        if not isinstance(identity, str) or not _MCP_NAME.fullmatch(identity):
            raise InvalidInputError("MCP Registry canonical server name is invalid")
        version = _version(server.get("version"))
        outer = raw.get("_meta", {})
        official = (
            outer.get("io.modelcontextprotocol.registry/official", {})
            if isinstance(outer, dict)
            else {}
        )
        status = (
            official.get("status", "unresolved") if isinstance(official, dict) else "unresolved"
        )
        if not isinstance(status, str) or status not in {
            "active",
            "deprecated",
            "deleted",
            "unresolved",
        }:
            raise InvalidInputError("MCP Registry status is unsupported")
        requirements: list[dict[str, Any]] = []
        packages = server.get("packages", [])
        if not isinstance(packages, list) or len(packages) > 32:
            raise InvalidInputError("MCP Registry package declarations exceed allowance")
        for package in packages:
            if not isinstance(package, dict):
                raise InvalidInputError("MCP package declaration is invalid")
            env = package.get("environmentVariables", [])
            if not isinstance(env, list) or len(env) > 128:
                raise InvalidInputError("MCP environment declarations exceed allowance")
            for variable in env:
                if not isinstance(variable, dict):
                    raise InvalidInputError("MCP environment declaration is invalid")
                if variable.get("isRequired") is True:
                    entry = {"kind": "environment", "name": variable.get("name")}
                    if entry not in requirements:
                        requirements.append(entry)
        return _record(
            adapter="mcp_registry",
            identity=identity,
            kind="mcp_server",
            version=version,
            name=_plain(server.get("title"), 128, default=identity),
            description=_plain(server.get("description"), 4096),
            publisher=identity.split("/", 1)[0],
            source_url=f"https://registry.modelcontextprotocol.io/v0.1/servers/{quote(identity, safe='')}/versions/{quote(version, safe='')}",
            manifest=bounded_json(raw),
            source_sha256=content_hash(bounded_json(raw)),
            status=status,
            requirements=requirements,
            unsupported=[
                "registry_metadata_is_not_live_tool_discovery",
                "package_installation_requires_existing_operator_recipe",
            ],
        )

    def _skills_summary(self, raw: Any) -> RegistryCandidate:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("id"), str)
            or not _SKILLS_ID.fullmatch(raw["id"])
        ):
            raise InvalidInputError("skills.sh canonical identity is invalid")
        identity = raw["id"]
        source = raw.get("source")
        if not isinstance(source, str) or identity.rsplit("/", 1)[0] != source:
            raise InvalidInputError("skills.sh source and canonical identity disagree")
        manifest = {
            key: raw[key]
            for key in ("id", "slug", "name", "source", "sourceType", "installUrl", "url")
            if key in raw
        }
        return _record(
            adapter="skills_sh",
            identity=identity,
            kind="skill",
            version=None,
            name=_plain(raw.get("name"), 128, default=identity.rsplit("/", 1)[1]),
            description=_plain(raw.get("description"), 4096),
            publisher=source,
            source_url="https://skills.sh/" + quote(identity, safe="/"),
            manifest=manifest,
            source_sha256=content_hash(bounded_json(raw)),
            status="unresolved",
            unsupported=["registry_listing_has_no_immutable_git_revision", "snapshot_not_resolved"],
        )

    def inspect(
        self, adapter: str, canonical_id: str, version: str | None = None
    ) -> RegistryCandidate:
        if adapter not in ADAPTERS:
            raise InvalidInputError("Unsupported registry adapter")
        text_value(canonical_id, "Registry identity", 320)
        if version is not None:
            _version(version)
        if adapter == "curated":
            matches = [
                c
                for c in self.curated
                if c.canonical_id == canonical_id and (version is None or c.version == version)
            ]
            if len(matches) != 1:
                raise RegistrySourceUnavailable("not_found" if not matches else "unavailable")
            return matches[0]
        if adapter == "clawhub":
            owner, slug = _claw_identity(canonical_id)
            base = "https://clawhub.ai/api/v1/skills/" + quote(slug, safe="")
            params = urlencode({"ownerHandle": owner})
            detail, _ = self._json(adapter, base + "?" + params)
            if (
                not isinstance(detail.get("skill"), dict)
                or not isinstance(detail.get("owner"), dict)
                or detail["owner"].get("handle") != owner
                or detail["skill"].get("slug") != slug
            ):
                raise StateMismatchError(
                    "ClawHub returned a different publisher or canonical identity"
                )
            merged = {
                **detail["skill"],
                "ownerHandle": owner,
                "latestVersion": detail.get("latestVersion"),
                "metadata": detail.get("metadata"),
            }
            summary = self._claw_summary(merged)
            exact = version or summary.version
            if exact is None:
                return summary
            _version(exact)
            resolved, _ = self._json(
                adapter, base + "/versions/" + quote(exact, safe="") + "?" + params
            )
            item = resolved.get("version")
            if not isinstance(item, dict) or item.get("version") != exact:
                raise StateMismatchError("ClawHub returned a different exact version")
            if isinstance(resolved.get("skill"), dict) and resolved["skill"].get("slug") != slug:
                raise StateMismatchError("ClawHub version identity differs from the selection")
            files = _files_inventory(item.get("files"))
            moderation = detail.get("moderation", {})
            blocked = isinstance(moderation, dict) and (
                moderation.get("isMalwareBlocked") is True
                or moderation.get("verdict") == "malicious"
            )
            # Counters and moving latest tags do not change an exact release.
            # Identity, file hashes, publisher metadata and moderation do.
            release_skill = {
                key: merged[key]
                for key in ("slug", "displayName", "summary", "topics", "ownerHandle", "metadata")
                if key in merged
            }
            manifest = {"skill": release_skill, "version": item, "moderation": moderation}
            return _record(
                adapter=adapter,
                identity=canonical_id,
                kind="skill",
                version=exact,
                name=summary.name,
                description=summary.description,
                publisher=owner,
                source_url=summary.to_dict()["sourceUrl"],
                manifest=manifest,
                source_sha256=content_hash(bounded_json(manifest)),
                status="unsupported" if blocked else "active",
                distribution={
                    "kind": "clawhub_skill",
                    "owner": owner,
                    "slug": slug,
                    "version": exact,
                    "files": files,
                },
                unsupported=["registry_scan_is_not_local_qualification"]
                + (["registry_malware_blocked"] if blocked else []),
            )
        if adapter == "mcp_registry":
            if not _MCP_NAME.fullmatch(canonical_id):
                raise InvalidInputError("MCP Registry canonical identity is invalid")
            selector = version or "latest"
            raw, _ = self._json(
                adapter,
                f"https://registry.modelcontextprotocol.io/v0.1/servers/{quote(canonical_id, safe='')}/versions/{quote(selector, safe='')}",
            )
            candidate = self._mcp(raw)
            if candidate.canonical_id != canonical_id or (
                version is not None and candidate.version != version
            ):
                raise StateMismatchError("MCP Registry returned a different identity or version")
            return candidate
        if not _SKILLS_ID.fullmatch(canonical_id):
            raise InvalidInputError("skills.sh canonical identity is invalid")
        raw, _ = self._json(
            adapter, "https://skills.sh/api/v1/skills/" + quote(canonical_id, safe="/")
        )
        if raw.get("id") != canonical_id or raw.get("source") != canonical_id.rsplit("/", 1)[0]:
            raise StateMismatchError("skills.sh returned a different source identity")
        if raw.get("files") is None:
            return self._skills_summary(raw)
        package, inventory = self._skills_package(raw, canonical_id)
        digest = content_hash({"id": canonical_id, "source": raw["source"], "files": inventory})
        exact = "snapshot:" + digest
        if version is not None and version != exact:
            raise StateMismatchError("skills.sh snapshot changed after selection")
        manifest = {
            "id": canonical_id,
            "source": raw["source"],
            "slug": raw.get("slug"),
            "claimedContentHash": raw.get("hash"),
            "files": inventory,
            "packageManifestSha256": package.release_sha256,
            "metadata": package.manifest["metadata"],
        }
        return _record(
            adapter=adapter,
            identity=canonical_id,
            kind="skill",
            version=exact,
            name=package.name,
            description=package.description,
            publisher=raw["source"],
            source_url="https://skills.sh/" + quote(canonical_id, safe="/"),
            manifest=manifest,
            source_sha256=digest,
            distribution={
                "kind": "skills_sh_snapshot",
                "id": canonical_id,
                "files": inventory,
                "responseSha256": digest,
            },
            requirements=_requirements(package),
            unsupported=[
                "registry_snapshot_is_not_an_immutable_git_revision",
                "registry_audits_are_not_local_qualification",
            ],
        )

    def resolve(self, adapter: str, canonical_id: str, version: str) -> RegistryCandidate:
        _version(version)
        result = self.inspect(adapter, canonical_id, version=version)
        if result.version != version:
            raise StateMismatchError(
                "Registry resolution did not retain the exact selected version"
            )
        return result

    def _skills_package(
        self, raw: dict[str, Any], identity: str
    ) -> tuple[SkillPackage, list[dict[str, Any]]]:
        entries = raw.get("files")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
            raise InvalidInputError("skills.sh snapshot requires a bounded exact file list")
        files: dict[str, bytes] = {}
        folded = set()
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("contents"), str):
                raise InvalidInputError("skills.sh snapshot contains unsupported non-text bytes")
            if not isinstance(item.get("path"), str):
                raise InvalidInputError("skills.sh snapshot path must be text")
            path = validate_resource_path(item["path"])
            if path.casefold() in folded:
                raise InvalidInputError("skills.sh snapshot contains colliding paths")
            folded.add(path.casefold())
            files[path] = item["contents"].encode("utf-8")
        package = package_from_files(
            files,
            source={
                "kind": "registry",
                "adapter": "skills_sh",
                "identity": identity,
                "verification": "content-integrity-only",
            },
        )
        inventory = [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(files.items())
        ]
        return package, inventory

    def fetch_skill(self, candidate: RegistryCandidate) -> SkillPackage:
        candidate = RegistryCandidate.from_dict(candidate.to_dict())
        if candidate.kind != "skill" or candidate.version is None or candidate.status != "active":
            raise CapabilityUnavailableError(
                "Only an active exact skill observation can be fetched"
            )
        target = candidate.distribution
        if target["kind"] == "github_skill":
            return fetch_github_skill(target["repository"], target["commit"], target["path"])
        if target["kind"] == "skills_sh_snapshot":
            raw, _ = self._json(
                "skills_sh",
                "https://skills.sh/api/v1/skills/" + quote(candidate.canonical_id, safe="/"),
            )
            if (
                raw.get("id") != candidate.canonical_id
                or raw.get("source") != candidate.publisher["id"]
            ):
                raise StateMismatchError("skills.sh snapshot changed before acquisition")
            package, inventory = self._skills_package(raw, candidate.canonical_id)
            digest = content_hash(
                {"id": candidate.canonical_id, "source": raw["source"], "files": inventory}
            )
            if (
                digest != target["responseSha256"]
                or raw.get("hash") != candidate.manifest["claimedContentHash"]
            ):
                raise StateMismatchError("skills.sh snapshot changed before acquisition")
        elif target["kind"] == "clawhub_skill":
            # Re-resolve the exact owner/version before touching download bytes.
            current = self.resolve("clawhub", candidate.canonical_id, candidate.version)
            if current.sha256 != candidate.sha256:
                raise StateMismatchError("ClawHub metadata changed before acquisition")
            response = self._get(
                "clawhub",
                "https://clawhub.ai/api/v1/download?"
                + urlencode(
                    {
                        "slug": target["slug"],
                        "ownerHandle": target["owner"],
                        "version": target["version"],
                    }
                ),
                maximum=MAX_ARCHIVE_BYTES,
                binary=True,
            )
            media = (
                next((v for k, v in response.headers.items() if k.lower() == "content-type"), "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if media == "application/json":
                handoff = parse_json(response.body, maximum=MAX_CANDIDATE_BYTES)
                if not isinstance(handoff, dict) or handoff.get("sourceRef") != "public-github":
                    raise InvalidInputError("Unsupported ClawHub source handoff")
                repository = handoff.get("repo")
                if isinstance(repository, str) and not repository.startswith("https://"):
                    repository = "https://github.com/" + repository
                repository = github_repository(repository)
                # archiveUrl is deliberately never dereferenced. The existing
                # fixed codeload fetcher independently validates repo/commit/path.
                commit, path = handoff.get("commit"), handoff.get("path")
                if not isinstance(commit, str) or not isinstance(path, str):
                    raise InvalidInputError("ClawHub Git handoff requires an exact commit and path")
                package = fetch_github_skill(repository, commit, path)
            elif media in {
                "application/zip",
                "application/octet-stream",
                "application/x-zip-compressed",
            }:
                package = parse_skill_zip(
                    response.body,
                    source={
                        "kind": "registry",
                        "adapter": "clawhub",
                        "identity": candidate.canonical_id,
                        "version": candidate.version,
                        "candidate_sha256": candidate.sha256,
                    },
                )
            else:
                raise InvalidInputError("ClawHub returned an unsupported download format")
        else:
            raise CapabilityUnavailableError(
                "Registry entry has no supported exact skill distribution"
            )
        expected = {item["path"]: (item["size"], item["sha256"]) for item in target["files"]}
        actual = {
            path: (len(data), hashlib.sha256(data).hexdigest())
            for path, data in package.files.items()
        }
        if actual != expected:
            raise StateMismatchError(
                "Registry skill bytes do not match the selected complete file inventory"
            )
        # Preserve original source evidence and add independently verified exact
        # candidate identity. This changes provenance, not package content hash.
        source = {
            **package.source,
            "registry_adapter": candidate.adapter,
            "registry_identity": candidate.canonical_id,
            "registry_version": candidate.version,
            "registry_candidate_sha256": candidate.sha256,
            "registry_source_sha256": candidate.source_sha256,
        }
        return package_from_files(package.files, source=source)
