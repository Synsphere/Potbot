"""Authored workload for the installed Python profiling acceptance gate."""

import errno
import os
import socket
from pathlib import Path

for family in (socket.AF_INET, socket.AF_INET6):
    try:
        connection = socket.socket(family)
    except PermissionError:
        pass
    else:
        connection.close()
        raise RuntimeError("Unexpected network capability")

try:
    with Path("/potbot-profile-qualification-write-probe").open("xb"):
        raise RuntimeError("Unexpected writable image root")
except OSError as error:
    if error.errno not in (errno.EACCES, errno.EROFS):
        raise


def item_cost(value):
    return (value * value) % 101


def hotspot():
    return sum(item_cost(value) for value in range(200_000))


print("potbot-profile-qualification-private-output")
RESULT = {
    "uid": os.getuid(),
    "gid": os.getgid(),
    "total": hotspot(),
    "network_denied": True,
    "root_write_denied": True,
}
