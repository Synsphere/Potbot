"""Bare launchers start the application; explicit commands remain CLI-only."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from potbot import cli, oneclick, startup


def test_explicit_commands_are_forwarded_without_install_or_database(monkeypatch):
    received = []
    monkeypatch.setattr(cli, "main", lambda command: received.append(command) or 7)
    assert startup.main(["dependencies", "status", "--model-dir", "space in path"]) == 7
    assert received == [["dependencies", "status", "--model-dir", "space in path"]]


def test_default_launch_delegates_to_oneclick_instead_of_printing_help(monkeypatch):
    monkeypatch.setenv("POTBOT_LOCAL_UI", "web")
    received = []
    monkeypatch.setattr(oneclick, "main", lambda args: received.append(args) or 4)
    monkeypatch.setattr(cli, "main", lambda _: pytest.fail("CLI help is not a launch"))
    assert startup.main([]) == 4
    assert received == [[]]


@pytest.mark.parametrize("command", ("start", "stop", "restart", "status", "logs", "ui-password"))
def test_simple_commands_have_no_intermediate_cli(monkeypatch, command):
    monkeypatch.setenv("POTBOT_LOCAL_UI", "web")
    calls = []
    monkeypatch.setattr(oneclick, "main", lambda args: calls.append(args) or 0)
    assert startup.main([command]) == 0
    assert calls == [[command]]


def test_help_needs_no_credential_store_or_postgres(monkeypatch, capsys):
    monkeypatch.setattr(oneclick, "main", lambda _: pytest.fail("help must not start app"))
    with pytest.raises(SystemExit) as completed:
        startup.main(["--help"])
    assert completed.value.code == 0
    assert "usage: potbot" in capsys.readouterr().out


def test_startup_imports_do_not_load_optional_adapters(tmp_path: Path) -> None:
    program = """
import importlib.abc
import sys
blocked = {'keyring', 'psycopg', 'transformers', 'torch'}
class DenyAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('unexpected optional adapter: ' + fullname)
sys.meta_path.insert(0, DenyAdapters())
import potbot.startup
assert not blocked.intersection(sys.modules)
print('isolated')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"
