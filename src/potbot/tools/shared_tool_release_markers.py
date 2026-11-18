"""Host-only denial projections for S12; PostgreSQL owns release decisions.

A hold is written before an installation can change. Its presence never grants
execution, including after restart. An absent authority masks the affected tool.
Unchanged installations retain their existing admission, without auto-enrolling
or disabling them. The private metadata is never mounted into a worker.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash

if TYPE_CHECKING:
    from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
    from potbot.tools.workspace_supervision import WorkspaceLeaseGuard

DIRECTORY = ".potbot-s12-release-holds"
_SLUG = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _directory(root: Path, *, create: bool = False) -> Path:
    parent = root / DIRECTORY
    if create:
        parent.mkdir(mode=0o700, exist_ok=True)
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ScopeDeniedError("S12 release holds require an operator-private directory")
    if create:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return parent


def validate_hold(value: object, slug: str) -> dict[str, Any]:
    fields = {
        "schema",
        "slug",
        "workspace_id",
        "agent_id",
        "operation_id",
        "direction",
        "definition_sha256",
        "commit",
        "identity_sha256",
        "image_digest",
        "qualification_sha256",
        "test_evidence_sha256",
        "suite_sha256",
        "entrypoints",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or (
            value.get("schema") != "potbot-tool-release-hold/1"
            or value.get("slug") != slug
            or not _SLUG.fullmatch(slug)
            or value.get("direction") not in {"activate", "rollback", "withdraw"}
            or not isinstance(value.get("commit"), str)
            or re.fullmatch(r"[a-f0-9]{40}", value["commit"]) is None
        )
    ):
        raise StateMismatchError("S12 release hold is malformed")
    try:
        for field in ("workspace_id", "agent_id", "operation_id"):
            if str(UUID(value[field])) != value[field]:
                raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise StateMismatchError("S12 release hold identity is invalid") from None
    for field in fields - {
        "schema",
        "slug",
        "workspace_id",
        "agent_id",
        "operation_id",
        "direction",
        "commit",
        "entrypoints",
    }:
        if not isinstance(value[field], str) or not _DIGEST.fullmatch(value[field]):
            raise StateMismatchError("S12 release hold requires exact digests")
    pins = value["entrypoints"]
    if not isinstance(pins, dict) or not pins or len(pins) > 48:
        raise StateMismatchError("S12 release requires at least one pinned executable")
    prefix = f"/usr/local/tools/{slug}/"
    for path, digest in pins.items():
        if (
            not isinstance(path, str)
            or not path.startswith(prefix)
            or path.removeprefix(prefix).split("/", 1)[0] not in {"static", "environment"}
            or any(part in {"", ".", ".."} for part in path[1:].split("/"))
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise StateMismatchError("S12 release entrypoint is invalid")
    return value


def read_hold(root: Path, slug: str) -> dict[str, Any] | None:
    if not _SLUG.fullmatch(slug):
        raise ScopeDeniedError("S12 requires a canonical tool slug")
    if not (root / DIRECTORY).exists() and not (root / DIRECTORY).is_symlink():
        return None
    parent = _directory(root)
    path = parent / (slug + ".json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 32768
        ):
            raise ScopeDeniedError("S12 release hold is not a private regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise StateMismatchError("S12 release hold exceeds its bound")
        value = json.loads(raw)
        if canonical(value).encode() != raw:
            raise StateMismatchError("S12 release hold is not canonical")
        return validate_hold(value, slug)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise StateMismatchError("S12 release hold is unreadable") from exc
    finally:
        os.close(fd)


def write_hold(root: Path, slug: str, value: dict[str, Any] | None) -> None:
    """Called only under the operator's exclusive per-tool maintenance lease."""
    if not _SLUG.fullmatch(slug):
        raise ScopeDeniedError("S12 requires a canonical tool slug")
    parent = _directory(root, create=True)
    target = parent / (slug + ".json")
    if value is None:
        target.unlink(missing_ok=True)
    else:
        validate_hold(value, slug)
        raw = canonical(value).encode()
        if len(raw) > 32768:
            raise StateMismatchError("S12 release hold exceeds its bound")
        temporary = parent / (".pending-" + uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, target)
        finally:
            os.close(fd)
            temporary.unlink(missing_ok=True)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class ToolMaintenanceProbe:
    """A bounded exception for one admitted test while its writer lease is live."""

    guard: WorkspaceLeaseGuard
    directory: str
    hold_sha256: str | None = None

    def admits(self, root: Path, name: str, hold: dict[str, Any] | None) -> bool:
        if name != self.directory:
            return False
        from potbot.tools.workspace_supervision import WorkspaceLeaseGuard

        if not isinstance(self.guard, WorkspaceLeaseGuard):
            raise ScopeDeniedError("S12 maintenance probe requires its owning lease")
        self.guard.ensure_live()
        request = self.guard.request
        if hold is not None:
            if (
                hold["direction"] == "withdraw"
                or self.hold_sha256 is None
                or request.operation != "tool.postflight"
                or str(request.workspace_id) != hold["workspace_id"]
                or str(request.actor_id) != hold["agent_id"]
                or request.image_digest != hold["image_digest"]
                or content_hash(hold) != self.hold_sha256
                or read_hold(root, hold["slug"]) != hold
            ):
                raise StateMismatchError("S12 postflight binding changed")
        elif (
            self.hold_sha256 is not None
            or request.operation != "tool.test_upgrade"
            or re.fullmatch(r"\.potbot-s9-stage-[a-z][a-z0-9-]{1,62}-[a-f0-9]{32}", name) is None
        ):
            raise ScopeDeniedError("S12 staged probe requires its exact S9 test lease")
        resources = request.resources
        slug = (
            hold["slug"]
            if hold is not None
            else name.split(".potbot-s9-stage-", 1)[-1].rsplit("-", 1)[0]
        )
        if not any(
            r.kind == "shared_tool" and r.name == slug and r.mode == "write" for r in resources
        ) or not any(
            r.kind == "shared_tool" and r.name == "*" and r.mode == "read" for r in resources
        ):
            raise ScopeDeniedError("S12 maintenance probe has no matching exclusive tool lease")
        return True


