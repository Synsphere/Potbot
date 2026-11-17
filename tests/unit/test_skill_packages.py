"""Byte identity, source isolation and hostile archive/frontmatter boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

from potbot.context import skill_packages as packages
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

SKILL = b"---\nname: example-skill\ndescription: A portable example.\nlicense: MIT\n---\nRead references/guide.md.\n"


def archive(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as value:
        for path, data in files.items():
            value.writestr(path, data)
    return out.getvalue()


def test_original_bytes_complete_release_identity_and_source_independence() -> None:
    files = {
        "SKILL.md": SKILL.replace(b"\n", b"\r\n"),
        "references/guide.md": b"Original guide\n",
        "assets/table.bin": b"\x00\xff",
    }
    first = packages.parse_skill_zip(archive(files))
    second = packages.package_from_files(files, source={"kind": "other-reviewable-source"})
    changed = packages.package_from_files(
        {**files, "assets/table.bin": b"\x00\xfe"}, source={"kind": "zip"}
    )
    assert first.content.encode() == files["SKILL.md"]
    assert first.content_sha256 == hashlib.sha256(files["SKILL.md"]).hexdigest()
    assert first.release_sha256 == second.release_sha256
    assert first.release_sha256 != changed.release_sha256
    assert first.content_sha256 == changed.content_sha256
    assert first.manifest["compatibility"]["status"] == "portable"
    assert first.files == files
    assert packages.verify_package(first.manifest, first.files, source=first.source) == first
    with pytest.raises(InvalidInputError, match="immutable manifest"):
        packages.verify_package(first.manifest, changed.files, source=first.source)


def test_yaml_block_descriptions_vendor_requirements_and_loss_report() -> None:
    skill = b"""---
name: example-skill
description: >-
  Parse a package and
  inspect its resources.
allowed-tools: Read Bash(python:*)
context: fork
metadata:
  author: Example
  openclaw:
    requires:
      bins: [python]
      anyBins: [uv, pip]
      env: [SERVICE_TOKEN]
      config: [browser.enabled]
    os: [linux]
    install:
      - kind: uv
        package: dangerous-if-executed
    primaryEnv: SERVICE_TOKEN
