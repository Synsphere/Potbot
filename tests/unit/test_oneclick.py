"""Offline safety, first-run credential and UI lifecycle contracts."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot import frontend_worker, oneclick, startup
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError


def test_application_url_uses_separate_restricted_login_and_encodes_password() -> None:
    result = oneclick._application_url(
        "postgresql://admin:old@localhost:55432/potbot?sslmode=require",
        "potbot_role", "private@:%secret",
    )
    assert result == (
        "postgresql://potbot_role:private%40%3A%25secret@localhost:55432/potbot"
        "?sslmode=require"
    )
    with pytest.raises(InvalidInputError):
        oneclick._application_url("dbname=potbot user=postgres", "user", "secret")


def test_generated_credentials_are_reused_and_never_written_to_frontend(monkeypatch) -> None:
    memory = {}
    class Store:
        def __init__(self, _profile):
            pass
        def get(self, key):
            return memory.get(key)
        def set(self, key, value):
            memory[key] = value
    monkeypatch.setattr(oneclick, "CredentialStore", Store)
    assert oneclick._ensure_secrets() == memory["ui.password"]
    assert oneclick._ensure_secrets() is None
    assert {"api.access_token", "api.setup_token", "ui.password", "ui.cookie_secret"} == set(memory)
    assert all(len(value) >= 32 for value in memory.values())


def test_frontend_environment_uses_vault_not_checkout_files(monkeypatch):
    content = {"api.access_token": "s" * 48, "ui.password": "p" * 48,
               "ui.cookie_secret": "c" * 48, "api.setup_token": "t" * 48}
    store = SimpleNamespace(get=content.get)
    config = SimpleNamespace(host="127.0.0.1", port=8765, workspace_id=str(uuid4()))
    monkeypatch.setenv("POTBOT_UI_WORKSPACES_JSON", '[{"id":"wrong"}]')
    env = frontend_worker.frontend_environment(config, store)
    assert env["POTBOT_UI_BACKEND_URL"] == "http://127.0.0.1:8765"
    assert env["POTBOT_UI_BACKEND_TOKEN"] == content["api.access_token"]
    assert env["POTBOT_UI_PASSWORD"] == content["ui.password"]
    assert env["POTBOT_UI_SETUP_TOKEN"] == content["api.setup_token"]
    assert "POTBOT_UI_WORKSPACES_JSON" not in env


def test_frontend_fingerprint_does_not_include_env_or_built_artifacts(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package-lock.json").write_text('{"name":"test"}')
    first = frontend_worker._frontend_fingerprint(frontend)
    (frontend / ".env.local").write_text("SECRET=dont-hash-me")
    (frontend / ".next").mkdir()
    (frontend / ".next" / "build.json").write_text("generated")
    assert frontend_worker._frontend_fingerprint(frontend) == first
    (frontend / "package-lock.json").write_text('{"name":"changed"}')
    assert frontend_worker._frontend_fingerprint(frontend) != first


def test_untrusted_node_archive_path_is_rejected(tmp_path: Path):
    archive = tmp_path / "node.zip"
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr("../../escaped", b"bad")
    with pytest.raises(CapabilityUnavailableError):
        frontend_worker._extract_release(archive, tmp_path / "stage", "node-v22.16.0-win-x64.zip")
    assert not (tmp_path / "escaped").exists()


def test_status_has_no_setup_side_effect(monkeypatch, tmp_path, capsys):
    from potbot import service
    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "missing.json")
    assert oneclick.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "configured": False, "backend_and_gui_healthy": False, "gui_url": None,
    }


def test_default_launcher_never_starts_work_on_failed_frontend_setup(monkeypatch, tmp_path):
    from potbot import service
    calls = []
    monkeypatch.setattr(oneclick, "project_root", lambda: tmp_path)
    monkeypatch.setattr(oneclick, "_provision_database", lambda _: SimpleNamespace(workspace_id=uuid4(), principal_id=uuid4()))
    monkeypatch.setattr(oneclick, "_policy_path", lambda _: tmp_path / "policy.json")
    monkeypatch.setattr(oneclick, "_ensure_secrets", lambda: None)
    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "service.json")
    monkeypatch.setattr(service, "install_manager", lambda **kw: calls.append("install"))
    monkeypatch.setattr(service, "_atomic_configuration", lambda _: calls.append("save"))
    monkeypatch.setattr(frontend_worker, "prepare_frontend", lambda _: (_ for _ in ()).throw(CapabilityUnavailableError("build failed")))
    assert oneclick.main([]) == 1
    assert calls == []


def test_supported_node_release_does_not_depend_on_latest_network_listing(monkeypatch):
    monkeypatch.setattr(frontend_worker.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(frontend_worker, "os", SimpleNamespace(name="nt"))
    assert frontend_worker._node_release_name() == "node-v22.16.0-win-x64.zip"
