"""Bounded metadata-only inventory of native-operator-selected project roots.

No model loading, package imports, executable/version probes, subprocesses,
credential reads, directory discovery or outbound requests occur here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import content_hash

_SKIP = {".git", "node_modules", "__pycache__", ".cache", "build", "dist", "target"}
_MODEL_SUFFIXES = (".gguf", ".safetensors", ".onnx", ".bin")
MAX_ENTRIES = 2000
MAX_ITEMS = 100
MAX_SECONDS = 2.0


def _text(value: Any, maximum: int = 120) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise InvalidInputError("Inventory labels must be bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvalidInputError("Inventory labels cannot contain control characters")
    return value


def _relative(value: Any) -> str:
    value = _text(value, 240)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or ":" in value
        or any(part in {".", ".."} for part in path.parts)
        or not path.parts
    ):
        raise InvalidInputError("Inventory locations must be project-relative")
    return str(value)


def _link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


@dataclass(frozen=True)
class InventoryProfile:
    workspace_id: UUID
    project_root: Path
    node_label: str
    model_roots: tuple[str, ...]
    tool_roots: tuple[str, ...]

    @classmethod
    def read(cls, path: Path, workspace_id: UUID) -> InventoryProfile:
        try:
            with path.open("rb") as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                raise ValueError

            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                obj: dict[str, Any] = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError
                    obj[key] = value
                return obj

            value = json.loads(raw, object_pairs_hook=unique)
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "schema",
                    "workspace_id",
                    "project_root",
                    "node_label",
                    "model_roots",
                    "tool_roots",
                }
                or value["schema"] != "potbot-local-inventory/v1"
            ):
                raise ValueError
            if value["workspace_id"] != str(workspace_id):
                raise ScopeDeniedError("Inventory profile belongs to another workspace")
            root = Path(_text(value["project_root"], 4096))
            if not root.is_absolute() or not root.is_dir() or _link(root.lstat()):
                raise ValueError
            roots: list[tuple[str, ...]] = []
            for name in ("model_roots", "tool_roots"):
                items = value[name]
                if not isinstance(items, list) or len(items) > 8 or len(set(items)) != len(items):
                    raise ValueError
                roots.append(tuple(_relative(item) for item in items))
            if any(PurePosixPath(item).parts[0] != "models" for item in roots[0]):
                raise ValueError
            return cls(workspace_id, root.resolve(), _text(value["node_label"]), *roots)
        except ScopeDeniedError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise InvalidInputError("Local inventory profile is invalid or unavailable") from None

    @property
    def fingerprint(self) -> str:
        # Paths are part of the private configuration identity, never the public projection.
        return content_hash(
            {
                "workspace_id": str(self.workspace_id),
                "project_root": str(self.project_root),
                "node_label": self.node_label,
                "model_roots": self.model_roots,
                "tool_roots": self.tool_roots,
            }
        )


def _metadata(path: Path) -> tuple[str, str] | None:
    """Read only a bounded regular METADATA file, never a FIFO or symlink."""
    info = path.lstat()
    if _link(info) or not stat.S_ISREG(info.st_mode):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            return None
        raw = os.read(descriptor, 16384)
    finally:
        os.close(descriptor)
    # Only standard public distribution headers, not arbitrary metadata/body text.
    lines = (
        raw.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n\n", 1)[0].splitlines()
    )
    names = [line[6:].strip() for line in lines if line.startswith("Name: ")]
    versions = [line[9:].strip() for line in lines if line.startswith("Version: ")]
    if len(names) != 1 or len(versions) != 1:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", names[0]) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,79}", versions[0]
    ):
        return None
    return names[0], versions[0]


def inspect_local(profile: InventoryProfile | None, kind: str) -> dict[str, Any]:
    if kind not in {"models", "tools"}:
        raise InvalidInputError("Unknown local inventory kind")
    if profile is None:
        return {
            "status": "not_configured",
            "nodeLabel": "This execution backend",
            "profileSha256": None,
            "items": [],
            "truncated": False,
            "unavailableRoots": 0,
        }
    roots = profile.model_roots if kind == "models" else profile.tool_roots
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    visited = 0
    unavailable = 0
    truncated = False
    deadline = time.monotonic() + MAX_SECONDS

    def safe_root(relative: str) -> Path:
        current = profile.project_root
        for part in PurePosixPath(relative).parts:
            current = current / part
            info = current.lstat()
            if _link(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError("Unusable inventory root")
        return current

    def walk(directory: Path, depth: int) -> None:
        nonlocal visited, truncated, unavailable
        if depth > 4:
            truncated = True
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if (
                        visited >= MAX_ENTRIES
                        or len(items) >= MAX_ITEMS
                        or time.monotonic() >= deadline
                    ):
                        truncated = True
                        return
                    visited += 1
                    if entry.name in _SKIP:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if _link(info):
                            continue
                        path = Path(entry.path)
                        if stat.S_ISDIR(info.st_mode):
                            if kind == "tools" and entry.name.endswith(".dist-info"):
                                metadata = _metadata(path / "METADATA")
                                if metadata:
                                    add(path, metadata[0], metadata[1], "package_metadata", None)
                                else:
                                    unavailable += 1
                            elif kind == "models":
                                walk(path, depth + 1)
                        elif kind == "models" and stat.S_ISREG(info.st_mode):
                            lower = entry.name.lower()
                            partial = lower.endswith((".part", ".partial", ".incomplete"))
                            if (
                                lower.endswith(_MODEL_SUFFIXES)
                                or partial
                                and any(suffix in lower for suffix in _MODEL_SUFFIXES)
                            ):
                                add(
                                    path,
                                    entry.name,
                                    "",
                                    "partial_file" if partial else "local_file",
                                    info.st_size,
                                )
                    except (OSError, InvalidInputError):
                        unavailable += 1
        except OSError:
            unavailable += 1

    def add(path: Path, name: str, version: str, origin: str, size: int | None) -> None:
        relative = path.relative_to(profile.project_root).as_posix()
        identity = hashlib.sha256((kind + ":" + relative).encode()).hexdigest()
        if identity in seen:
            return
        seen.add(identity)
        items.append(
            {
                "id": "local:" + identity,
                "name": _text(name, 240),
                "version": version,
                "origin": origin,
                "location": _text(relative, 512),
                "bytes": size,
                "presence": "partial" if origin == "partial_file" else "observed",
                "verification": "not_checked",
                "qualification": "unknown",
                "runtime": "unknown",
                "permission": "not_evaluated",
                "detail": "Package metadata present; import/execution not checked."
                if origin == "package_metadata"
                else "File observed; identity and loading not checked.",
            }
        )

    for relative in roots:
        if visited >= MAX_ENTRIES or len(items) >= MAX_ITEMS or time.monotonic() >= deadline:
            truncated = True
            break
        try:
            walk(safe_root(relative), 0)
        except OSError:
            unavailable += 1
    return {
        "status": "partial" if unavailable or truncated else "observed",
        "nodeLabel": profile.node_label,
        "profileSha256": profile.fingerprint,
        "items": sorted(items, key=lambda item: (item["name"].lower(), item["id"])),
        "truncated": truncated,
        "unavailableRoots": unavailable,
    }
