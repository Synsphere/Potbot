"""The production WSL operator must include and preflight its database adapter."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.windows_core_runtime import _ROOT_BOOTSTRAP


def _operator_probe() -> str:
    tail = _ROOT_BOOTSTRAP.split('progress "service-runtime-probe"', 1)[1]
    tokens = shlex.split(tail.replace("\\\n", ""))
    # The first probe uses the installed venv as the actual service account.
    position = tokens.index("/usr/sbin/runuser")
    assert tokens[position:position + 7] == [
        "/usr/sbin/runuser", "-u", "synoperator", "--",
        "$source_root/.venv/bin/python", "-I", "-B",
    ]
    assert tokens[position + 7] == "-c"
    return tokens[position + 8]


def test_production_bootstrap_selects_database_runtime_extra_without_dev_group():
    command = _ROOT_BOOTSTRAP.split('"$uv_bin" sync ', 1)[1].split("\ntest -x", 1)[0]
    assert "--no-dev" in shlex.split(command.replace("\\\n", ""))
    selected = set(re.findall(r"--extra ([a-z][a-z-]+)", command))
    assert "postgres" in selected
    assert "embeddings" not in selected  # The operator does not need a Torch download.


@pytest.mark.parametrize("missing_adapter", [False, True])
def test_service_user_probe_checks_database_import_before_operator_launch(missing_adapter):
    if not missing_adapter:
        pytest.importorskip("psycopg", reason="positive preflight needs the postgres extra")
    source = Path(__file__).resolve().parents[2] / "src"
    code = "import sys; sys.path.insert(0, sys.argv[1]);\n"
    if missing_adapter:
        code += """import importlib.abc
class MissingDatabaseAdapter(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'psycopg' or fullname.startswith('psycopg.'):
            raise ModuleNotFoundError('production adapter missing', name='psycopg')
sys.meta_path.insert(0, MissingDatabaseAdapter())
"""
    code += _operator_probe()
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(source)],
        capture_output=True, text=True, timeout=20,
    )
    if missing_adapter:
        assert result.returncode != 0
        assert "ModuleNotFoundError: production adapter missing" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
