"""Real offline Git acquisition verifies source bytes without remote execution."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools import tool_sources
from potbot.tools.tool_sources import GitToolSourceStore, source_identity

REPOSITORY = "https://github.com/example/reviewed-tool.git"


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return result.stdout.strip()


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "--quiet", "--template=")
    git(remote, "config", "user.name", "Fixture")
    git(remote, "config", "user.email", "fixture@example.invalid")
    (remote / "README.md").write_bytes(b"Pinned fixture source\n")
    (remote / "run.py").write_bytes(b"print('fixture')\n")
    (remote / "run.py").chmod(0o755)
    git(remote, "add", ".")
    git(remote, "commit", "--quiet", "-m", "source fixture")
    commit = git(remote, "rev-parse", "HEAD")
    executable = Path(shutil.which("git") or "/usr/bin/git").resolve()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    store = GitToolSourceStore(
        root=cache,
        git_executable=executable,
        git_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )

    def offline(checkout: Path, repository: str, pinned: str) -> None:
        assert repository == REPOSITORY
        # Test-only transport replacement; production never admits file://.
        store._git(
            checkout,
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "--no-recurse-submodules",
            str(remote),
            pinned,
        )

    monkeypatch.setattr(store, "_fetch", offline)
    return store, remote, commit


def test_real_git_acquire_review_materialize_replay(source, tmp_path: Path, monkeypatch):
    store, _, commit = source
    operation = uuid4()
    receipt = store.acquire(operation, REPOSITORY, commit)
    assert receipt.files == 2
    assert receipt.bytes == len(b"Pinned fixture source\nprint('fixture')\n")
    assert receipt.commit == commit
    assert len(receipt.source_sha256) == 64
    assert "path" not in receipt.to_dict()
    assert store.inspect(operation) == receipt
    monkeypatch.setattr(store, "_fetch", lambda *_: pytest.fail("committed replay refetched"))
    assert store.acquire(operation, REPOSITORY, commit) == receipt
    destination = tmp_path / "build"
    assert store.materialize(operation, destination) == receipt
    assert git(destination / "checkout", "rev-parse", "HEAD") == commit
    assert git(destination / "checkout", "remote", "get-url", "origin") == REPOSITORY
    assert git(destination / "checkout", "status", "--porcelain") == ""
    assert (destination / "checkout/run.py").stat().st_mode & 0o111
    assert all((destination / name).is_dir() for name in ("environment", "static", "downloads"))
    with pytest.raises(StateMismatchError):
        store.materialize(operation, destination)
    with pytest.raises(StateMismatchError):
        store.acquire(operation, REPOSITORY, "0" * 40)


@pytest.mark.parametrize(
    "repository",
    [
        "https://github.com/owner/repo",
        "http://github.com/owner/repo.git",
        "https://github.com/owner/repo.git?token=private",
        "https://user@github.com/owner/repo.git",
        "https://github.com.evil.invalid/owner/repo.git",
        "https://github.com/../repo.git",
        "https://github.com/owner/.git",
        "git@github.com:owner/repo.git",
        "file:///tmp/repo.git",
    ],
)
def test_public_source_policy(repository):
    with pytest.raises(InvalidInputError):
        source_identity(repository, "a" * 40)


@pytest.mark.parametrize("commit", ["main", "a" * 7, "A" * 40, "a" * 39, "a" * 40 + "\n"])
def test_exact_commit_policy(commit):
    with pytest.raises(InvalidInputError):
        source_identity(REPOSITORY, commit)


def test_rejects_symlink_source_before_checkout(source):
    store, remote, _ = source
    (remote / "escape").symlink_to("/etc/passwd")
    git(remote, "add", ".")
    git(remote, "commit", "--quiet", "-m", "untrusted link")
    operation = uuid4()
    with pytest.raises(ScopeDeniedError):
        store.acquire(operation, REPOSITORY, git(remote, "rev-parse", "HEAD"))
    assert not (store.root / operation.hex / "tree/checkout/escape").exists()
    with pytest.raises(StateMismatchError):
        store.acquire(operation, REPOSITORY, git(remote, "rev-parse", "HEAD"))


def test_rejects_submodule_source_before_checkout(source):
    store, remote, commit = source
    git(remote, "update-index", "--add", "--cacheinfo", f"160000,{commit},nested")
    git(remote, "commit", "--quiet", "-m", "untrusted submodule")
    with pytest.raises(ScopeDeniedError):
        store.acquire(uuid4(), REPOSITORY, git(remote, "rev-parse", "HEAD"))


def test_modified_cache_refused_and_materialization_not_created(source, tmp_path):
    store, _, commit = source
    operation = uuid4()
    store.acquire(operation, REPOSITORY, commit)
    (store.root / operation.hex / "tree/checkout/README.md").write_text("tampered")
    with pytest.raises(StateMismatchError):
        store.inspect(operation)
    with pytest.raises(StateMismatchError):
        store.materialize(operation, tmp_path / "build")
    assert not (tmp_path / "build").exists()


def test_host_git_pin_checked_before_every_subprocess(source, monkeypatch):
    store, _, commit = source
    store.git_sha256 = "0" * 64
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("bad Git pin executed"))
    with pytest.raises(ScopeDeniedError):
        store.acquire(uuid4(), REPOSITORY, commit)


def test_cache_permissions_and_redirects_refused(source, tmp_path):
    store, _, _ = source
    store.root.chmod(0o755)
    with pytest.raises(ScopeDeniedError):
        GitToolSourceStore(root=store.root, git_executable=store.git, git_sha256=store.git_sha256)
    link = tmp_path / "redirect"
    link.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ScopeDeniedError):
        GitToolSourceStore(root=link, git_executable=store.git, git_sha256=store.git_sha256)


def test_source_file_limits_before_checkout(source, monkeypatch):
    store, _, commit = source
    monkeypatch.setattr(tool_sources, "MAX_FILE_BYTES", 2)
    operation = uuid4()
    with pytest.raises(ScopeDeniedError):
        store.acquire(operation, REPOSITORY, commit)
    assert not (store.root / operation.hex / "tree/checkout/README.md").exists()


def test_partial_attempt_is_never_overwritten(source):
    store, _, commit = source
    operation = uuid4()
    (store.root / operation.hex).mkdir(mode=0o700)
    with pytest.raises(StateMismatchError):
        store.acquire(operation, REPOSITORY, commit)


def test_source_configuration_never_inherits_credentials(source, monkeypatch):
    store, _, _ = source
    for name in (
        "HTTPS_PROXY",
        "GIT_CONFIG_COUNT",
        "GIT_SSH_COMMAND",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GIT_CONFIG_GLOBAL",
        "HOME",
    ):
        monkeypatch.setenv(name, "must-not-inherit")
    environment = store._environment()
    assert "must-not-inherit" not in environment.values()
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["HOME"] == str(store.empty_home)


def test_source_transport_rejects_implicit_netrc_credentials(source):
    store, _, commit = source
    (store.empty_home / ".netrc").write_text("machine github.com login private password secret")
    with pytest.raises(ScopeDeniedError):
        store.acquire(uuid4(), REPOSITORY, commit)


def test_destination_parent_symlink_refused(source, tmp_path):
    store, _, commit = source
    operation = uuid4()
    store.acquire(operation, REPOSITORY, commit)
    parent = tmp_path / "linked"
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ScopeDeniedError):
        store.materialize(operation, parent / "new")


def test_untrusted_checkout_attributes_cannot_start_host_command(source, tmp_path):
    store, remote, _ = source
    (remote / ".gitattributes").write_text("* filter=hosttrap\n")
    git(remote, "add", ".")
    git(remote, "commit", "--quiet", "-m", "untrusted attributes")
    operation = uuid4()
    receipt = store.acquire(operation, REPOSITORY, git(remote, "rev-parse", "HEAD"))
    assert receipt.files == 3
    assert store.inspect(operation) == receipt


def test_receipt_and_git_object_corruption_fail_closed(source):
    store, _, commit = source
    operation = uuid4()
    store.acquire(operation, REPOSITORY, commit)
    config = store.root / operation.hex / "tree/checkout/.git/config"
    config.write_text(config.read_text() + "\n[credential]\nhelper = !echo hosttrap\n")
    with pytest.raises(StateMismatchError):
        store.inspect(operation)