---
Untrusted instructions. Do not interpret them during acquisition.
"""
    result = packages.parse_skill_zip(
        archive(
            {
                "download/SKILL.md": skill,
                "download/AGENTS.md": b"Not live Context",
                "download/scripts/check.py": b"raise RuntimeError('not executed')",
            }
        )
    )
    assert result.description == "Parse a package and inspect its resources."
    assert result.source["path"] == "download"
    requirements = {(item["kind"], item["name"]) for item in result.manifest["requirements"]}
    assert ("binary", "python") in requirements
    assert ("environment", "SERVICE_TOKEN") in requirements
    assert ("operating_system", "linux") in requirements
    assert ("tool", "Bash(python:*)") in requirements
    assert result.manifest["compatibility"]["status"] == "review_required"
    assert any("installers" in item for item in result.manifest["compatibility"]["unsupported"])
    assert (
        "Uninterpreted frontmatter field: context"
        in result.manifest["compatibility"]["unsupported"]
    )
    assert {item["code"] for item in result.manifest["findings"]} >= {
        "scripts_data_only",
        "context_files_are_resources",
        "requirements_unverified",
    }
    assert result.files["AGENTS.md"] == b"Not live Context"
    assert all(item["status"] == "unverified" for item in result.manifest["requirements"])


@pytest.mark.parametrize(
    "metadata",
    [
        "name: example-skill\nname: replaced\ndescription: duplicate",
        "name: example-skill\ndescription: !!python/object/apply:os.system ['false']",
        "name: example-skill\ndescription: &anchor a\nmetadata: {alias: *anchor}",
        "name: example-skill\ndescription: valid\nmetadata: {duplicate: first, duplicate: last}",
        "name: example-skill\ndescription: valid\nmetadata: {date: 2026-01-01}",
        "name: example-skill\ndescription: valid\nmetadata: {number: .inf}",
        "name: example-skill\ndescription: ''",
        "name: Example-Skill\ndescription: invalid name",
        "name: example--skill\ndescription: invalid name",
        "name: example-skill\ndescription: valid\ncompatibility: [invalid]",
        "name: example-skill\ndescription: valid\nmetadata: {openclaw: {requires: {env: [{bad: entry}]}}}",
    ],
)
def test_malformed_or_ambiguous_metadata_is_rejected(metadata: str) -> None:
    with pytest.raises(InvalidInputError):
        packages.package_from_files(
            {"SKILL.md": f"---\n{metadata}\n---\nbody\n".encode()}, source={"kind": "zip"}
        )


@pytest.mark.parametrize("name", ["a", "3d-model", "a" * 64])
def test_portable_name_bounds_are_independent_of_authored_skill_names(name: str) -> None:
    data = f"---\nname: {name}\ndescription: valid\n---\nbody\n".encode()
    assert packages.package_from_files({"SKILL.md": data}, source={}).name == name


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a//b",
        "a/./b",
        "a/../b",
        "C:/drive",
        "a\\b",
        "a\x00b",
        "a\nb",
        "a/CON.txt",
        ".git/config",
        "a/with-dot.",
        "a/with-space ",
        "a/e\u0301",
        "a/" + "x" * 129,
    ],
)
def test_resource_paths_fail_closed_without_normalization(path: str) -> None:
    with pytest.raises(InvalidInputError):
        packages.validate_resource_path(path)


@pytest.mark.parametrize(
    "files",
    [
        {"SKILL.md": SKILL, "assets/Foo": b"a", "assets/foo": b"b"},
        {"SKILL.md": SKILL, "assets": b"a", "assets/foo": b"b"},
        {"SKILL.md": SKILL, "ASSETS": b"a", "assets/foo": b"b"},
        {"skill.md": SKILL},
        {"SKILL.md": b"\xff"},
    ],
)
def test_file_aliases_collisions_and_invalid_text_fail(files: dict[str, bytes]) -> None:
    with pytest.raises(InvalidInputError):
        packages.parse_skill_zip(archive(files))


def test_zip_symlink_duplicates_and_unsafe_paths_are_never_extracted(tmp_path: Path) -> None:
    for member in ("../outside", "assets/link", "SKILL.md"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as value:
            value.writestr("SKILL.md", SKILL)
            item = zipfile.ZipInfo(member)
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            with pytest.warns(UserWarning) if member == "SKILL.md" else _no_warning():
                value.writestr(item, "../../outside")
        with pytest.raises(InvalidInputError):
            packages.parse_skill_zip(buffer.getvalue())
    assert not (tmp_path / "outside").exists()


def _no_warning() -> Any:
    from contextlib import nullcontext

    return nullcontext()


def test_compressed_and_expanded_file_and_package_budgets() -> None:
    with pytest.raises(InvalidInputError, match="compressed"):
        packages.parse_skill_zip(b"x" * (packages.MAX_ARCHIVE_BYTES + 1))
    with pytest.raises(InvalidInputError, match="1 MiB"):
        packages.parse_skill_zip(
            archive({"SKILL.md": SKILL, "huge.bin": b"x" * (packages.MAX_FILE_BYTES + 1)})
        )
    with pytest.raises(InvalidInputError, match="expanded"):
        packages.parse_skill_zip(
            archive(
                {
                    "SKILL.md": SKILL,
                    **{f"assets/{i}.bin": b"x" * packages.MAX_FILE_BYTES for i in range(8)},
                }
            )
        )
    with pytest.raises(InvalidInputError, match="expanded"):
        packages.parse_skill_zip(
            archive({"SKILL.md": SKILL, **{f"assets/{i}": b"x" for i in range(packages.MAX_FILES)}})
        )


def test_explicit_subdirectory_selects_one_skill_and_keeps_exact_source() -> None:
    raw = archive({"repo/skills/example-skill/SKILL.md": SKILL, "repo/README.md": b"unselected"})
    result = packages.parse_skill_zip(raw, subdirectory="repo/skills/example-skill")
    assert set(result.files) == {"SKILL.md"}
    assert result.source["archive_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result.source["path"] == "repo/skills/example-skill"
    with pytest.raises(InvalidInputError):
        packages.parse_skill_zip(raw, subdirectory="../elsewhere")


def test_local_folder_rejects_linked_resources_and_packages_original_bytes(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_bytes(SKILL)
    assert packages.parse_skill_zip(packages.bundle_skill_directory(root)).content.encode() == SKILL
    (root / "linked").symlink_to(root / "SKILL.md")
    with pytest.raises((InvalidInputError, OSError)):
        packages.bundle_skill_directory(root)


@pytest.mark.parametrize(
    "url,commit,path",
    [
        ("http://github.com/a/b", "a" * 40, ""),
        ("https://github.com.evil.invalid/a/b", "a" * 40, ""),
        ("https://token@github.com/a/b", "a" * 40, ""),
        ("https://github.com/a/b.git", "a" * 40, ""),
        ("https://github.com/a/b?token=secret", "a" * 40, ""),
        ("https://github.com/a/b", "main", ""),
        ("https://github.com/a/b", "a" * 40, "../private"),
    ],
)
def test_git_source_has_no_arbitrary_host_authentication_or_moving_reference(
    url: str, commit: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid source must not make a request")

    monkeypatch.setattr(packages, "build_opener", forbidden)
    with pytest.raises(InvalidInputError):
        packages.fetch_github_skill(url, commit, path)


def test_git_source_fetches_only_exact_fixed_host_without_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    raw = archive({f"repository-{commit}/skills/example-skill/SKILL.md": SKILL})
    observed: list[Any] = []

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(raw))}

    class Opener:
        def open(self, request: Any, timeout: int) -> Response:
            observed.append((request.full_url, request.headers, timeout))
            return Response(raw)

    def opener(*handlers: Any) -> Opener:
        assert any(isinstance(item, packages._NoRedirect) for item in handlers)
        assert any(getattr(item, "proxies", None) == {} for item in handlers)
        return Opener()

    monkeypatch.setattr(packages, "build_opener", opener)
    result = packages.fetch_github_skill(
        "https://github.com/example/repository", commit, "skills/example-skill"
    )
    assert observed[0][0] == f"https://codeload.github.com/example/repository/zip/{commit}"
    assert set(observed[0][1]) == {"Accept", "User-agent"}
    assert result.source["commit"] == commit
    assert result.source["requested_path"] == "skills/example-skill"
    assert result.files == {"SKILL.md": SKILL}
    assert json.loads(packages.canonical_json(result.manifest))["name"] == "example-skill"


def test_git_network_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failed:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise packages.URLError("Do not leak internal network error")

    monkeypatch.setattr(packages, "build_opener", lambda *args: Failed())
    with pytest.raises(CapabilityUnavailableError, match="Unable to retrieve") as error:
        packages.fetch_github_skill("https://github.com/a/b", "f" * 40)
    assert "internal" not in str(error.value)


def test_metadata_numbers_are_jsonb_stable_and_keys_are_valid_unicode() -> None:
    metadata = b"---\nname: numbers\ndescription: Numeric metadata\nmetadata: {zero: -0.0, exponent: 1.25e-7, integral: 12.0}\n---\n"
    package = packages.package_from_files({"SKILL.md": metadata}, source={})
    assert package.manifest["metadata"]["metadata"] == {
        "zero": 0,
        "exponent": 1.25e-7,
        "integral": 12,
    }
    for value in (
        "{big: 9007199254740992}",
        "{big: 1.0e+20}",
        '{"bad\\0key": value}',
        '{"bad\\uD800": value}',
    ):
        content = (
            f"---\nname: numbers\ndescription: Numeric metadata\nmetadata: {value}\n---\n".encode()
        )
        with pytest.raises(InvalidInputError):
            packages.package_from_files({"SKILL.md": content}, source={})


def test_explicit_zip_file_rejects_fifo_links_and_oversize_before_reading(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    raw = archive({"SKILL.md": SKILL})
    source.write_bytes(raw)
    assert packages.read_skill_archive(source) == raw
    linked = tmp_path / "linked.zip"
    linked.symlink_to(source)
    with pytest.raises(InvalidInputError):
        packages.read_skill_archive(linked)
    fifo = tmp_path / "not-a-zip"
    os.mkfifo(fifo)
    with pytest.raises(InvalidInputError, match="regular file"):
        packages.read_skill_archive(fifo)
    hardlink = tmp_path / "hardlink.zip"
    os.link(source, hardlink)
    with pytest.raises(InvalidInputError, match="regular file"):
        packages.read_skill_archive(source)
