"""The managed context receipt is private host scope, never database authority."""

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context.operator_bootstrap import _managed_context_scope
from potbot.domain.errors import ScopeDeniedError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Managed WSL scope uses POSIX custody")


@pytest.fixture
def scope(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    paths = SimpleNamespace(workspace_id=uuid4(), controller_ledger=private / "ledger")
    principal = uuid4()
    document = {
        "profile": "potbot.operator-context/1", "workspace_id": str(paths.workspace_id),
        "principal_id": str(principal), "schema": "private_schema",
    }
    path = private / "operator-context.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return paths, principal, document, path


def test_private_scope_preserves_exact_ids_and_custom_schema(scope):
    paths, principal, _, path = scope
    assert _managed_context_scope(paths, str(path)) == (paths.workspace_id, principal, "private_schema")


@pytest.mark.parametrize("problem", ["public-file", "public-parent", "symlink", "hardlink", "other-path"])
def test_scope_refuses_unsafe_storage(scope, problem):
    paths, _, _, path = scope
    if problem == "public-file":
        path.chmod(0o644)
    elif problem == "public-parent":
        path.parent.chmod(0o755)
    elif problem == "symlink":
        target = path.with_suffix(".target")
        path.rename(target)
        path.symlink_to(target)
    elif problem == "hardlink":
        os.link(path, path.with_suffix(".alias"))
    else:
        target = path.with_suffix(".other")
        path.rename(target)
        path = target
    with pytest.raises(ScopeDeniedError):
        _managed_context_scope(paths, str(path))


@pytest.mark.parametrize("changes", [
    {"profile": "unknown"}, {"workspace_id": "bad-id"}, {"principal_id": None},
    {"schema": "public;drop table x"}, {"unexpected": "data"},
])
def test_scope_refuses_invalid_document(scope, changes):
    paths, _, document, path = scope
    document.update(changes)
    path.write_text(json.dumps(document))
    with pytest.raises(ScopeDeniedError):
        _managed_context_scope(paths, str(path))


def test_scope_refuses_oversized_or_foreign_owned_record(scope, monkeypatch):
    paths, _, _, path = scope
    original = path.read_bytes()
    path.write_bytes(b" " * 8193)
    with pytest.raises(ScopeDeniedError):
        _managed_context_scope(paths, str(path))
    path.write_bytes(original)
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ScopeDeniedError):
        _managed_context_scope(paths, str(path))
