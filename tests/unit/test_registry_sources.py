from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from potbot.context.skill_packages import package_from_files
from potbot.controller.registry_sources import (
    RegistryHTTPResponse,
    RegistrySources,
    RegistrySourceUnavailable,
    parse_curated_manifest,
    registry_http_get,
)
from potbot.domain.discovery import RegistryCandidate, RegistryPage, parse_json
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 18, tzinfo=UTC)
SKILL = b"---\nname: demo\ndescription: A portable inspection skill.\n---\nInspect the supplied evidence.\n"
FILES = {"SKILL.md": SKILL, "resources/info.txt": b"exact bytes\n"}


def response(value: Any, status: int = 200, **headers: str) -> RegistryHTTPResponse:
    return RegistryHTTPResponse(
        status, {"Content-Type": "application/json", **headers}, canonical_json(value).encode()
    )


def inventory(files: dict[str, bytes] = FILES) -> list[dict[str, Any]]:
    return [
        {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for path, data in sorted(files.items())
    ]


def archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zip_file:
        for path, data in files.items():
            zip_file.writestr(path, data)
    return stream.getvalue()


def curated() -> tuple[RegistryCandidate, ...]:
    return parse_curated_manifest(
        (ROOT / "examples/registry/curated-capabilities.json").read_bytes()
    )


def updated(candidate: RegistryCandidate, **changes: Any) -> RegistryCandidate:
    raw = candidate.to_dict()
    raw.update(changes)
    raw["manifestSha256"] = content_hash(raw["manifest"])
    return RegistryCandidate.from_dict(raw)


class ClawTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.owner = "alice"
        self.files = dict(FILES)
        self.version = "1.2.3"
        self.download: RegistryHTTPResponse | None = None
        self.detail: dict[str, Any] = {
            "skill": {"slug": "demo", "displayName": "Demo", "summary": "Inspect evidence."},
            "owner": {"handle": self.owner},
            "latestVersion": {"version": self.version},
        }

    def __call__(
        self, url: str, *, headers: dict[str, str], max_bytes: int
    ) -> RegistryHTTPResponse:
        self.calls.append((url, headers))
        parsed = urlsplit(url)
        assert parsed.hostname == "clawhub.ai"
        assert "Authorization" not in headers
        if parsed.path.endswith("/download"):
            assert parse_qs(parsed.query) == {
                "slug": ["demo"],
                "ownerHandle": ["alice"],
                "version": ["1.2.3"],
            }
            return self.download or RegistryHTTPResponse(
                200, {"Content-Type": "application/zip"}, archive(self.files)
            )
        assert parse_qs(parsed.query)["ownerHandle"] == ["alice"]
        if "/versions/" in parsed.path:
            return response(
                {
                    "skill": {"slug": "demo"},
                    "version": {"version": self.version, "files": inventory()},
                }
            )
        return response(self.detail)


def test_curated_twenty_profiles_are_immutable_source_only_and_paginate() -> None:
    values = curated()
    assert len(values) == 20
    assert len({value.capabilities["classes"][0] for value in values}) == 20
    assert all(
        value.manifest["runtimeQualified"] is False
        and value.distribution == {"kind": "inspect_only"}
        for value in values
    )
    for value in values:
        for entry in value.manifest["sourceEvidence"]:
            assert (
                hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
            )
    source = RegistrySources(curated=values, now=lambda: NOW)
    collected = []
    cursor = None
    while True:
        page = source.search("curated", "", cursor, limit=7)
        assert RegistryPage.from_dict(page.to_dict()) == page
        collected.extend(page.candidates)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(collected) == 20
    first = collected[0]
    assert source.resolve("curated", first.canonical_id, first.version) == first
    detached = first.manifest
    detached["runtimeQualified"] = True
    assert first.manifest["runtimeQualified"] is False
    with pytest.raises(FrozenInstanceError):
        first.payload_json = "{}"
    with pytest.raises(CapabilityUnavailableError):
        source.fetch_skill(first)


def test_complete_curated_collection_accepts_its_documented_256_entry_bound() -> None:
    original = json.loads((ROOT / "examples/registry/curated-capabilities.json").read_bytes())
    entries = []
    for index in range(256):
        entry = dict(original["entries"][index % 20])
        entry["canonicalId"] = "fixture." + str(index)
        entries.append(entry)
    parsed = parse_curated_manifest(
        canonical_json({"schema": original["schema"], "entries": entries}).encode()
    )
    assert len(parsed) == 256


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "wrong"),
        ("adapter", []),
        ("kind", {}),
        ("status", []),
        ("version", "latest"),
        ("name", "\u0000bad"),
        ("sourceSha256", "bad"),
        ("sourceUrl", "http://localhost/private"),
    ],
)
def test_candidate_rejects_invalid_identity_and_unknown_authority(field: str, value: Any) -> None:
    raw = curated()[0].to_dict()
    raw[field] = value
    with pytest.raises(InvalidInputError):
        RegistryCandidate.from_dict(raw)
    raw = curated()[0].to_dict()
    raw["executable"] = True
    with pytest.raises(InvalidInputError):
        RegistryCandidate.from_dict(raw)