def blocked_directories(runtime: SharedWorkspaceRuntime) -> tuple[str, ...]:
    """Mask affected code even when the requested executable is an interpreter."""
    root = runtime.shared_tools
    if root is None:
        return ()
    blocked: set[str] = set()
    probe = runtime.tool_maintenance_probe
    if probe is not None and not isinstance(probe, ToolMaintenanceProbe):
        raise ScopeDeniedError("S12 maintenance probe is not an operator capability")
    for child in root.iterdir():
        if child.name.startswith(".potbot-s10-incoming-"):
            # Interrupted descriptor-copy residue is never a runnable stage.
            # Releasing a reconciled lease must not expose it to interpreters.
            blocked.add(child.name)
        if child.name.startswith(".potbot-s9-stage-"):
            if probe is None or not probe.admits(root, child.name, None):
                blocked.add(child.name)
    if (root / DIRECTORY).exists() or (root / DIRECTORY).is_symlink():
        parent = _directory(root)
        blocked.add(DIRECTORY)
        for path in parent.iterdir():
            if path.name.startswith(".pending-"):
                continue  # Incomplete projection temp never replaced the committed hold.
            slug = path.name.removesuffix(".json")
            if not path.name.endswith(".json") or not _SLUG.fullmatch(slug):
                raise StateMismatchError("Unknown S12 release projection needs reconciliation")
            hold = read_hold(root, slug)
            if hold is None:
                raise StateMismatchError("S12 release hold disappeared during admission")
            if hold["direction"] == "withdraw":
                # An absence receipt never grants permission to execute the
                # retained removed tree, even under a postflight capability.
                blocked.add(slug)
                continue
            if probe is not None and probe.admits(root, slug, hold):
                continue
            authority = runtime.tool_release_authority
            allowed = False
            if authority is not None:
                from potbot.tools.tool_release_gate_s12 import ToolReleaseAuthority

                if not isinstance(authority, ToolReleaseAuthority):
                    raise ScopeDeniedError("S12 release requires the PostgreSQL operator authority")
                try:
                    allowed = authority.admits(hold, runtime)
                except Exception:
                    allowed = False  # An outage or invalid evidence cannot expose changed code.
            if not allowed:
                blocked.add(slug)
    return tuple(sorted(blocked))
