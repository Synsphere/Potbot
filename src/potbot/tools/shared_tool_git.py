"""Validate builder-writable Git data before controller-side inspection.

Git is a trusted reader, but repository configuration, attributes, object
redirection and index flags are untrusted inputs. No repository-defined host
command is needed to verify a pinned tool checkout.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from potbot.domain.errors import ScopeDeniedError, StateMismatchError

_OBJECT = re.compile(r"[a-f0-9]{40}\Z")
_REF = re.compile(r"[A-Za-z0-9_./-]{1,240}\Z")
_MAX_CONTROL_ENTRIES = 40_000
_MAX_CONTROL_BYTES = 512 * 1024**2
_MAX_SOURCE_ENTRIES = 50_000
_MAX_SOURCE_BYTES = 1024**3
_MAX_SOURCE_FILE = 128 * 1024**2
_FORBIDDEN = frozenset(
    {
        "commondir",
        "gitdir",
        "config.worktree",
        "worktrees",
        "refs/replace",
        "info/attributes",
        "info/grafts",
        "objects/info/alternates",
        "objects/info/http-alternates",
    }
)


def _config(raw: bytes) -> None:
    """Accept only inert configuration needed by standalone cloned checkouts.

    This deliberately small grammar rejects includes, filters, path redirects,
    continuations and unknown sections before starting Git. Operator-created
    checkouts and ordinary local edit clones need no executable configuration.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ScopeDeniedError("Shared tool Git configuration must be plain UTF-8") from None
    if "\x00" in text or "\\" in text:
        raise ScopeDeniedError("Shared tool Git configuration contains unsupported escapes")
    section = ""
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if len(raw_line.encode()) > 8192:
            raise ScopeDeniedError("Shared tool Git configuration exceeds its line bound")
        if line.startswith("["):
            if line.casefold() in {"[core]", "[user]"}:
                section = line[1:-1].casefold()
            elif line == '[remote "origin"]':
                section = "remote"
            elif re.fullmatch(r'\[branch "[A-Za-z0-9_./-]{1,240}"\]', line):
                section = line
            else:
                raise ScopeDeniedError("Shared tool Git configuration contains an unsafe section")
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*)\s*=\s*(.*)", line)
        if match is None or not section:
            raise ScopeDeniedError("Shared tool Git configuration has unsupported syntax")
        key, value = match[1].casefold(), match[2].strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        if '"' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ScopeDeniedError("Shared tool Git configuration contains unsupported values")
        identity = (section, key)
        if identity in seen:
            raise ScopeDeniedError("Shared tool Git configuration contains duplicate values")
        seen.add(identity)
        allowed = False
        if section == "core":
            if key == "repositoryformatversion":
                allowed = value == "0"
            elif key in {"bare", "autocrlf"}:
                allowed = value.casefold() == "false"
            elif key in {"filemode", "logallrefupdates", "ignorecase", "precomposeunicode"}:
                allowed = value.casefold() in {"true", "false"}
            elif key == "eol":
                allowed = value in {"lf", "native"}
        elif section == "user":
            allowed = key in {"name", "email"} and len(value.encode()) <= 1024
        elif section == "remote":
            # The store separately compares origin to the approved canonical
            # repository. Local edit clones may have a literal absolute origin;
            # no reader in this module performs network or transport operations.
            if key == "url":
                allowed = value.startswith(("https://github.com/", "/")) and len(value) <= 4096
            elif key == "fetch":
                allowed = value == "+refs/heads/*:refs/remotes/origin/*"
        elif section.startswith("[branch "):
            allowed = (key == "remote" and value == "origin") or (
                key == "merge" and value.startswith("refs/heads/") and bool(_REF.fullmatch(value))
            )
        if not allowed:
            raise ScopeDeniedError("Shared tool Git configuration is outside the inert allowlist")


