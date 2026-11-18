"""Owned source transport helpers must stop even after their leader has exited."""

from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from potbot.domain.errors import StateMismatchError
from potbot.tools import tool_sources
from potbot.tools.tool_sources import GitToolSourceStore

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The qualified source transport uses Linux process groups"
)


def _stopped(pid: int) -> bool:
    try:
        # An orphan killed by the transport may await PID 1's reaper. A zombie
        # owns no running process or open pipes and is already stopped.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state in {"Z", "X"}


def _wait_stopped(pid: int) -> bool:
    deadline = time.monotonic() + 2
    while not _stopped(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return _stopped(pid)


@pytest.fixture
def source_command(tmp_path: Path):
    """A real, pinned trusted test command; no network or production Git changes."""
    group_file = tmp_path / "group.txt"
    helper_file = tmp_path / "helper.txt"

    def create(*, close_pipes: bool) -> tuple[GitToolSourceStore, Path, Path]:
        executable = tmp_path / "trusted-source-fixture"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import os, signal\n"
            "from pathlib import Path\n"
            f"Path({str(group_file)!r}).write_text(str(os.getpid()))\n"
            "helper = os.fork()\n"
            "if helper == 0:\n"
            f"    if {close_pipes!r}:\n"
            "        os.close(1)\n"
            "        os.close(2)\n"
            "    while True:\n"
            "        signal.pause()\n"
            f"Path({str(helper_file)!r}).write_text(str(helper))\n"
            "os.write(1, b'fixture-completed\\n')\n"
            "os._exit(0)\n"
        )
        executable.chmod(0o700)
        cache, cwd = tmp_path / "cache", tmp_path / "checkout"
        cache.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        store = GitToolSourceStore(
            root=cache,
            git_executable=executable,
            git_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        )
        return store, cwd, helper_file

    try:
        yield create
    finally:
        # Always clean the fixture group, including when testing the regressed
        # implementation or when an assertion fails before readback completes.
        if group_file.exists():
            try:
                os.killpg(int(group_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if helper_file.exists():
            _wait_stopped(int(helper_file.read_text()))


def test_exited_leader_with_pipe_holding_helper_is_stopped_on_deadline(
    source_command, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cwd, helper_file = source_command(close_pipes=False)
    monkeypatch.setattr(tool_sources, "SOURCE_TIMEOUT_SECONDS", 1)
    with pytest.raises(StateMismatchError, match="deadline exceeded"):
        store._git(cwd, "--version")
    assert helper_file.exists(), "The real fixture must have spawned its helper"
    assert _wait_stopped(int(helper_file.read_text())), "A source helper escaped its deadline"


def test_completed_leader_with_closed_pipe_helper_is_stopped_on_success(
    source_command, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cwd, helper_file = source_command(close_pipes=True)
    monkeypatch.setattr(tool_sources, "SOURCE_TIMEOUT_SECONDS", 1)
    assert store._git(cwd, "--version") == b"fixture-completed\n"
    assert helper_file.exists(), "The real fixture must have spawned its helper"
    assert _wait_stopped(int(helper_file.read_text())), (
        "A source helper outlived its completed read"
    )
