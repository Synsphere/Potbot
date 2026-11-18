"""Offline safety, first-run credential and UI lifecycle contracts."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot import frontend_worker, oneclick, startup
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError


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


def test_native_bootstrap_recovers_a_saved_administrator_as_application_login(
    monkeypatch, tmp_path: Path
) -> None:
    """The first native boot must not bind the superuser as a workspace principal."""
    from potbot import native_postgres
    from potbot.persistence import database

    admin_url = "postgresql://potbot_admin:private@127.0.0.1:55436/potbot?sslmode=disable"
    memory = {"postgres.application": admin_url}
    recovered = SimpleNamespace(workspace_id=uuid4(), principal_id=uuid4())
    events: list[str] = []

    class Store:
        def __init__(self, _profile: str) -> None:
            pass

        def get(self, key: str) -> str | None:
            return memory.get(key)

    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.setattr(oneclick, "CredentialStore", Store)
    monkeypatch.setattr(native_postgres, "provision_if_needed", lambda root: True)
    monkeypatch.setattr(oneclick, "resolve_postgres_dsn", lambda purpose: admin_url)
    monkeypatch.setattr(database, "migrate", lambda *args: events.append("migrate"))

    def reject_role(app: str, admin: str, schema: str) -> str:
        assert (app, admin, schema) == (admin_url, admin_url, "potbot")
        events.append("reject_admin")
        raise ScopeDeniedError("Administrator cannot act as the application")

    monkeypatch.setattr(oneclick, "_verified_application_role", reject_role)
    monkeypatch.setattr(oneclick, "_existing_context", lambda *args, **kwargs: pytest.fail("Do not bind admin"))
    monkeypatch.setattr(oneclick, "_new_workspace", lambda *args: pytest.fail("Do not bind admin"))

    def create_restricted(admin: str, schema: str, role_root: Path):
        assert (admin, schema, role_root) == (admin_url, "potbot", tmp_path)
        events.append("new_restricted_login")
        return recovered

    monkeypatch.setattr(oneclick, "_create_restricted_login", create_restricted)
    assert oneclick._provision_database(tmp_path) is recovered
    assert events == ["migrate", "reject_admin", "new_restricted_login"]


def test_explicit_admin_override_does_not_get_replaced_silently(
    monkeypatch, tmp_path: Path
) -> None:
    from potbot import native_postgres
    from potbot.persistence import database

    admin_url = "postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?sslmode=disable"
    monkeypatch.setenv("POTBOT_DSN", admin_url)
    monkeypatch.setattr(native_postgres, "provision_if_needed", lambda root: True)
    monkeypatch.setattr(oneclick, "resolve_postgres_dsn", lambda purpose: admin_url)
    monkeypatch.setattr(database, "migrate", lambda *args: None)
    monkeypatch.setattr(oneclick, "CredentialStore", lambda profile: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr(oneclick, "_verified_application_role", lambda *args: (_ for _ in ()).throw(
        ScopeDeniedError("Administrator cannot be application login")
    ))
    monkeypatch.setattr(oneclick, "_create_restricted_login", lambda *args: pytest.fail(
        "Explicit connection must never be replaced"
    ))
    with pytest.raises(InvalidInputError, match="POTBOT_DSN"):
        oneclick._provision_database(tmp_path)


def test_native_bootstrap_reuses_a_valid_existing_application_login(
    monkeypatch, tmp_path: Path
) -> None:
    from potbot import native_postgres
    from potbot.persistence import database
    from contextlib import nullcontext

    admin_url = "postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?sslmode=disable"
    app_url = "postgresql://potbot_app_test:secret@127.0.0.1:55436/potbot?sslmode=disable"
    scope = SimpleNamespace(workspace_id=uuid4(), principal_id=uuid4())
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.setattr(native_postgres, "provision_if_needed", lambda root: True)
    monkeypatch.setattr(oneclick, "resolve_postgres_dsn", lambda purpose: admin_url)
    monkeypatch.setattr(database, "migrate", lambda *args: None)
    monkeypatch.setattr(oneclick, "CredentialStore", lambda profile: SimpleNamespace(
        get=lambda key: app_url if key == "postgres.application" else None
    ))
    monkeypatch.setattr(oneclick, "_verified_application_role", lambda *args: "potbot_app_test")
    monkeypatch.setattr(oneclick, "_existing_context", lambda *args, **kwargs: scope)
    monkeypatch.setattr(oneclick, "_create_restricted_login", lambda *args: pytest.fail(
        "Existing restricted login must be reused"
    ))
    monkeypatch.setattr(database, "Database", lambda *args: SimpleNamespace(transaction=lambda ctx: nullcontext()))
    assert oneclick._provision_database(tmp_path) is scope


@pytest.mark.parametrize("url,expected", [
    ("postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?sslmode=disable", True),
    ("postgresql://potbot_admin:secret@127.0.0.1:55436/potbot", True),
    ("postgresql://potbot_admin:secret@127.0.0.1:55436/other", False),
    ("postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?host=elsewhere", False),
    ("postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?sslmode=disable&port=9999", False),
    ("postgresql://potbot_admin:secret@example.com:55436/potbot", False),
    ("postgresql://potbot_admin:secret@127.0.0.1:55437/potbot", False),
])
def test_native_endpoint_check_rejects_redirects(url: str, expected: bool) -> None:
    assert oneclick._is_native_database_url(url) is expected


def test_verified_application_role_rejects_privileged_identity_before_workspace_lookup(
    monkeypatch
) -> None:
    """A successful password login is not enough to authorize a workspace."""
    import sys
    from contextlib import nullcontext
    from potbot.persistence import database

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query: str):
            assert query == "SELECT session_user"
            return SimpleNamespace(fetchone=lambda: ("potbot_admin",))

    monkeypatch.setitem(
        sys.modules, "psycopg", SimpleNamespace(connect=lambda *args, **kwargs: Connection(), Error=Exception)
    )
    monkeypatch.setattr(database, "admin_connection", lambda *_: nullcontext("checked-admin"))
    validated: list[str] = []

    def reject(_conn, schema: str, role: str) -> None:
        validated.append(role)
        assert schema == "potbot"
        raise ScopeDeniedError("Application transactions require a restricted database login")

    monkeypatch.setattr(database, "_restricted_login", reject)
    with pytest.raises(ScopeDeniedError):
        oneclick._verified_application_role("app-dsn", "admin-dsn", "potbot")
    assert validated == ["potbot_admin"]
