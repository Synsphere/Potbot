"""Immutable operator recipes for the existing offline S10/S9 build boundary."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.shared_tool_store import SharedToolDefinition
from potbot.tools.shared_tool_upgrades import ToolTestCase, ToolTestSuite
from potbot.tools.tool_sources import source_identity
from potbot.tools.tool_stage_s10 import command_digest

SCHEMA = "potbot-tool-acquisition-recipe/v1"
MAX_RECIPE_BYTES = 32_768
_AGENT = re.compile(r"(?:[a-z][a-z0-9_-]{1,62}|[0-9a-f]{64})\Z")
_TOKENS = ("{build}", "{checkout}", "{tool}", "{agent_home}")


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\0" in value:
        raise InvalidInputError("Operator recipe text is outside its bounded contract")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InvalidInputError("Operator recipe text must be UTF-8") from None
    return value


def _paths(value: object, minimum: int, maximum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or any(not isinstance(path, str) for path in value)
        or len(set(value)) != len(value)
    ):
        raise InvalidInputError("Operator recipe paths must be distinct bounded arrays")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ToolRecipe:
    slug: str
    name: str
    description: str
    repository: str
    build_argv: tuple[str, ...]
    timeout_ms: int
    cleanup_paths: tuple[str, ...]
    lockfile_paths: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    tests: ToolTestSuite

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ToolRecipe:
        fields = {
            "schema",
            "slug",
            "name",
            "description",
            "repository",
            "build_argv",
            "timeout_ms",
            "cleanup_paths",
            "lockfile_paths",
            "artifact_paths",
            "tests",
        }
        if not isinstance(value, dict) or set(value) != fields or value.get("schema") != SCHEMA:
            raise InvalidInputError("Invalid bounded operator tool recipe")
        try:
            encoded = canonical_json(value).encode("utf-8")
        except UnicodeError:
            raise InvalidInputError("Operator recipe must contain UTF-8 text") from None
        if len(encoded) > MAX_RECIPE_BYTES:
            raise InvalidInputError("Invalid bounded operator tool recipe")
        slug, repository = _text(value["slug"], 63), _text(value["repository"], 512)
        source_identity(repository, "a" * 40)
        cleanup = _paths(value["cleanup_paths"], 1, 64)
        locks = _paths(value["lockfile_paths"], 0, 24)
        artifacts = _paths(value["artifact_paths"], 1, 48)
        # Reuse the installed-definition path, namespace and cleanup rules.
        SharedToolDefinition(
            slug,
            repository,
            "a" * 40,
            cleanup,
            tuple((path, "a" * 64) for path in locks),
            False,
            tuple((path, "a" * 64) for path in artifacts),
        )
        argv = value["build_argv"]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(not isinstance(arg, str) or not arg or len(arg.encode()) > 4096 for arg in argv)
        ):
            raise InvalidInputError("Operator recipe requires a bounded exact argv")
        command_digest(tuple(argv))
        for arg in argv:
            rendered = arg
            for token in _TOKENS:
                rendered = rendered.replace(token, "")
            if "{" in rendered or "}" in rendered:
                raise InvalidInputError("Operator recipe contains an unknown build placeholder")
        if any(token in argv[0] for token in _TOKENS):
            raise InvalidInputError("The builder must remain a pinned rootfs executable")
        timeout = value["timeout_ms"]
        if type(timeout) is not int or not 100 <= timeout <= 900_000:
            raise InvalidInputError("Operator build timeout is outside policy")
        tests = value["tests"]
        if not isinstance(tests, list) or not 2 <= len(tests) <= 16:
            raise InvalidInputError("Operator recipe requires real smoke and unit tests")
        cases = []
        for test in tests:
            if (
                not isinstance(test, dict)
                or set(test) != {"name", "kind", "argv", "timeout_ms"}
                or not isinstance(test["name"], str)
                or not isinstance(test["kind"], str)
                or not isinstance(test["argv"], list)
            ):
                raise InvalidInputError("Invalid operator recipe test case")
            cases.append(
                ToolTestCase(test["name"], test["kind"], tuple(test["argv"]), test["timeout_ms"])
            )
        return cls(
            slug,
            _text(value["name"], 120),
            _text(value["description"], 4096),
            repository,
            tuple(argv),
            timeout,
            cleanup,
            locks,
            artifacts,
            ToolTestSuite(tuple(cases)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "repository": self.repository,
            "build_argv": list(self.build_argv),
            "timeout_ms": self.timeout_ms,
            "cleanup_paths": list(self.cleanup_paths),
            "lockfile_paths": list(self.lockfile_paths),
            "artifact_paths": list(self.artifact_paths),
            "tests": [case.public() for case in self.tests.cases],
        }

    @property
    def digest(self) -> str:
        return content_hash(self.to_dict())

    def render(self, *, operation_id: UUID, agent_directory: str) -> dict[str, Any]:
        if (
            not isinstance(operation_id, UUID)
            or not isinstance(agent_directory, str)
            or not _AGENT.fullmatch(agent_directory)
        ):
            raise InvalidInputError(
                "Build rendering requires an exact operation and registered Agent directory"
            )
        home = "/home/" + agent_directory
        build = home + "/builds/" + operation_id.hex
        replacements = {
            "{build}": build,
            "{tool}": build,
            "{checkout}": build + "/checkout",
            "{agent_home}": home,
        }
        arguments = []
        for arg in self.build_argv:
            for key, target in replacements.items():
                arg = arg.replace(key, target)
            if len(arg.encode()) > 4096:
                raise InvalidInputError("Rendered build command exceeds its argument allowance")
            arguments.append(arg)
        return {
            "agent_directory": agent_directory,
            "build_root": build,
            "build_argv": arguments,
            "command_sha256": command_digest(tuple(arguments)),
        }


def load_tool_recipe(path: Path) -> ToolRecipe:
    """Read one bounded, private operator file without following redirected paths."""
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.resolve(strict=True) != path
    ):
        raise ScopeDeniedError("Tool recipe must be an exact private operator file")
    before = path.stat(follow_symlinks=False)
    if (
        before.st_uid != os.geteuid()
        or before.st_mode & 0o077
        or before.st_nlink != 1
        or before.st_size > MAX_RECIPE_BYTES
    ):
        raise ScopeDeniedError("Tool recipe must be an operator-owned private regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ScopeDeniedError("Tool recipe changed before it was read")
        data = bytearray()
        while piece := os.read(descriptor, MAX_RECIPE_BYTES + 1):
            data.extend(piece)
            if len(data) > MAX_RECIPE_BYTES:
                raise InvalidInputError("Tool recipe exceeds its byte allowance")
        after = os.fstat(descriptor)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ScopeDeniedError("Tool recipe changed while it was read")
    finally:
        os.close(descriptor)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidInputError("Duplicate operator recipe field")
            result[key] = value
        return result

    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidInputError("Tool recipe must contain bounded UTF-8 JSON") from None
    return ToolRecipe.from_dict(parsed)
