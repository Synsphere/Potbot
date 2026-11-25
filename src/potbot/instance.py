"""Kernel-owned instance leases and generation-bound local stop requests.

Lock files are permanent: deleting them would allow two owners to lock different
inodes. The OS releases the lock on a crash, so a stale receipt never owns a PID.
Windows uses one directory per user because the task, ports and WSL host are
shared between checkouts. Stop requests carry a fresh generation, never a PID.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

_WINDOWS = os.name == "nt"
_ROLES = {"startup", "desktop", "service", "stopping", "control"}
_RETAINED: list[InstanceLease] = []


class InstanceBusy(CapabilityUnavailableError):
    """An existing owner or a stop operation prevents another start."""


class StartupCancelled(CapabilityUnavailableError):
    """The user requested shutdown while startup was in progress."""


def _root(root: Path | None) -> Path:
    if root is None:
        from potbot.dependencies import project_root

        root = project_root()
    return root.resolve()


def runtime_home(root: Path | None = None) -> Path:
    if _WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if not local or not Path(local).is_absolute():
            raise InvalidInputError("Potbot instance storage needs an absolute LOCALAPPDATA.")
        return Path(local) / "Potbot" / "runtime" / "instances"
    return _root(root) / ".potbot" / "instances"


def _home(root: Path | None) -> Path:
    path = runtime_home(root)
    path.mkdir(parents=True, exist_ok=True, mode=0o777 if _WINDOWS else 0o700)
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise InvalidInputError("Potbot instance storage cannot be a link.")
    info = path.stat()
    if not _WINDOWS and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise InvalidInputError("Potbot instance storage must be private to its owner.")
    return path


def _role(role: str) -> str:
    if role not in _ROLES:
        raise ValueError("Unknown Potbot instance role")
    return role


def _event_signalled(name: str) -> bool:
    from ctypes import wintypes

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    kernel.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenEventW(0x00100000, False, name)  # SYNCHRONIZE; read state only.
    if not handle:
        if cast(Any, ctypes).get_last_error() == 2:  # Launcher has already exited.
            return False
        raise CapabilityUnavailableError("Potbot cannot read its launcher's cancellation event.")
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result not in {0, 258}:  # WAIT_OBJECT_0 / WAIT_TIMEOUT
            raise CapabilityUnavailableError("Potbot cannot check launcher cancellation.")
        return bool(result == 0)
    finally:
        kernel.CloseHandle(handle)


def _launcher_stop_requested() -> bool:
    name = os.environ.get("POTBOT_START_STOP_EVENT")
    if not _WINDOWS or not name:
        return False
    if not re.fullmatch(r"Global\\Potbot\.Start\.Stop\.S-1-(?:[0-9]+-){1,14}[0-9]+", name):
        raise InvalidInputError("Potbot launcher cancellation event name is invalid.")
    return _event_signalled(name)


class _FileLock:
    def __init__(self, path: Path) -> None:
        if path.is_symlink():
            raise InvalidInputError("Potbot instance locks cannot be symbolic links.")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(path, flags, 0o600)
        self.locked = False
        if not stat.S_ISREG(os.fstat(self.fd).st_mode):
            os.close(self.fd)
            raise InvalidInputError("Potbot instance lock is not a regular file.")

    def acquire(self) -> bool:
        try:
            if _WINDOWS:
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                native = cast(Any, msvcrt)  # These APIs are absent from POSIX typing stubs.
                native.locking(self.fd, native.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
            return True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise

    def close(self) -> None:
        # Closing our descriptor releases only our own kernel lock.
        os.close(self.fd)


class _Gate:
    """Serialize publication/release and the stopping check, never long work."""

    def __init__(self, home: Path) -> None:
        self.lock = _FileLock(home / "gate.lock")

    def __enter__(self) -> None:
        deadline = time.monotonic() + 5
        while not self.lock.acquire():
            if time.monotonic() >= deadline:
                self.lock.close()
                raise InstanceBusy("Another Potbot control operation is busy; retry shortly.")
            time.sleep(0.02)

    def __exit__(self, *args: object) -> None:
        self.lock.close()


def _busy(home: Path, role: str) -> bool:
    lock = _FileLock(home / (role + ".lock"))
    try:
        return not lock.acquire()
    finally:
        lock.close()


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            return None
        with path.open("rb") as stream:
            raw = stream.read(8193)
        value = json.loads(raw) if len(raw) <= 8192 else None
        if not isinstance(value, dict):
            return None
        return value
    except (OSError, ValueError):
        return None


def _receipt(home: Path, role: str) -> dict[str, Any]:
    value = _read(home / (role + ".json"))
    if (
        value is None
        or value.get("role") != role
        or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("generation", "")))
        or not isinstance(value.get("root"), str)
        or not Path(value["root"]).is_absolute()
    ):
        raise CapabilityUnavailableError(
            "A Potbot process owns the instance lock but its control receipt is unavailable."
        )
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class InstanceLease:
    def __init__(
        self, role: str, *, root: Path | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.role = _role(role)
        self.root = _root(root)
        self.home = _home(self.root)
        self.generation = secrets.token_hex(16)
        self.stopped = stop_event if stop_event is not None else threading.Event()
        self._closed = threading.Event()
        self._lock: _FileLock | None = None
        self._thread: threading.Thread | None = None
        self._retained = False

    def __enter__(self) -> InstanceLease:
        with _Gate(self.home):
            if self.role != "stopping" and _busy(self.home, "stopping"):
                raise InstanceBusy("Potbot is stopping; wait for stop to finish before starting.")
            if self.role != "stopping" and _launcher_stop_requested():
                # uv can create this Python after stop has already found no
                # leases. The launcher's event survives until its child exits.
                raise StartupCancelled("Potbot startup was cancelled by stop.bat.")
            lock = _FileLock(self.home / (self.role + ".lock"))
            if not lock.acquire():
                lock.close()
                owner = _receipt(self.home, self.role)
                raise InstanceBusy(f"Potbot {self.role} is already running from {owner['root']}.")
            try:
                _write(self.home / (self.role + ".json"), {
                    "role": self.role, "root": str(self.root), "generation": self.generation,
                })
            except BaseException:
                lock.close()
                raise
            self._lock = lock
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def _watch(self) -> None:
        while not self._closed.wait(0.1):
            request = _read(self.home / (self.role + ".stop"))
            if request and request.get("generation") == self.generation:
                self.stopped.set()
                return

    def check_stop(self) -> None:
        if self.stopped.is_set():
            raise StartupCancelled("Potbot startup was cancelled by a stop request.")

    def retain_until_exit(self) -> None:
        """Keep ownership until Windows tears down the supervisor's process job."""
        if not self._retained:
            self._retained = True
            _RETAINED.append(self)

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._retained:
            return
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        with _Gate(self.home):
            try:
                for suffix in (".json", ".stop"):
                    path = self.home / (self.role + suffix)
                    value = _read(path)
                    if value and value.get("generation") == self.generation:
                        path.unlink(missing_ok=True)
            finally:
                if self._lock is not None:
                    self._lock.close()
                    self._lock = None


def is_running(role: str, *, root: Path | None = None) -> bool:
    home = _home(root)
    with _Gate(home):
        return _busy(home, _role(role))


def owner_root(role: str, *, root: Path | None = None) -> Path | None:
    home = _home(root)
    role = _role(role)
    with _Gate(home):
        return Path(_receipt(home, role)["root"]) if _busy(home, role) else None


def request_stop(role: str, *, root: Path | None = None) -> bool:
    home = _home(root)
    role = _role(role)
    with _Gate(home):
        if not _busy(home, role):
            return False
        owner = _receipt(home, role)
        if Path(owner["root"]) != _root(root):
            raise InstanceBusy(f"Run stop from the active Potbot checkout: {owner['root']}.")
        _write(home / (role + ".stop"), {"generation": owner["generation"]})
        return True


def wait_stopped(role: str, *, root: Path | None = None, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while is_running(role, root=root):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True