@pytest.mark.parametrize(
    "requirements",
    [
        [{"kind": "any_binary", "name": "python", "group": "different"}],
        [{"kind": "binary", "name": "python", "group": "any_binary"}],
        [{"kind": "binary", "name": "python"}] * 2,
        [{"kind": [], "name": "python"}],
    ],
)
def test_requirement_groups_match_actual_evaluator(requirements: list[dict[str, Any]]) -> None:
    with pytest.raises(InvalidInputError):
        updated(curated()[0], requirements=requirements)


def test_curated_exact_git_and_existing_recipe_are_the_only_acquisition_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = curated()[0]
    target = {
        "kind": "github_skill",
        "repository": "https://github.com/example/skills",
        "commit": "a" * 40,
        "path": "skills/demo",
    }
    candidate = updated(first, kind="skill", distribution=target)
    calls = []
    monkeypatch.setattr(
        "potbot.controller.registry_sources.fetch_github_skill",
        lambda *args: calls.append(args) or package_from_files(FILES, source={"kind": "github"}),
    )
    result = RegistrySources().fetch_skill(candidate)
    assert result.files == FILES
    assert calls == [(target["repository"], target["commit"], target["path"])]
    with pytest.raises(InvalidInputError):
        updated(first, kind="skill", distribution={**target, "commit": "main"})
    with pytest.raises(InvalidInputError):
        updated(
            first,
            kind="skill",
            distribution={**target, "repository": "https://github.com/../private"},
        )
    with pytest.raises(InvalidInputError):
        updated(
            first,
            distribution={
                "kind": "tool_recipe",
                "recipeId": "not-a-uuid",
                "repository": target["repository"],
                "commit": target["commit"],
            },
        )
    with pytest.raises(InvalidInputError):
        updated(first, distribution={"kind": "shell", "argv": ["sh", "-c", "source instruction"]})


def test_clawhub_owner_identity_and_exact_verified_zip() -> None:
    transport = ClawTransport()
    source = RegistrySources(transport=transport)
    candidate = source.resolve("clawhub", "alice/demo", "1.2.3")
    assert candidate.publisher == {"id": "alice", "evidence": "registry_claim"}
    assert candidate.distribution["files"] == inventory()
    result = source.fetch_skill(candidate)
    assert result.files == FILES
    assert result.source["registry_candidate_sha256"] == candidate.sha256
    assert candidate.to_dict().get("executable") is None
    assert all("install" not in urlsplit(url).path for url, _ in transport.calls)


def test_clawhub_counters_and_latest_tags_do_not_change_selected_exact_release() -> None:
    transport = ClawTransport()
    source = RegistrySources(transport=transport)
    candidate = source.resolve("clawhub", "alice/demo", "1.2.3")
    transport.detail["skill"]["stats"] = {"downloads": 1234}
    transport.detail["latestVersion"] = {"version": "2.0.0"}
    assert source.resolve("clawhub", "alice/demo", "1.2.3").sha256 == candidate.sha256
    assert source.fetch_skill(candidate).files == FILES


@pytest.mark.parametrize("change", ["publisher", "version", "file", "extra"])
def test_clawhub_rejects_identity_metadata_and_complete_file_drift(change: str) -> None:
    transport = ClawTransport()
    source = RegistrySources(transport=transport)
    candidate = source.resolve("clawhub", "alice/demo", "1.2.3")
    if change == "publisher":
        transport.detail["owner"]["handle"] = "mallory"
    elif change == "version":
        transport.version = "2.0.0"
    elif change == "file":
        transport.files["resources/info.txt"] += b"changed"
    else:
        transport.files["install.sh"] = b"unlisted file"
    with pytest.raises(StateMismatchError):
        source.fetch_skill(candidate)


