"""Bounded, host-owned immutable Git source acquisition for reviewed tool builds.

Only a canonical public GitHub repository and full commit are accepted. The
operator pins the host Git executable separately from the guest build command.
No repository command, hook, credential helper, submodule or checkout filter is
run on the host. Failed/partial attempts are retained for explicit inspection;
they are never overwritten or resumed automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.shared_tool_git import validate_git_metadata, validate_tracked_source
from potbot.tools.tool_stage_s10 import _SafeCopy

_REPOSITORY = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_SOURCE_FILES = 8192
MAX_SOURCE_BYTES = 128 * 1024**2
MAX_FILE_BYTES = 16 * 1024**2
MAX_STORAGE_BYTES = 384 * 1024**2
MAX_STORAGE_ENTRIES = 32768
MAX_OUTPUT_BYTES = 2 * 1024**2
SOURCE_TIMEOUT_SECONDS = 120


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def source_identity(repository: str, commit: str) -> None:
    """Reject alternate protocols, credentials, moving refs and ambiguous names."""
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise InvalidInputError("Tool source requires a canonical public GitHub .git URL")
    owner, project = repository.removeprefix("https://github.com/").split("/")
    if owner in {".", ".."} or project in {".git", "..git"} or project.startswith("."):
        raise InvalidInputError("Invalid public GitHub source identity")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise InvalidInputError("Tool source requires an exact lowercase 40-character commit")


def _physical(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ScopeDeniedError("Tool source directory must be a physical absolute directory")
    if path.resolve(strict=True) != path:
        raise ScopeDeniedError("Tool source directory must not contain redirected ancestors")
    return path


def _path(value: str) -> None:
    parts = PurePosixPath(value).parts
    if (
        not value
        or len(value.encode()) > 1024
        or len(parts) > 32
        or value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in value.split("/"))
    ):
        raise ScopeDeniedError("Tool source contains an unsafe resource path")


def _storage_inventory(root: Path) -> tuple[str, int, int]:
    """Hash the retained tree through no-follow descriptors, including Git data."""
    count = total = 0
    inventory: list[list[object]] = []

    def walk(fd: int, prefix: str, depth: int) -> None:
        nonlocal count, total
        if depth > 40:
            raise ScopeDeniedError("Tool source storage nesting exceeds policy")
        for name in sorted(os.listdir(fd)):
            count += 1
            if count > MAX_STORAGE_ENTRIES:
                raise ScopeDeniedError("Tool source storage exceeds entry allowance")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (
                        info.st_dev,
                        info.st_ino,
                    ):
                        raise StateMismatchError("Tool source storage changed while reading")
                    inventory.append([relative + "/"])
                    walk(child, relative + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o7000:
                if info.st_size > MAX_STORAGE_BYTES:
                    raise ScopeDeniedError("Tool source storage file exceeds allowance")
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                digest = hashlib.sha256()
                size = 0
                try:
                    opened = os.fstat(source)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise StateMismatchError("Tool source storage changed while reading")
                    while chunk := os.read(source, 1024**2):
                        size += len(chunk)
                        total += len(chunk)
                        if total > MAX_STORAGE_BYTES:
                            raise ScopeDeniedError("Tool source storage exceeds byte allowance")
                        digest.update(chunk)
                    after = os.fstat(source)
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                        opened.st_size,
                        opened.st_mtime_ns,
                        opened.st_ctime_ns,
                    ) or size != opened.st_size:
                        raise StateMismatchError("Tool source storage changed while reading")
                finally:
                    os.close(source)
                inventory.append([relative, bool(info.st_mode & 0o111), size, digest.hexdigest()])
            else:
                raise ScopeDeniedError("Tool source storage contains a link or special entry")

    directory = os.open(_physical(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(directory, "", 0)
    finally:
        os.close(directory)
    return _digest(inventory), count, total


@dataclass(frozen=True, slots=True)
class ToolSourceReceipt:
    operation_id: UUID
    repository: str
    commit: str
    git_tree: str
    tree_sha256: str
    files: int
    bytes: int
    source_sha256: str
    storage_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "potbot-tool-source/v1",
            "operation_id": str(self.operation_id),
            "repository": self.repository,
            "commit": self.commit,
            "git_tree": self.git_tree,
            "tree_sha256": self.tree_sha256,
            "files": self.files,
            "bytes": self.bytes,
            "source_sha256": self.source_sha256,
            "storage_sha256": self.storage_sha256,
        }


class GitToolSourceStore:
    """Private content receipt owner, independent of the tool registry/queue."""

    def __init__(self, *, root: Path, git_executable: Path, git_sha256: str) -> None:
        self.root = _physical(root)
        info = self.root.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ScopeDeniedError("Tool source cache must be an operator-owned 0700 directory")
        self.git = git_executable
        self.git_sha256 = git_sha256
        self._verify_git()
        # libcurl can consult .netrc even with Git credential helpers disabled.
        # Give only this child process an empty private home, leaving the host
        # environment and the operator's normal credential home untouched.
        self.empty_home = self.root / "credential-free-home"
        self.empty_home.mkdir(mode=0o700, exist_ok=True)
        _physical(self.empty_home)
        if self.empty_home.stat().st_mode & 0o077 or self.empty_home.stat().st_uid != os.geteuid():
            raise ScopeDeniedError("Source transport credential directory must remain private")

    def _verify_git(self) -> None:
        if (
            not self.git.is_absolute()
            or self.git.is_symlink()
            or not self.git.is_file()
            or self.git.resolve(strict=True) != self.git
            or self.git.stat().st_mode & 0o022
            or not os.access(self.git, os.X_OK)
            or not isinstance(self.git_sha256, str)
            or not _DIGEST.fullmatch(self.git_sha256)
            or hashlib.sha256(self.git.read_bytes()).hexdigest() != self.git_sha256
        ):
            raise ScopeDeniedError("Host source Git executable differs from the operator pin")

    def _environment(self) -> dict[str, str]:
        # Do not inherit proxy, askpass, Git config, tokens, HOME or credentials.
        if any(self.empty_home.iterdir()):
            raise ScopeDeniedError("Source transport credential directory must remain empty")
        return {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "HOME": str(self.empty_home),
            "XDG_CONFIG_HOME": str(self.empty_home),
            "GIT_NO_REPLACE_OBJECTS": "1",
        }

    @staticmethod
    def _budget(root: Path) -> None:
        count = total = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                count += 1
                info = (Path(directory) / name).lstat()
                if stat.S_ISLNK(info.st_mode) or not (
                    stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                ):
                    raise ScopeDeniedError("Tool source fetch produced a redirected entry")
                total += info.st_size if stat.S_ISREG(info.st_mode) else 0
                if count > MAX_STORAGE_ENTRIES or total > MAX_STORAGE_BYTES:
                    raise ScopeDeniedError("Tool source fetch exceeds storage allowance")

    def _git(self, cwd: Path, *args: str) -> bytes:
        self._verify_git()
        argv = [
            str(self.git),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "credential.interactive=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.sslVerify=true",
            "-c",
            "http.proxy=",
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "transfer.fsckObjects=true",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            *args,
        ]
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        output = bytearray()
        size = 0
        deadline = time.monotonic() + SOURCE_TIMEOUT_SECONDS
        next_budget = 0.0
        try:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    now = time.monotonic()
                    if now > deadline:
                        raise StateMismatchError("Tool source Git deadline exceeded")
                    if now >= next_budget:
                        self._budget(cwd)
                        next_budget = now + 0.1
                    for key, _ in selector.select(timeout=0.05):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(chunk)
                        if size > MAX_OUTPUT_BYTES:
                            raise ScopeDeniedError("Tool source Git output exceeds allowance")
                        if key.data:
                            output.extend(chunk)
                result = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            self._budget(cwd)
            if result != 0:
                raise StateMismatchError("Pinned public Git source acquisition/readback failed")
            return bytes(output)
        finally:
            # The session/group belongs to this invocation. A Git helper may
            # still own a pipe after the group leader exits, so leader.poll()
            # alone does not prove that the transport has stopped. Drain the
            # owned group on success as well as every exceptional path.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def _fetch(self, checkout: Path, repository: str, commit: str) -> None:
        self._git(
            checkout,
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "--no-recurse-submodules",
            repository,
            commit,
        )

    def _verified_source(self, checkout: Path, commit: str) -> None:
        validate_git_metadata(checkout)

        def read(directory: Path, *args: str) -> str:
            return self._git(directory, *args).decode("utf-8", errors="strict").strip()

        validate_tracked_source(checkout, commit, read)

    def _source_inventory(self, checkout: Path, commit: str) -> tuple[str, int, int]:
        rows = self._git(checkout, "ls-tree", "-rz", "--full-tree", "-l", commit)
        inventory: list[list[object]] = []
        total = 0
        for raw in rows.split(b"\0"):
            if not raw:
                continue
            try:
                prefix, raw_path = raw.split(b"\t", 1)
                mode, kind, object_id, raw_size = prefix.split()
                path = raw_path.decode("utf-8", errors="strict")
                size = int(raw_size)
            except (UnicodeError, ValueError):
                raise ScopeDeniedError("Tool source inventory is invalid") from None
            _path(path)
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise ScopeDeniedError("Tool source links and submodules are not admitted")
            total += size
            if not 0 <= size <= MAX_FILE_BYTES or total > MAX_SOURCE_BYTES:
                raise ScopeDeniedError("Tool source exceeds expanded byte allowance")
            inventory.append([path, mode.decode(), object_id.decode(), size])
            if len(inventory) > MAX_SOURCE_FILES:
                raise ScopeDeniedError("Tool source exceeds file allowance")
        if not inventory:
            raise ScopeDeniedError("Tool source must contain committed regular files")
        return _digest(inventory), len(inventory), total

    def _directory(self, operation_id: UUID) -> Path:
        if not isinstance(operation_id, UUID):
            raise InvalidInputError("Tool source operation must be a UUID")
        return self.root / operation_id.hex

    def acquire(self, operation_id: UUID, repository: str, commit: str) -> ToolSourceReceipt:
        source_identity(repository, commit)
        destination = self._directory(operation_id)
        if destination.exists() or destination.is_symlink():
            receipt = self.inspect(operation_id)
            if receipt.repository != repository or receipt.commit != commit:
                raise StateMismatchError("Tool source attempt is bound to another revision")
            return receipt
        destination.mkdir(mode=0o700)
        tree = destination / "tree"
        tree.mkdir(mode=0o700)
        for directory in ("checkout", "environment", "static", "downloads"):
            (tree / directory).mkdir(mode=0o700)
        checkout = tree / "checkout"
        self._git(checkout, "init", "--quiet", "--template=")
        self._fetch(checkout, repository, commit)
        actual = (
            self._git(checkout, "rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
        )
        if actual != commit:
            raise StateMismatchError("Fetched source differs from the exact reviewed commit")
        self._git(checkout, "fsck", "--strict", "--full", "--no-reflogs")
        source_tree, files, total = self._source_inventory(checkout, commit)
        # No remote configuration or repository scripts are imported by fetch.
        # Global/system attributes and credentials were disabled before init.
        self._git(checkout, "remote", "add", "origin", repository)
        self._git(checkout, "-c", "core.autocrlf=false", "checkout", "--quiet", "--detach", commit)
        git_tree = self._git(checkout, "rev-parse", "HEAD^{tree}").decode().strip()
        self._verified_source(checkout, commit)
        if not _COMMIT.fullmatch(git_tree):
            raise StateMismatchError("Tool source tree identity is invalid")
        storage, _, _ = _storage_inventory(tree)
        source = _digest(
            {
                "repository": repository,
                "commit": commit,
                "git_tree": git_tree,
                "tree_sha256": source_tree,
                "files": files,
                "bytes": total,
            }
        )
        receipt = ToolSourceReceipt(
            operation_id, repository, commit, git_tree, source_tree, files, total, source, storage
        )
        fd = os.open(destination / "receipt.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as writer:
            writer.write(_canonical(receipt.to_dict()))
            writer.flush()
            os.fsync(writer.fileno())
        parent = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return self.inspect(operation_id)

    def inspect(self, operation_id: UUID) -> ToolSourceReceipt:
        directory = _physical(self._directory(operation_id))
        path = directory / "receipt.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise StateMismatchError(
                "Source attempt is incomplete; operator reconciliation required"
            )
        try:
            data: dict[str, Any] = json.loads(path.read_bytes())
            if data.pop("schema") != "potbot-tool-source/v1":
                raise ValueError("schema")
            data["operation_id"] = UUID(data["operation_id"])
            receipt = ToolSourceReceipt(**data)
            source_identity(receipt.repository, receipt.commit)
        except (ValueError, KeyError, TypeError):
            raise StateMismatchError("Tool source receipt is invalid") from None
        if receipt.operation_id != operation_id:
            raise StateMismatchError("Tool source receipt operation mismatch")
        tree = directory / "tree"
        storage, _, _ = _storage_inventory(tree)
        if storage != receipt.storage_sha256:
            raise StateMismatchError("Retained tool source storage changed")
        checkout = tree / "checkout"
        self._verified_source(checkout, receipt.commit)
        if (
            self._git(checkout, "rev-parse", "--verify", "HEAD").decode().strip() != receipt.commit
            or self._git(checkout, "remote", "get-url", "origin").decode().strip()
            != receipt.repository
            or self._git(checkout, "status", "--porcelain=v1", "--untracked-files=all").strip()
        ):
            raise StateMismatchError("Retained tool source Git binding changed")
        expected_tree, files, size = self._source_inventory(checkout, receipt.commit)
        git_tree = self._git(checkout, "rev-parse", "HEAD^{tree}").decode().strip()
        source = _digest(
            {
                "repository": receipt.repository,
                "commit": receipt.commit,
                "git_tree": git_tree,
                "tree_sha256": expected_tree,
                "files": files,
                "bytes": size,
            }
        )
        if (expected_tree, files, size, source, git_tree) != (
            receipt.tree_sha256,
            receipt.files,
            receipt.bytes,
            receipt.source_sha256,
            receipt.git_tree,
        ):
            raise StateMismatchError("Retained tool source inventory changed")
        return receipt

    def materialize(
        self, operation_id: UUID, destination: Path, *, host_owner: tuple[int, int] | None = None
    ) -> ToolSourceReceipt:
        """Copy once into the enrolled home's build area; never infer host IDs.

        The host caller may supply ownership read from its already-qualified
        physical agent home. Guest UID metadata is not a host ownership map.
        """
        receipt = self.inspect(operation_id)
        _physical(destination.parent)
        if not destination.is_absolute() or destination.exists() or destination.is_symlink():
            raise StateMismatchError("Tool build destination must be a new physical directory")
        if host_owner is not None and (
            len(host_owner) != 2 or any(type(item) is not int or item < 0 for item in host_owner)
        ):
            raise InvalidInputError("Build ownership requires explicit physical host identities")
        destination.mkdir(mode=0o700)
        source = self._directory(operation_id) / "tree"
        _SafeCopy().copy(source, destination)
        if _storage_inventory(destination)[0] != receipt.storage_sha256:
            raise StateMismatchError("Materialized tool source differs from immutable receipt")
        if host_owner is not None:
            for directory, dirs, files in os.walk(destination, followlinks=False):
                for name in files + dirs:
                    os.chown(Path(directory) / name, *host_owner, follow_symlinks=False)
            os.chown(destination, *host_owner, follow_symlinks=False)
        self.inspect(operation_id)
        return receipt

    def verify_materialized(self, operation_id: UUID, destination: Path) -> None:
        """Bind the build input to the retained source while its writer lease is held."""
        receipt = self.inspect(operation_id)
        if _storage_inventory(destination)[0] != receipt.storage_sha256:
            raise StateMismatchError("Build input no longer matches the reviewed immutable source")

    def lockfiles(self, operation_id: UUID, paths: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        """Read exact committed dependency evidence without exposing cache paths."""
        self.inspect(operation_id)
        checkout = self._directory(operation_id) / "tree" / "checkout"
        values: list[tuple[str, str]] = []
        for relative in paths:
            _path(relative)
            path = checkout / relative
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise StateMismatchError("Declared source lockfile is unavailable")
            if path.resolve(strict=True) != path:
                raise ScopeDeniedError("Declared source lockfile is redirected")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ScopeDeniedError("Declared source lockfile exceeds policy")
                values.append((relative, hashlib.sha256(data).hexdigest()))
            finally:
                os.close(fd)
        self.inspect(operation_id)
        return tuple(values)
