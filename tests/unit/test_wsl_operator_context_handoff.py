"""Exercise the real WSL scope writer, manifest parser and context reader together."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context.operator_bootstrap import _managed_context_scope
from potbot.tools.operator_cli import parse_manifest

pytestmark = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="The WSL finalize contract requires an unprivileged POSIX account",
)


@pytest.fixture
def finalized(tmp_path, monkeypatch):
    from potbot import wsl_core_runtime as runtime

    workspace, principal = uuid4(), uuid4()
    values = {
        "workspace_id": str(workspace), "principal_id": str(principal),
        "schema": "custom_workspace_schema", "image_digest": "a" * 64,
        "image_source": "b" * 40, "qualification_sha256": "c" * 64,
    }
    for key in (
        "home", "private", "rootfs", "model_staging", "shared_models",
        "shared_tools", "ai_config", "ai_past",
    ):
        directory = tmp_path / key
        directory.mkdir(mode=0o700)
        values[key] = str(directory)
    dsn = "postgresql://restricted:test-postgres-password@127.0.0.1:55436/potbot"
    monkeypatch.setenv("POTBOT_DSN", dsn)
    monkeypatch.setenv("POTBOT_XPRA_BRIDGE_TOKEN", "x" * 48)
    command = ["finalize"]
    for key, value in values.items():
        command.extend(("--" + key.replace("_", "-"), value))
    # Actual SQLite ledger creation/image pin, atomic files and CLI parser.
    assert runtime.main(command) == 0
    private = Path(values["private"])
    return SimpleNamespace(
        runtime=runtime,
        workspace=workspace, principal=principal, schema=values["schema"], dsn=dsn,
        private=private, scope=private / "operator-context.json",
        manifest=private / "operator.json", state=private / "core-state.json",
    )


def test_finalize_scope_is_consumable_and_contains_no_credentials(finalized, capsys):
    rig = finalized
    paths = parse_manifest(rig.manifest)
    assert _managed_context_scope(paths, str(rig.scope)) == (
        rig.workspace, rig.principal, rig.schema,
    )
    assert json.loads(rig.scope.read_text()) == {
        "profile": "potbot.operator-context/1", "workspace_id": str(rig.workspace),
        "principal_id": str(rig.principal), "schema": rig.schema,
    }
    assert rig.scope.stat().st_mode & 0o077 == 0
    assert rig.scope.stat().st_uid == os.geteuid()
    output = capsys.readouterr()
    for contents in [output.out, output.err, *(p.read_text() for p in rig.private.glob("*.json"))]:
        assert "test-postgres-password" not in contents
        assert "x" * 48 not in contents


def test_runtime_hands_exact_scope_to_operator_before_launch(monkeypatch, finalized):
    from potbot.syn_release.windows_custody import CUSTODY_ENVIRONMENT, REQUIRED_MARKER
    from potbot.tools import operator_attachment_s13

    rig = finalized
    runtime = rig.runtime
    for name in (*CUSTODY_ENVIRONMENT, REQUIRED_MARKER):
        monkeypatch.setenv(name, "nonempty-test-capability")
    monkeypatch.setenv("POTBOT_OPERATOR_CONTEXT_CONFIG", "/unrelated/ambient-scope.json")
    # Root-owned source staging, the host monitor and processes need real WSL.
    monkeypatch.setattr(runtime, "_protected_root", lambda path: path)
    monkeypatch.setattr(runtime, "_monitor_environment", lambda: {})
    monkeypatch.setattr(
        operator_attachment_s13, "OperatorAttachmentClient",
        lambda **kw: SimpleNamespace(
            mounts_sha256=kw["mounts_sha256"], healthy=lambda: True, require_healthy=lambda: None,
        ),
    )
    children = []
    order = []

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, **kwargs):
            assert self.returncode == 0
            return self.returncode

    def start(name, env):
        assert env["POTBOT_OPERATOR_REQUIRED"] == "1"
        assert env["POTBOT_OPERATOR_CONTEXT_CONFIG"] == str(rig.scope)
        assert env["POTBOT_DSN"] == rig.dsn
        assert _managed_context_scope(
            parse_manifest(rig.manifest), env["POTBOT_OPERATOR_CONTEXT_CONFIG"],
        ) == (rig.workspace, rig.principal, rig.schema)
        order.append(name)
        child = Child()
        children.append(child)
        return child

    monkeypatch.setattr(runtime, "_start_secret_service", lambda source, env, stop: start("secrets", env))

    def spawn(argv, *, env, stdin):
        assert "test-postgres-password" not in " ".join(argv)
        if "potbot.startup" in argv:
            assert argv[argv.index("--manifest") + 1] == str(rig.manifest)
            assert order == ["secrets"]
            return start("operator", env)
        assert "potbot.tools.x11_workspace_server" in argv
        assert order == ["secrets", "operator"]
        return start("xpra", env)

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "_await_xpra", lambda process, workspace, token, stop: stop.set())
    assert runtime._run_core_owned(SimpleNamespace(state=str(rig.state)), threading.Event()) == 0
    assert order == ["secrets", "operator", "xpra"]
    assert all(child.poll() == 0 for child in children)
