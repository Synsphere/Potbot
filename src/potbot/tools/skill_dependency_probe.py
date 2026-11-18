"""Fixed guest-side prerequisite observations; never execute a declared dependency.

The controller runs this program with a separately pinned image interpreter,
``-I -S -B -c`` and bounded JSON stdin. Neither registry descriptions nor package
code become Python, argv fragments, imports, environment values or shell input.
The caller must already own the agent's qualified runtime and read lease.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.serialization import canonical_json

PROBE_SCHEMA = "potbot-skill-dependency-observation/v1"
MAX_REQUIREMENTS = 128
MAX_WIRE_BYTES = 131072
BINARY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,127}\Z")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
SHARED_EXECUTABLE = re.compile(
    r"/usr/local/tools/[a-z][a-z0-9-]{1,62}/(?:environment|static)/[A-Za-z0-9_./+\-]{1,220}\Z"
)
IMAGE_EXECUTABLE = re.compile(r"/(?:usr/bin|bin)/[A-Za-z0-9][A-Za-z0-9_.+\-]{0,127}\Z")
BASE_ENVIRONMENT = frozenset(
    {"HOME", "PATH", "TMPDIR", "LANG", "TERM", "SHELL", "POTBOT_AI_CONFIG", "POTBOT_AI_HISTORY"}
)
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_STATES = frozenset(
    {"present", "missing", "not_executable", "unreadable", "limit", "changed", "outside_scope"}
)

# This source is intentionally self-contained: the selected image need not
# contain Potbot, and importing from the writable agent home is disabled.
PROBE_PROGRAM = r"""
import hashlib, json, os, stat, sys

MAX_WIRE = 131072
MAX_FILE = 33554432
remaining = 67108864
request = json.loads(sys.stdin.buffer.read(MAX_WIRE + 1))
if set(request) != {"schema", "binaries", "environment"} or request["schema"] != "potbot-skill-dependency-observation/v1":
    raise ValueError("invalid probe input")
if len(request["binaries"]) > 128 or len(request["environment"]) > 128:
    raise ValueError("too many probe inputs")

def observe(name):
    global remaining
    locations = [name] if name.startswith("/") else ["/usr/bin/" + name, "/bin/" + name]
    non_executable = False
    for path in locations:
        physical = os.path.realpath(path)
        if path.startswith("/usr/local/tools/"):
            in_scope = physical == path
        else:
            in_scope = physical.startswith(("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/opt/")) and not any(physical == mount or physical.startswith(mount + "/") for mount in ("/usr/local/tools", "/usr/local/skills", "/opt/potbot/models"))
        if not in_scope:
            return {"name": name, "state": "outside_scope", "sha256": None}
        try:
            details = os.stat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return {"name": name, "state": "unreadable", "sha256": None}
        if not stat.S_ISREG(details.st_mode) or not os.access(path, os.X_OK):
            non_executable = True
            continue
        if details.st_size > MAX_FILE or details.st_size > remaining:
            return {"name": name, "state": "limit", "sha256": None}
        try:
            fd = os.open(physical, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size) != (details.st_dev, details.st_ino, details.st_mode, details.st_size):
                    return {"name": name, "state": "changed", "sha256": None}
                hashed = hashlib.sha256()
                count = 0
                while count <= opened.st_size:
                    chunk = os.read(fd, min(65536, opened.st_size + 1 - count))
                    if not chunk:
                        break
                    count += len(chunk)
                    remaining -= len(chunk)
                    hashed.update(chunk)
                after = os.fstat(fd)
                if count != opened.st_size or (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                    return {"name": name, "state": "changed", "sha256": None}
                return {"name": name, "state": "present", "sha256": hashed.hexdigest()}
            finally:
                os.close(fd)
        except OSError:
            return {"name": name, "state": "unreadable", "sha256": None}
    return {"name": name, "state": "not_executable" if non_executable else "missing", "sha256": None}

result = {
    "schema": "potbot-skill-dependency-observation/v1",
    "operatingSystem": sys.platform,
    "uid": os.getuid(),
    "gid": os.getgid(),
    "binaries": [observe(name) for name in request["binaries"]],
    "environment": [{"name": name, "present": bool(os.environ.get(name))} for name in request["environment"]],
}
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
""".strip()
PROBE_SHA256 = hashlib.sha256(PROBE_PROGRAM.encode("utf-8")).hexdigest()


def binary_supported(name: str) -> bool:
    return bool(
        BINARY_NAME.fullmatch(name)
        or IMAGE_EXECUTABLE.fullmatch(name)
        or (
            SHARED_EXECUTABLE.fullmatch(name)
            and not any(part in {"", ".", ".."} for part in name.split("/")[1:])
        )
    )


def probe_input(binaries: list[str], environment: list[str]) -> bytes:
    """Only validated literal selectors enter the fixed program's stdin."""
    if (
        type(binaries) is not list
        or type(environment) is not list
        or len(binaries) > MAX_REQUIREMENTS
        or len(environment) > MAX_REQUIREMENTS
        or any(not isinstance(name, str) or not binary_supported(name) for name in binaries)
        or any(not isinstance(name, str) or name not in BASE_ENVIRONMENT for name in environment)
        or len(set(binaries)) != len(binaries)
        or len(set(environment)) != len(environment)
    ):
        raise InvalidInputError("Dependency probe selectors exceed the literal contract")
    return canonical_json(
        {"schema": PROBE_SCHEMA, "binaries": binaries, "environment": environment}
    ).encode("utf-8")


def parse_observation(
    raw: bytes, *, binaries: list[str], environment: list[str], uid: int, gid: int
) -> dict[str, Any]:
    """Reject extra fields, duplicate JSON keys, selectors and secret-bearing output."""
    probe_input(binaries, environment)
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_WIRE_BYTES:
        raise StateMismatchError("Dependency probe output exceeds its bounded contract")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "operatingSystem", "uid", "gid", "binaries", "environment"}
            or value["schema"] != PROBE_SCHEMA
            or value["operatingSystem"] not in {"linux", "darwin", "win32", "freebsd"}
            or type(value["uid"]) is not int
            or type(value["gid"]) is not int
            or value["uid"] != uid
            or value["gid"] != gid
            or type(value["binaries"]) is not list
            or type(value["environment"]) is not list
            or len(value["binaries"]) != len(binaries)
            or len(value["environment"]) != len(environment)
        ):
            raise ValueError("invalid observation")
        for name, row in zip(binaries, value["binaries"], strict=True):
            if (
                not isinstance(row, dict)
                or set(row) != {"name", "state", "sha256"}
                or row["name"] != name
                or row["state"] not in _STATES
                or (
                    row["state"] == "present"
                    and (not isinstance(row["sha256"], str) or not _SHA.fullmatch(row["sha256"]))
                )
                or (row["state"] != "present" and row["sha256"] is not None)
            ):
                raise ValueError("invalid binary observation")
        for name, row in zip(environment, value["environment"], strict=True):
            if (
                not isinstance(row, dict)
                or set(row) != {"name", "present"}
                or row["name"] != name
                or type(row["present"]) is not bool
            ):
                raise ValueError("invalid environment observation")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StateMismatchError(
            "Dependency probe output differs from the selected contract"
        ) from None
    return value