def test_clawhub_git_handoff_never_uses_archive_url(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ClawTransport()
    transport.download = response(
        {
            "sourceRef": "public-github",
            "repo": "example/skills",
            "commit": "a" * 40,
            "path": "skills/demo",
            "contentHash": "b" * 64,
            "archiveUrl": "http://127.0.0.1/secret",
        }
    )
    calls = []
    monkeypatch.setattr(
        "potbot.controller.registry_sources.fetch_github_skill",
        lambda *args: calls.append(args) or package_from_files(FILES, source={"kind": "github"}),
    )
    source = RegistrySources(transport=transport)
    candidate = source.resolve("clawhub", "alice/demo", "1.2.3")
    assert source.fetch_skill(candidate).files == FILES
    assert calls == [("https://github.com/example/skills", "a" * 40, "skills/demo")]


def test_clawhub_search_keeps_same_slug_publishers_distinct() -> None:
    def transport(url: str, **_: Any) -> RegistryHTTPResponse:
        assert urlsplit(url).path == "/api/v1/search"
        return response(
            {
                "results": [
                    {
                        "ownerHandle": owner,
                        "slug": "demo",
                        "displayName": "Demo",
                        "summary": "Inspect evidence",
                        "version": "1.0.0",
                        "score": 9999,
                    }
                    for owner in ("alice", "bob")
                ]
            }
        )

    page = RegistrySources(transport=transport).search("clawhub", "evidence")
    assert {c.canonical_id for c in page.candidates} == {"alice/demo", "bob/demo"}
    assert all(c.distribution == {"kind": "inspect_only"} for c in page.candidates)


def test_clawhub_listing_short_page_still_follows_opaque_cursor() -> None:
    def transport(url: str, **_: Any) -> RegistryHTTPResponse:
        return response({"items": [], "nextCursor": "opaque:not-a-url"})

    page = RegistrySources(transport=transport).search("clawhub", "", limit=25)
    assert page.candidates == () and page.next_cursor == "opaque:not-a-url"
    with pytest.raises(InvalidInputError):
        RegistrySources(transport=transport).search("clawhub", "", cursor="opaque:not-a-url")


def test_mcp_public_exact_version_status_and_packages_stay_inspect_only() -> None:
    requests = []
    item = {
        "server": {
            "name": "io.github.example/weather",
            "description": "Weather metadata",
            "version": "1.10.0",
            "packages": [
                {
                    "registryType": "npm",
                    "identifier": "untrusted",
                    "version": "1.0.0",
                    "runtimeArguments": [{"value": "DO NOT EXECUTE"}],
                    "environmentVariables": [{"name": "API_KEY", "isRequired": True}],
                }
            ],
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {"status": "deprecated", "isLatest": True}
        },
    }

    def transport(url: str, **_: Any) -> RegistryHTTPResponse:
        requests.append(url)
        return response(
            item
            if "/versions/" in urlsplit(url).path
            else {"servers": [item], "metadata": {"nextCursor": None}}
        )

    source = RegistrySources(transport=transport)
    candidate = source.search("mcp_registry", "weather").candidates[0]
    assert candidate.status == "deprecated"
    assert candidate.requirements == [{"kind": "environment", "name": "API_KEY"}]
    assert candidate.distribution == {"kind": "inspect_only"}
    assert (
        source.resolve("mcp_registry", candidate.canonical_id, "1.10.0").sha256 == candidate.sha256
    )
    assert "/io.github.example%2Fweather/versions/1.10.0" in requests[-1]
    with pytest.raises(InvalidInputError):
        source.search("mcp_registry", "xy")
    with pytest.raises(InvalidInputError):
        source.resolve("mcp_registry", candidate.canonical_id, "latest")


def test_skills_sh_without_dedicated_credential_never_uses_legacy_endpoint() -> None:
    def forbidden(*_: Any, **__: Any) -> RegistryHTTPResponse:
        pytest.fail("Missing native token must not initiate a request")

    source = RegistrySources(transport=forbidden)
    result = source.search("skills_sh", "evidence")
    assert result.status == "authentication_required" and not result.candidates
    with pytest.raises(RegistrySourceUnavailable) as error:
        source.inspect("skills_sh", "example/skills/demo")
    assert error.value.status == "authentication_required"


def test_skills_sh_snapshot_verifies_all_text_bytes_without_inventing_git_pin() -> None:
    raw = {
        "id": "example/skills/demo",
        "source": "example/skills",
        "slug": "demo",
        "hash": "f" * 64,
        "files": [{"path": path, "contents": data.decode()} for path, data in FILES.items()],
    }
    calls = []

    def transport(url: str, *, headers: dict[str, str], max_bytes: int) -> RegistryHTTPResponse:
        calls.append(url)
        assert url == "https://skills.sh/api/v1/skills/example/skills/demo"
        assert headers["Authorization"] == "Bearer dedicated-native-token"
        return response(raw)

    source = RegistrySources(transport=transport, skills_sh_token=lambda: "dedicated-native-token")
    candidate = source.inspect("skills_sh", "example/skills/demo")
    assert candidate.version.startswith("snapshot:")
    assert candidate.distribution["kind"] == "skills_sh_snapshot"
    assert candidate.manifest["claimedContentHash"] == "f" * 64
    assert "commit" not in candidate.distribution
    assert "dedicated-native-token" not in candidate.payload_json
    assert source.fetch_skill(candidate).files == FILES
    raw["installs"] = 10000
    raw["files"].reverse()
    assert (
        source.resolve("skills_sh", candidate.canonical_id, candidate.version).sha256
        == candidate.sha256
    )
    assert source.fetch_skill(candidate).files == FILES
    raw["files"][0]["contents"] += "tampered"
    with pytest.raises(StateMismatchError):
        source.fetch_skill(candidate)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication_required"),
        (403, "unavailable"),
        (404, "not_found"),
        (410, "gone"),
        (429, "rate_limited"),
        (503, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_source_failures_are_bounded_and_do_not_reflect_provider_body(
    status: int, expected: str
) -> None:
    source = RegistrySources(
        transport=lambda *_, **__: RegistryHTTPResponse(
            status, {"Retry-After": "15"}, b"secret diagnostic"
        )
    )
    page = source.search("clawhub", "query")
    assert page.status == expected and page.retry_after_seconds == 15
    assert "secret" not in canonical_json(page.to_dict())


@pytest.mark.parametrize(
    "url",
    [
        "http://clawhub.ai/api/v1/skills",
        "https://user@clawhub.ai/api/v1/skills",
        "https://clawhub.ai:443/api/v1/skills",
        "https://127.0.0.1/private",
        "https://clawhub.ai.evil.example/api/v1/search",
    ],
)
def test_transport_rejects_noncanonical_hosts_before_network(url: str) -> None:
    with pytest.raises(InvalidInputError):
        registry_http_get(url, headers={"Accept": "application/json"}, max_bytes=100)


def test_transport_does_not_forward_credentials_to_public_sources() -> None:
    with pytest.raises(InvalidInputError):
        registry_http_get(
            "https://clawhub.ai/api/v1/skills",
            headers={"Authorization": "Bearer private"},
            max_bytes=100,
        )


def test_default_transport_disables_proxies_and_redirects_and_enforces_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.request import HTTPRedirectHandler, ProxyHandler

    handlers = []

    class FakeResponse:
        headers = {"Content-Length": "4", "Content-Type": "application/json"}
        status = 200

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def read1(self, maximum: int) -> bytes:
            return b"{}"

    class Opener:
        def open(self, request: Any, *, timeout: int) -> FakeResponse:
            assert request.full_url == "https://clawhub.ai/api/v1/skills"
            assert timeout == 10
            return FakeResponse()

    def build(*values: Any) -> Opener:
        handlers.extend(values)
        return Opener()

    monkeypatch.setattr("potbot.controller.registry_sources.build_opener", build)
    with pytest.raises(CapabilityUnavailableError, match="byte allowance"):
        registry_http_get(
            "https://clawhub.ai/api/v1/skills", headers={"Accept": "application/json"}, max_bytes=3
        )
    proxy = next(value for value in handlers if isinstance(value, ProxyHandler))
    redirect = next(value for value in handlers if isinstance(value, HTTPRedirectHandler))
    assert proxy.proxies == {}
    with pytest.raises(CapabilityUnavailableError, match="redirect"):
        redirect.redirect_request(None, None, 302, "Found", {}, "http://localhost/secret")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":1e999}',
        b'{"a":"\\u0000"}',
        b'{"a":"\\ud800"}',
        b"[]" * 100,
    ],
)
def test_untrusted_json_rejects_ambiguous_nonfinite_and_invalid_unicode(raw: bytes) -> None:
    with pytest.raises(InvalidInputError):
        parse_json(raw)


def test_registry_page_cannot_carry_stale_success_on_an_outage() -> None:
    page = (
        RegistrySources(curated=curated(), now=lambda: NOW).search("curated", "", limit=1).to_dict()
    )
    page["status"] = "unavailable"
    with pytest.raises(InvalidInputError):
        RegistryPage.from_dict(page)