def validate_git_metadata(checkout: Path) -> None:
    """Refuse redirects, active hooks and executable config before invoking Git."""
    if checkout.is_symlink() or checkout.resolve(strict=True) != checkout.absolute():
        raise ScopeDeniedError("Shared tool Git checkout is redirected")
    entries = total = 0

    def walk(fd: int, prefix: str, depth: int) -> None:
        nonlocal entries, total
        if depth > 28:
            raise ScopeDeniedError("Shared tool Git control tree is too deep")
        for name in os.listdir(fd):
            relative = f"{prefix}/{name}" if prefix else name
            entries += 1
            if (
                entries > _MAX_CONTROL_ENTRIES
                or relative in _FORBIDDEN
                or (relative.startswith("objects/pack/") and relative.endswith(".promisor"))
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or (
                    relative.startswith("hooks/")
                    and (relative.count("/") != 1 or not relative.endswith(".sample"))
                )
            ):
                raise ScopeDeniedError("Shared tool Git control data contains an unsafe entry")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise StateMismatchError("Shared tool Git metadata changed during readback")
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > _MAX_CONTROL_BYTES:
                    raise ScopeDeniedError("Shared tool Git control data exceeds its byte bound")
            else:
                raise ScopeDeniedError("Shared tool Git control data is redirected or not regular")

    descriptor = os.open(checkout / ".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(descriptor, "", 0)
        config = os.open("config", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(config)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65_536:
                raise ScopeDeniedError(
                    "Shared tool Git configuration is not a bounded regular file"
                )
            with os.fdopen(config, "rb", closefd=False) as reader:
                raw = reader.read(65_537)
            after = os.fstat(config)
            if len(raw) != before.st_size or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise StateMismatchError("Shared tool Git configuration changed during readback")
            _config(raw)
        finally:
            os.close(config)
    finally:
        os.close(descriptor)


def validate_tracked_source(checkout: Path, commit: str, git: Callable[..., str]) -> None:
    """Compare real tracked bytes/modes to HEAD, independently of mutable index flags."""
    listing = git(checkout, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = listing.split("\0")
    if not entries[-1]:
        entries.pop()
    if len(entries) > _MAX_SOURCE_ENTRIES:
        raise ScopeDeniedError("Shared tool tracked source exceeds its entry bound")
    total = 0
    seen: set[str] = set()
    root = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for entry in entries:
            identity, separator, relative = entry.partition("\t")
            parts = identity.split(" ")
            if (
                not separator
                or len(parts) != 3
                or parts[0] not in {"100644", "100755", "120000"}
                or parts[1] != "blob"
                or not _OBJECT.fullmatch(parts[2])
                or relative in seen
                or not relative
                or "\\" in relative
                or any(p in {"", ".", ".."} or p.casefold() == ".git" for p in relative.split("/"))
            ):
                raise ScopeDeniedError("Shared tool source contains an unsupported tracked entry")
            seen.add(relative)
            parent = os.dup(root)
            try:
                path = relative.split("/")
                for piece in path[:-1]:
                    child = os.open(
                        piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                    os.close(parent)
                    parent = child
                info = os.stat(path[-1], dir_fd=parent, follow_symlinks=False)
                if parts[0] == "120000":
                    if not stat.S_ISLNK(info.st_mode):
                        raise StateMismatchError(
                            "Shared tool tracked symlink differs from its source"
                        )
                    data = os.readlink(path[-1], dir_fd=parent).encode("utf-8")
                    digest = hashlib.sha1(
                        b"blob " + str(len(data)).encode() + b"\0" + data
                    ).hexdigest()
                    total += len(data)
                else:
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_size > _MAX_SOURCE_FILE
                        or bool(info.st_mode & 0o111) != (parts[0] == "100755")
                    ):
                        raise StateMismatchError("Shared tool tracked file mode or type changed")
                    descriptor = os.open(
                        path[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    try:
                        before = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(before.st_mode)
                            or before.st_nlink != 1
                            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                            != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                        ):
                            raise StateMismatchError(
                                "Shared tool tracked input changed before readback"
                            )
                        hasher = hashlib.sha1(b"blob " + str(before.st_size).encode() + b"\0")
                        count = 0
                        while chunk := os.read(descriptor, 1024 * 1024):
                            count += len(chunk)
                            if count > before.st_size:
                                raise StateMismatchError(
                                    "Shared tool tracked input grew during readback"
                                )
                            hasher.update(chunk)
                        after = os.fstat(descriptor)
                        if count != before.st_size or (
                            before.st_size,
                            before.st_mtime_ns,
                            before.st_ctime_ns,
                        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                            raise StateMismatchError(
                                "Shared tool tracked input changed during readback"
                            )
                        digest = hasher.hexdigest()
                        total += count
                    finally:
                        os.close(descriptor)
                if total > _MAX_SOURCE_BYTES or digest != parts[2]:
                    raise StateMismatchError(
                        "Shared tool tracked source differs from its pinned commit"
                    )
            finally:
                os.close(parent)
    finally:
        os.close(root)
