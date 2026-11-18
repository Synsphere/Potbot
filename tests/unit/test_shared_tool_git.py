"""Builder-writable Git metadata cannot become controller command authority."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from tests.unit.test_shared_tool_store import store as store
from tests.unit.test_shared_tool_upgrades_s9 import git, prepare
from tests.unit.test_shared_tool_upgrades_s9 import rig as rig


@pytest.mark.parametrize(
    "unsafe",
    [
        '[filter "candidate"]\n\tclean = /bin/false\n',
        '[filter "candidate"]\n\tprocess = /bin/false\n',
        "[include]\n\tpath = /tmp/untrusted-config\n",
        '[includeIf "gitdir:/"]\n\tpath = /tmp/untrusted-config\n',
        "[core]\n\tfsmonitor = /bin/false\n",
        "[core]\n\tworktree = /tmp\n",
        "[extensions]\n\tpartialClone = origin\n",
        '[diff "candidate"]\n\tcommand = /bin/false\n',
    ],
)
def test_untrusted_configuration_is_rejected_before_starting_host_git(store, monkeypatch, unsafe):
    manager, _, _, _, _, _, checkout, _ = store
    config = checkout / ".git" / "config"
    config.write_text(config.read_text() + unsafe)
    started = []

    def forbidden(*args, **kwargs):
        started.append(args)
        raise AssertionError("No host Git process may start with executable repository config")

    monkeypatch.setattr("potbot.tools.shared_tool_store.subprocess.run", forbidden)
    with pytest.raises(ScopeDeniedError):
        manager._git(checkout, "status", "--porcelain")
    assert started == []


@pytest.mark.parametrize(
    "relative",
    [
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/attributes",
        "info/grafts",
        "commondir",
        "config.worktree",
        "refs/replace/" + "a" * 40,
        "hooks/pre-commit",
        "objects/pack/fixture.promisor",
    ],
)
def test_redirecting_git_control_data_is_rejected_before_host_readback(store, relative):
    manager, _, _, _, _, _, checkout, _ = store
    target = checkout / ".git" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("untrusted")
    with pytest.raises(ScopeDeniedError):
        manager._git(checkout, "rev-parse", "HEAD")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_git_metadata_is_regular_independent_data(store, tmp_path, kind):
    manager, _, _, _, _, _, checkout, _ = store
    target = checkout / ".git" / "objects" / "unsafe"
    if kind == "symlink":
        target.symlink_to(tmp_path / "unavailable")
    elif kind == "hardlink":
        target.hardlink_to(checkout / ".git" / "config")
    else:
        os.mkfifo(target)
    with pytest.raises(ScopeDeniedError):
        manager._git(checkout, "rev-parse", "HEAD")


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_pinned_source_verification_does_not_trust_index_flags(rig, index_flag):
    checkout = rig["manager"]._location("stemlab", rig["operation_id"]) / "checkout"
    git(checkout, "update-index", index_flag, "main.py")
    (checkout / "main.py").write_text("print('unreviewed source')\n")
    assert git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""
    with pytest.raises(StateMismatchError, match="pinned commit"):
        prepare(rig)
    assert rig["executor"].calls == []


def test_tracked_mode_cannot_be_hidden_by_core_filemode_false(rig):
    checkout = rig["manager"]._location("stemlab", rig["operation_id"]) / "checkout"
    git(checkout, "config", "core.filemode", "false")
    (checkout / "main.py").chmod(0o755)
    assert git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""
    with pytest.raises(StateMismatchError, match="mode"):
        prepare(rig)
    assert rig["executor"].calls == []


def test_host_git_receives_replacement_and_attribute_guards(store, monkeypatch):
    manager, _, _, _, _, _, checkout, _ = store
    import subprocess

    original = subprocess.run
    observed = []

    def inspect(argv, **kwargs):
        observed.append((argv, kwargs["env"]))
        return original(argv, **kwargs)

    monkeypatch.setattr("potbot.tools.shared_tool_store.subprocess.run", inspect)
    manager._git(checkout, "rev-parse", "HEAD")
    argv, environment = observed[-1]
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert "core.attributesFile=/dev/null" in argv
    assert "protocol.allow=never" in argv
    assert Path(environment["GIT_CONFIG_GLOBAL"]) == Path(os.devnull)
