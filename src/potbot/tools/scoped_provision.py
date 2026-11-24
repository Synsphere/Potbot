"""Provision the dedicated systemd launcher and accounting subtree on Linux."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.scoped_launcher import DEFAULT_SOCKET

SOCKET = "potbot-scoped-worker.socket"
BROKER = "potbot-scoped-worker@.service"
KEEPER = "potbot-scoped-worker-cgroups.service"
CGROUP_ROOT = Path("/sys/fs/cgroup")
CONTROLLERS = ("cpu", "io", "memory", "pids")
UNIT_DIR = Path("/etc/systemd/system")
ENV_FILE = Path("/etc/potbot-scoped-worker/monitor.env")
AUDIT = Path("/var/lib/potbot-scoped-worker/audit")
LOCK = Path("/run/potbot-scoped-worker-provision.lock")
SYSTEMCTL = "/usr/bin/systemctl"
MODULE = "potbot.tools.scoped_provision"
_MANAGED_HEADER = "# Managed by Potbot scoped launcher v2; do not edit\n"
_LEGACY_PATH = r"/[A-Za-z0-9_./+@:\\-]+"


def _protected(path: Path) -> None:
    if not path.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_/+.@\-]+", str(path)):
        raise CapabilityUnavailableError("Use a protected absolute path without spaces.")
    resolved = path.resolve(strict=True)
    for part in {path, *path.parents, resolved, *resolved.parents}:
        if part.is_symlink():
            # A root-owned Python venv may link to the trusted system interpreter.
            if part.lstat().st_uid != 0:
                raise CapabilityUnavailableError("A launcher path has an untrusted link.")
        else:
            info = part.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise CapabilityUnavailableError("A launcher path is writable by another user.")


def _executable(path: Path, digest: str | None = None) -> None:
    _protected(path)
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o111
        or info.st_mode & (stat.S_ISUID | stat.S_ISGID)
        or (digest is not None and path.resolve() != path)
    ):
        raise CapabilityUnavailableError("Expected a protected executable without set-ID bits.")
    if digest is not None:
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise CapabilityUnavailableError("A monitoring executable differs from its pin.")


def _keeper_unit(python: Path, uid: int, gid: int) -> dict[Path, str]:
    return {
        UNIT_DIR / KEEPER: _MANAGED_HEADER
        + (
            "[Unit]\nDescription=Potbot scoped worker accounting subtree\n"
            "[Service]\nType=notify\nNotifyAccess=main\nSlice=system.slice\n"
            f"User={uid}\nGroup={gid}\n"
            f"Delegate={' '.join(CONTROLLERS)}\nDelegateSubgroup=keeper\n"
            f"ExecStart={python} -I -m {MODULE} hold\n"
            "NoNewPrivileges=yes\nTimeoutStartSec=15\n"
        )
    }


def _runtime_units(
    python: Path,
    join: Path,
    tracer: Path,
    join_sha: str,
    tracer_sha: str,
    uid: int,
    gid: int,
    auditor_uid: int,
    auditor_gid: int,
    cgroup: Path,
) -> dict[Path, str]:
    return {
        UNIT_DIR / BROKER: _MANAGED_HEADER
        + (
            "[Unit]\nDescription=Potbot scoped worker launcher instance\n"
            "[Service]\nType=simple\nStandardInput=socket\nStandardOutput=journal\n"
            f"ExecStart={python} -I -m potbot.tools.scoped_launcher "
            f"accept {cgroup} {join} {uid} {gid} {auditor_uid} {auditor_gid}\n"
            "KillMode=control-group\nTimeoutStopSec=10\n"
        ),
        UNIT_DIR / SOCKET: _MANAGED_HEADER
        + (
            "[Unit]\nDescription=Potbot scoped worker launcher\n"
            f"BindsTo={KEEPER}\nAfter={KEEPER}\n"
            "[Socket]\nAccept=yes\nRemoveOnStop=yes\n"
            f"ListenSequentialPacket={DEFAULT_SOCKET}\n"
            f"SocketUser=root\nSocketGroup={gid}\nSocketMode=0660\n"
            "[Install]\nWantedBy=sockets.target\n"
        ),
        ENV_FILE: _MANAGED_HEADER
        + (
            f"POTBOT_OPERATOR_AUDIT_ROOT={AUDIT}\n"
            f"POTBOT_OPERATOR_CGROUP_ROOT={cgroup}\n"
            f"POTBOT_OPERATOR_STRACE={tracer}\nPOTBOT_OPERATOR_STRACE_SHA256={tracer_sha}\n"
            f"POTBOT_OPERATOR_CGROUP_JOIN={join}\nPOTBOT_OPERATOR_CGROUP_JOIN_SHA256={join_sha}\n"
        ),
    }


def _bounded_diagnostic(value: str, maximum: int = 16384) -> str:
    value = value.replace("\x00", "?")
    return value if len(value) <= maximum else "[older output truncated]\n" + value[-maximum:]


def _systemd_diagnostics(args: tuple[str, ...], result: subprocess.CompletedProcess[str]) -> None:
    print("POTBOT_SYSTEMD_FAILED_COMMAND=" + json.dumps([SYSTEMCTL, *args]), file=sys.stderr)
    for label, value in (("stdout", result.stdout), ("stderr", result.stderr)):
        if value:
            print(f"--- systemctl {label} ---", file=sys.stderr)
            print(_bounded_diagnostic(value), file=sys.stderr)
    try:
        journal = subprocess.run(
            [
                "/usr/bin/journalctl",
                "--no-pager",
                "--output=short-monotonic",
                "--lines=80",
                "-u",
                KEEPER,
                "-u",
                SOCKET,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": "/usr/sbin:/usr/bin", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if journal.stdout:
        print("--- scoped launcher journal ---", file=sys.stderr)
        print(_bounded_diagnostic(journal.stdout, 32768), file=sys.stderr)


def _manager(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [SYSTEMCTL, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=35,
        env={"PATH": "/usr/sbin:/usr/bin", "LANG": "C"},
    )
    if check and result.returncode:
        _systemd_diagnostics(args, result)
        operation = args[0] if args else "unknown"
        raise CapabilityUnavailableError(
            f"systemd refused scoped launcher provisioning during systemctl {operation}"
        )
    return result


def _preflight_unit(name: str) -> None:
    instance = name.replace("@.", "@provision-check.")
    enablement = _manager("is-enabled", instance, check=False)
    unit_state = enablement.stdout.strip()
    if unit_state == "not-found":
        return
    if not unit_state:
        raise CapabilityUnavailableError(
            "systemd could not inspect existing launcher configuration."
        )
    props = _manager("show", "-p", "FragmentPath", "-p", "DropInPaths", instance).stdout
    values = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
    if values.get("FragmentPath", "") not in ("", str(UNIT_DIR / name)) or values.get(
        "DropInPaths"
    ):
        raise CapabilityUnavailableError("Existing systemd launcher configuration was kept.")


def _keeper_cgroup() -> Path:
    value = _manager("show", "--property=ControlGroup", "--value", KEEPER).stdout.strip()
    if not value.startswith("/") or value == "/" or "\n" in value or "\r" in value:
        raise CapabilityUnavailableError("systemd returned an invalid scoped launcher cgroup")
    path = CGROUP_ROOT / value.lstrip("/")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise CapabilityUnavailableError("systemd scoped launcher cgroup is unavailable") from None
    if (
        resolved != path
        or path.name != KEEPER
        or not path.is_relative_to(CGROUP_ROOT)
        or CGROUP_ROOT.resolve(strict=True) != CGROUP_ROOT
    ):
        raise CapabilityUnavailableError("systemd scoped launcher cgroup escaped its hierarchy")
    return path


def _delegated_cgroup_from_membership(value: str) -> Path:
    rows = [row for row in value.splitlines() if row]
    if len(rows) != 1 or not rows[0].startswith("0::/"):
        raise CapabilityUnavailableError("Scoped launcher requires the unified cgroup hierarchy")
    process_cgroup = CGROUP_ROOT / rows[0][3:].lstrip("/")
    if process_cgroup.name != "keeper" or process_cgroup.parent.name != KEEPER:
        raise CapabilityUnavailableError(
            "Scoped launcher process is outside its delegated subgroup"
        )
    return process_cgroup.parent


def _legacy_managed(path: Path, payload: str) -> bool:
    escaped_socket = re.escape(str(DEFAULT_SOCKET))
    if path == UNIT_DIR / KEEPER:
        pattern = (
            r"\[Unit\]\nDescription=Potbot scoped worker accounting subtree\n"
            r"\[Service\]\nType=notify\nNotifyAccess=main\nSlice=system\.slice\n"
            r"User=\d+\nGroup=\d+\nDelegate=cpu io memory pids\nDelegateSubgroup=keeper\n"
            rf"ExecStart={_LEGACY_PATH} -I -m potbot\.tools\.scoped_provision hold\n"
            r"NoNewPrivileges=yes\nTimeoutStartSec=15\n"
        )
    elif path == UNIT_DIR / BROKER:
        pattern = (
            r"\[Unit\]\nDescription=Potbot scoped worker launcher instance\n"
            r"\[Service\]\nType=simple\nStandardInput=socket\nStandardOutput=journal\n"
            rf"ExecStart={_LEGACY_PATH} -I -m potbot\.tools\.scoped_launcher "
            rf"accept {_LEGACY_PATH} {_LEGACY_PATH} \d+ \d+ \d+(?: \d+)?\n"
            r"KillMode=control-group\nTimeoutStopSec=10\n"
        )
    elif path == UNIT_DIR / SOCKET:
        pattern = (
            r"\[Unit\]\nDescription=Potbot scoped worker launcher\n"
            rf"BindsTo={re.escape(KEEPER)}\nAfter={re.escape(KEEPER)}\n"
            r"\[Socket\]\nAccept=yes\nRemoveOnStop=yes\n"
            rf"ListenSequentialPacket={escaped_socket}\n"
            r"SocketUser=root\nSocketGroup=\d+\nSocketMode=0660\n"
            r"\[Install\]\nWantedBy=sockets\.target\n"
        )
    elif path == ENV_FILE:
        pattern = (
            rf"POTBOT_OPERATOR_AUDIT_ROOT={re.escape(str(AUDIT))}\n"
            rf"POTBOT_OPERATOR_CGROUP_ROOT={_LEGACY_PATH}\n"
            rf"POTBOT_OPERATOR_STRACE={_LEGACY_PATH}\n"
            r"POTBOT_OPERATOR_STRACE_SHA256=[0-9a-f]{64}\n"
            rf"POTBOT_OPERATOR_CGROUP_JOIN={_LEGACY_PATH}\n"
            r"POTBOT_OPERATOR_CGROUP_JOIN_SHA256=[0-9a-f]{64}\n"
        )
    else:
        return False
    return re.fullmatch(pattern, payload) is not None


def _managed_existing(path: Path, payload: str) -> bool:
    return payload.startswith(_MANAGED_HEADER) or _legacy_managed(path, payload)


def _inspect(path: Path, content: str) -> str:
    if not path.exists() and not path.is_symlink():
        return "missing"
    _protected(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o644:
        raise CapabilityUnavailableError(
            "A launcher file differs; existing configuration was kept."
        )
    try:
        payload = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise CapabilityUnavailableError(
            "A launcher file differs; existing configuration was kept."
        ) from None
    if payload == content:
        return "current"
    if _managed_existing(path, payload):
        return "stale"
    raise CapabilityUnavailableError("A launcher file differs; existing configuration was kept.")


def _temporary_file(path: Path, text: str) -> Path:
    temporary = path.with_name(f".potbot-{uuid4().hex}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish(path: Path, text: str, state: str) -> bytes | None:
    if state not in {"missing", "stale"}:
        raise AssertionError("publish requires a pending launcher file")
    previous: bytes | None = None
    snapshot: tuple[int, int, int, int] | None = None
    if state == "stale":
        before = path.lstat()
        previous = path.read_bytes()
        try:
            decoded = previous.decode("ascii")
        except UnicodeError:
            raise CapabilityUnavailableError(
                "A launcher file changed during provisioning."
            ) from None
        if not _managed_existing(path, decoded):
            raise CapabilityUnavailableError("A launcher file changed during provisioning.")
        snapshot = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)

    temporary = _temporary_file(path, text)
    try:
        if state == "missing":
            os.link(temporary, path, follow_symlinks=False)
        else:
            assert snapshot is not None
            current = path.lstat()
            observed = (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            )
            if observed != snapshot:
                raise CapabilityUnavailableError("A launcher file changed during provisioning.")
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return previous


def _restore(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".potbot-restore-{uuid4().hex}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _audit_directory(uid: int, gid: int) -> bool:
    try:
        AUDIT.mkdir(mode=0o700)
    except FileExistsError:
        info = AUDIT.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise CapabilityUnavailableError(
                "Existing audit directory has a different owner or mode."
            ) from None
        if info.st_gid != gid:
            try:
                # The dedicated WSL host may migrate synoperator's primary
                # group while preserving this private, service-owned audit tree.
                # Only that exact owner+0700 case is safe to repair automatically.
                os.chown(AUDIT, -1, gid)
            except OSError:
                raise CapabilityUnavailableError(
                    "Existing private audit directory group migration failed."
                ) from None
            migrated = AUDIT.lstat()
            if not stat.S_ISDIR(migrated.st_mode) or (
                migrated.st_uid,
                migrated.st_gid,
                stat.S_IMODE(migrated.st_mode),
            ) != (uid, gid, 0o700):
                raise CapabilityUnavailableError(
                    "Existing private audit directory group migration failed."
                ) from None
        return False
    try:
        os.chmod(AUDIT, 0o700)
        os.chown(AUDIT, uid, gid)
    except OSError:
        AUDIT.rmdir()
        raise
    return True


def _apply(
    python: Path,
    join: Path,
    tracer: Path,
    join_sha: str,
    tracer_sha: str,
    uid: int,
    gid: int,
    auditor_uid: int,
    auditor_gid: int,
) -> None:
    for name in (KEEPER, BROKER, SOCKET):
        _preflight_unit(name)

    pre_active = _manager("is-active", "--quiet", SOCKET, check=False).returncode == 0
    pre_keeper_active = _manager("is-active", "--quiet", KEEPER, check=False).returncode == 0
    enable_state = _manager("is-enabled", SOCKET, check=False).stdout.strip()
    pre_enabled = enable_state == "enabled"
    wants = UNIT_DIR / "sockets.target.wants" / SOCKET
    if enable_state not in ("", "not-found", "disabled", "enabled") or (
        (wants.exists() or wants.is_symlink())
        and (not wants.is_symlink() or wants.resolve() != UNIT_DIR / SOCKET)
    ):
        raise CapabilityUnavailableError(
            "An existing socket enablement needs administrator review."
        )
    if pre_active and not pre_keeper_active:
        raise CapabilityUnavailableError("Scoped launcher socket is active without its keeper")
    if (DEFAULT_SOCKET.exists() or DEFAULT_SOCKET.is_symlink()) and not pre_active:
        info = DEFAULT_SOCKET.lstat()
        if not stat.S_ISSOCK(info.st_mode) or (
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
        ) != (0, gid, 0o660):
            raise CapabilityUnavailableError(
                "An existing launcher socket needs administrator review."
            )
        DEFAULT_SOCKET.unlink()

    keeper_files = _keeper_unit(python, uid, gid)
    keeper_states = {path: _inspect(path, data) for path, data in keeper_files.items()}
    if pre_keeper_active and any(state == "missing" for state in keeper_states.values()):
        raise CapabilityUnavailableError(
            "The active scoped launcher keeper has no managed unit file."
        )
    keeper_change = any(state != "current" for state in keeper_states.values())

    changes: list[tuple[Path, bytes | None]] = []
    directories: list[Path] = []
    made_audit = False
    enabled_by_us = False
    socket_started = False
    socket_stopped = False
    keeper_started = False
    keeper_stopped = False
    socket_running = pre_active

    try:
        for directory in (ENV_FILE.parent, AUDIT.parent):
            if not directory.exists() and not directory.is_symlink():
                directory.mkdir(mode=0o755)
                directories.append(directory)
            _protected(directory)
            if directory.is_symlink():
                raise CapabilityUnavailableError(
                    "A launcher directory must not be a symbolic link."
                )
        made_audit = _audit_directory(uid, gid)

        if keeper_change:
            if socket_running:
                _manager("stop", SOCKET)
                socket_stopped = True
                socket_running = False
            if pre_keeper_active:
                _manager("stop", KEEPER)
                keeper_stopped = True
            for path, data in keeper_files.items():
                state = keeper_states[path]
                if state != "current":
                    changes.append((path, _publish(path, data, state)))
            _manager("daemon-reload")

        if not pre_keeper_active or keeper_change:
            keeper_started = True
            _manager("start", KEEPER)

        cgroup = _keeper_cgroup()
        runtime_files = _runtime_units(
            python,
            join,
            tracer,
            join_sha,
            tracer_sha,
            uid,
            gid,
            auditor_uid,
            auditor_gid,
            cgroup,
        )
        runtime_states = {path: _inspect(path, data) for path, data in runtime_files.items()}
        runtime_change = any(state != "current" for state in runtime_states.values())

        if runtime_change and socket_running:
            _manager("stop", SOCKET)
            socket_stopped = True
            socket_running = False

        if runtime_change:
            for path, data in runtime_files.items():
                state = runtime_states[path]
                if state != "current":
                    changes.append((path, _publish(path, data, state)))
            _manager("daemon-reload")

        if not pre_enabled:
            enabled_by_us = True
            _manager("enable", SOCKET)
        if not socket_running:
            socket_started = True
            _manager("start", SOCKET)
            socket_running = True

        info = DEFAULT_SOCKET.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, gid, 0o660)
            or cgroup.stat().st_uid != uid
            or not set(CONTROLLERS) <= set((cgroup / "cgroup.subtree_control").read_text().split())
        ):
            raise CapabilityUnavailableError(
                "Installed launcher or delegated controllers failed readback."
            )
    except (CapabilityUnavailableError, OSError, subprocess.TimeoutExpired):
        try:
            if socket_started:
                _manager("stop", SOCKET)
            if enabled_by_us:
                _manager("disable", SOCKET)
            if keeper_started:
                _manager("stop", KEEPER)
            for path, previous in reversed(changes):
                _restore(path, previous)
            if changes:
                _manager("daemon-reload")
            if keeper_stopped:
                _manager("start", KEEPER)
            if socket_stopped:
                _manager("start", SOCKET)
            if made_audit:
                AUDIT.rmdir()
            for path in reversed(directories):
                path.rmdir()
        except (CapabilityUnavailableError, OSError, subprocess.TimeoutExpired):
            raise CapabilityUnavailableError(
                "Provisioning rollback needs administrator review."
            ) from None
        raise


def provision(args: argparse.Namespace) -> None:
    import fcntl
    import pwd

    if os.geteuid() != 0 or not sys.flags.isolated:
        raise CapabilityUnavailableError(
            "Run the administrator-owned Python installation with -I as root."
        )
    _protected(UNIT_DIR)
    _protected(LOCK.parent)
    python = Path(sys.executable)
    _executable(python)
    _protected(Path(__file__))
    _executable(args.join, args.join_sha256)
    _executable(args.strace, args.strace_sha256)
    service, auditor = pwd.getpwnam(args.service_user), pwd.getpwnam(args.auditor_user)
    uid, gid = service.pw_uid, service.pw_gid
    if (
        min(uid, gid, auditor.pw_uid, auditor.pw_gid) <= 0
        or auditor.pw_uid == uid
        or auditor.pw_gid == gid
    ):
        raise CapabilityUnavailableError("Use separate unprivileged service and auditor accounts.")
    fd = os.open(LOCK, os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR, 0o600)
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise CapabilityUnavailableError("Provisioning lock is untrusted.")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _apply(
            python,
            args.join,
            args.strace,
            args.join_sha256,
            args.strace_sha256,
            uid,
            gid,
            auditor.pw_uid,
            auditor.pw_gid,
        )


def _hold() -> None:
    cgroup = _delegated_cgroup_from_membership(
        Path("/proc/self/cgroup").read_text(encoding="ascii")
    )
    try:
        resolved = cgroup.resolve(strict=True)
    except OSError:
        raise CapabilityUnavailableError("Delegated worker cgroup disappeared") from None
    if (
        os.geteuid() == 0
        or resolved != cgroup
        or not cgroup.is_relative_to(CGROUP_ROOT)
        or not set(CONTROLLERS) <= set((cgroup / "cgroup.controllers").read_text().split())
        or (cgroup / "cgroup.procs").read_text().strip()
    ):
        raise CapabilityUnavailableError("systemd did not delegate the required worker cgroup.")
    (cgroup / "cgroup.subtree_control").write_text(" ".join("+" + name for name in CONTROLLERS))
    if not set(CONTROLLERS) <= set((cgroup / "cgroup.subtree_control").read_text().split()):
        raise CapabilityUnavailableError("systemd did not enable the delegated controllers.")
    notify = os.environ["NOTIFY_SOCKET"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.sendto(
            b"READY=1",
            "\x00" + notify[1:] if notify.startswith("@") else notify,
        )
    while True:
        signal.pause()


def _digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError("expected lowercase SHA-256")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("install", help="install and enable the systemd launcher")
    for name in ("service-user", "auditor-user"):
        install.add_argument("--" + name, required=True)
    for name in ("join", "strace"):
        install.add_argument("--" + name, type=Path, required=True)
        install.add_argument("--" + name + "-sha256", type=_digest, required=True)
    commands.add_parser("hold", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not sys.platform.startswith("linux"):
            raise CapabilityUnavailableError("Scoped launcher provisioning requires Linux systemd.")
        if args.action == "hold":
            _hold()
        else:
            provision(args)
            print(json.dumps({"launcher_installed": True, "worker_qualified": False}))
        return 0
    except CapabilityUnavailableError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, OSError, subprocess.TimeoutExpired):
        print(
            "Scoped launcher provisioning failed; inspect local systemd diagnostics.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
