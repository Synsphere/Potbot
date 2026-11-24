"""Windows GUI bootstrap must never pass spaced npm.cmd paths through cmd.exe."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot import frontend_worker
from potbot.domain.errors import CapabilityUnavailableError


def _installed_node(tmp_path: Path) -> tuple[Path, Path, Path]:
    # A genuine C:\\Program Files layout is not required to reproduce the
    # quoting regression: a space inside any component exercises the contract.
    location = tmp_path / "Program Files" / "nodejs"
    location.mkdir(parents=True)
    node = location / "node.exe"
    npm = location / "npm.cmd"
    cli = location / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    for file in (node, npm, cli):
        file.write_text("test placeholder", encoding="utf-8")
    return node, npm, cli


def test_windows_npm_uses_node_js_directly_for_spaced_paths(monkeypatch, tmp_path: Path):
    node, npm, cli = _installed_node(tmp_path)
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(frontend_worker, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(frontend_worker.subprocess, "run", run)
    env = {"PATH": "test"}
    frontend_worker._run_npm(
        npm, ["ci", "--no-audit", "--no-fund"], cwd=tmp_path, env=env, node=node
    )
    frontend_worker._run_npm(npm, ["run", "build"], cwd=tmp_path, env=env, node=node)
    assert [item[0] for item in seen] == [
        [str(node), str(cli), "ci", "--no-audit", "--no-fund"],
        [str(node), str(cli), "run", "build"],
    ]
    assert all(item[1]["cwd"] == tmp_path and item[1]["env"] is env for item in seen)
    assert all("cmd.exe" not in item[0] for item in seen)


def test_windows_without_npm_cli_fails_before_starting_any_process(monkeypatch, tmp_path):
    node, npm, cli = _installed_node(tmp_path)
    cli.unlink()
    monkeypatch.setattr(frontend_worker, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        frontend_worker.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Do not run broken npm.cmd"),
    )
    with pytest.raises(CapabilityUnavailableError, match="npm-cli.js"):
        frontend_worker._run_npm(npm, ["ci"], cwd=tmp_path, env={}, node=node)


def test_posix_npm_still_executes_its_native_binary(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(frontend_worker, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        frontend_worker.subprocess,
        "run",
        lambda command, **kw: (calls.append(command), SimpleNamespace(returncode=0))[1],
    )
    npm = tmp_path / "node runtime" / "bin" / "npm"
    frontend_worker._run_npm(npm, ["ci"], cwd=tmp_path, env={})
    assert calls == [[str(npm), "ci"]]
