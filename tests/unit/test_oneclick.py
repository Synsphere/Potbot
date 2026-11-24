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
        "potbot_role",
        "private@:%secret",
    )
    assert result == (
        "postgresql://potbot_role:private%40%3A%25secret@localhost:55432/potbot?sslmode=require"
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
    monkeypatch.setattr(oneclick, "_prompt_gui_password", lambda: "a passphrase I actually chose")
    assert oneclick._ensure_secrets() is True
    assert memory["ui.password"] == "a passphrase I actually chose"
    assert memory["ui.password_confirmed"] == "operator-chosen-v1"
    assert all(
        len(memory[key]) >= 32
        for key in ("api.access_token", "api.setup_token", "ui.cookie_secret")
    )
    assert oneclick._ensure_secrets() is False
    assert len(memory) == 5


def test_confirmed_legacy_short_cookie_secret_rotates_without_password_prompt(monkeypatch) -> None:
    memory = {
        "api.access_token": "a" * 42,
        "api.setup_token": "s" * 42,
        "ui.password": "my operator chosen password",
        "ui.cookie_secret": "legacy-short-secret",
        "ui.password_confirmed": "operator-chosen-v1",
    }

    class Store:
        def __init__(self, _profile):
            pass

        def get(self, key):
            return memory.get(key)

        def set(self, key, value):
            memory[key] = value

    monkeypatch.setattr(oneclick, "CredentialStore", Store)
    monkeypatch.setattr(
        oneclick,
        "_prompt_gui_password",
        lambda: pytest.fail("A confirmed operator password must be retained"),
    )
    assert oneclick._ensure_secrets() is True
    assert memory["ui.password"] == "my operator chosen password"
    assert len(memory["ui.cookie_secret"]) >= 32
    assert memory["ui.cookie_secret"] != "legacy-short-secret"
    assert oneclick._ensure_secrets() is False


def test_frontend_environment_uses_vault_not_checkout_files(monkeypatch):
    content = {
        "api.access_token": "s" * 48,
        "ui.password": "p" * 48,
        "ui.cookie_secret": "c" * 48,
        "api.setup_token": "t" * 48,
    }
    store = SimpleNamespace(get=content.get)
    config = SimpleNamespace(host="127.0.0.1", port=8765, workspace_id=str(uuid4()))
    monkeypatch.setenv("POTBOT_UI_WORKSPACES_JSON", '[{"id":"wrong"}]')
    env = frontend_worker.frontend_environment(config, store)
    assert env["POTBOT_UI_BACKEND_URL"] == "http://127.0.0.1:8765"
    assert env["POTBOT_UI_BACKEND_TOKEN"] == content["api.access_token"]
    assert env["POTBOT_UI_PASSWORD"] == content["ui.password"]
    assert env["POTBOT_UI_SETUP_TOKEN"] == content["api.setup_token"]
    assert "POTBOT_UI_WORKSPACES_JSON" not in env


def test_frontend_environment_refuses_legacy_short_cookie_secret() -> None:
    content = {
        "api.access_token": "s" * 48,
        "ui.password": "operator password",
        "ui.cookie_secret": "too-short",
        "api.setup_token": "t" * 48,
    }
    store = SimpleNamespace(get=content.get)
    config = SimpleNamespace(host="127.0.0.1", port=8765, workspace_id=str(uuid4()))
    with pytest.raises(CapabilityUnavailableError, match="credential setup is incomplete"):
        frontend_worker.frontend_environment(config, store)


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


def test_supervised_next_start_does_not_build_standalone_output() -> None:
    config = (frontend_worker.project_root() / "frontend" / "next.config.ts").read_text(
        encoding="utf-8"
    )
    assert 'output: "standalone"' not in config


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
        "configured": False,
        "backend_and_gui_healthy": False,
        "gui_url": None,
    }


def test_default_launcher_never_starts_work_on_failed_frontend_setup(monkeypatch, tmp_path):
    from potbot import service

    calls = []
    monkeypatch.setattr(oneclick, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        oneclick,
        "_provision_database",
        lambda _: SimpleNamespace(workspace_id=uuid4(), principal_id=uuid4()),
    )
    monkeypatch.setattr(oneclick, "_policy_path", lambda _: tmp_path / "policy.json")
    monkeypatch.setattr(oneclick, "_ensure_secrets", lambda: None)
    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "service.json")
    monkeypatch.setattr(service, "install_manager", lambda **kw: calls.append("install"))
    monkeypatch.setattr(service, "_atomic_configuration", lambda _: calls.append("save"))
    monkeypatch.setattr(
        frontend_worker,
        "prepare_frontend",
        lambda _: (_ for _ in ()).throw(CapabilityUnavailableError("build failed")),
    )
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
    monkeypatch.setattr(
        oneclick, "_existing_context", lambda *args, **kwargs: pytest.fail("Do not bind admin")
    )
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
    monkeypatch.setattr(
        oneclick, "CredentialStore", lambda profile: SimpleNamespace(get=lambda key: None)
    )
    monkeypatch.setattr(
        oneclick,
        "_verified_application_role",
        lambda *args: (_ for _ in ()).throw(
            ScopeDeniedError("Administrator cannot be application login")
        ),
    )
    monkeypatch.setattr(
        oneclick,
        "_create_restricted_login",
        lambda *args: pytest.fail("Explicit connection must never be replaced"),
    )
    with pytest.raises(InvalidInputError, match="POTBOT_DSN"):
        oneclick._provision_database(tmp_path)


def test_native_bootstrap_reuses_a_valid_existing_application_login(
    monkeypatch, tmp_path: Path
) -> None:
    from contextlib import nullcontext

    from potbot import native_postgres
    from potbot.persistence import database

    admin_url = "postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?sslmode=disable"
    app_url = "postgresql://potbot_app_test:secret@127.0.0.1:55436/potbot?sslmode=disable"
    scope = SimpleNamespace(workspace_id=uuid4(), principal_id=uuid4())
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.setattr(native_postgres, "provision_if_needed", lambda root: True)
    monkeypatch.setattr(oneclick, "resolve_postgres_dsn", lambda purpose: admin_url)
    monkeypatch.setattr(database, "migrate", lambda *args: None)
    monkeypatch.setattr(
        oneclick,
        "CredentialStore",
        lambda profile: SimpleNamespace(
            get=lambda key: app_url if key == "postgres.application" else None
        ),
    )
    monkeypatch.setattr(oneclick, "_verified_application_role", lambda *args: "potbot_app_test")
    monkeypatch.setattr(oneclick, "_existing_context", lambda *args, **kwargs: scope)
    monkeypatch.setattr(
        oneclick,
        "_create_restricted_login",
        lambda *args: pytest.fail("Existing restricted login must be reused"),
    )
    monkeypatch.setattr(
        database, "Database", lambda *args: SimpleNamespace(transaction=lambda ctx: nullcontext())
    )
    assert oneclick._provision_database(tmp_path) is scope


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?sslmode=disable", True),
        ("postgresql://potbot_admin:secret@127.0.0.1:55436/potbot", True),
        ("postgresql://potbot_admin:secret@127.0.0.1:55436/other", False),
        ("postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?host=elsewhere", False),
        (
            "postgresql://potbot_admin:secret@127.0.0.1:55436/potbot?sslmode=disable&port=9999",
            False,
        ),
        ("postgresql://potbot_admin:secret@example.com:55436/potbot", False),
        ("postgresql://potbot_admin:secret@127.0.0.1:55437/potbot", False),
    ],
)
def test_native_endpoint_check_rejects_redirects(url: str, expected: bool) -> None:
    assert oneclick._is_native_database_url(url) is expected


def test_verified_application_role_rejects_privileged_identity_before_workspace_lookup(
    monkeypatch,
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
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *args, **kwargs: Connection(), Error=Exception),
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


def test_existing_generated_gui_password_is_replaced_on_next_launch(monkeypatch):
    memory = {"ui.password": "previous-random-string", "ui.cookie_secret": "prior-session-secret"}

    class Store:
        def __init__(self, _profile):
            pass

        def get(self, key):
            return memory.get(key)

        def set(self, key, value):
            memory[key] = value

    monkeypatch.setattr(oneclick, "CredentialStore", Store)
    monkeypatch.setattr(oneclick, "_prompt_gui_password", lambda: "my chosen password is different")
    assert oneclick._ensure_secrets() is True
    assert memory["ui.password"] == "my chosen password is different"
    assert memory["ui.cookie_secret"] != "prior-session-secret"
    assert memory["ui.password_confirmed"] == "operator-chosen-v1"
    assert oneclick._ensure_secrets() is False


def test_password_prompt_requires_interactive_hidden_confirmation(monkeypatch):
    from io import StringIO
    from types import SimpleNamespace

    std_in, std_err = StringIO(), StringIO()
    std_in.isatty = lambda: True
    std_err.isatty = lambda: True
    monkeypatch.setattr(oneclick, "sys", SimpleNamespace(stdin=std_in, stderr=std_err))
    values = iter(
        [
            "my first secure password",
            "wrong confirmation",
            "my final secure password",
            "my final secure password",
        ]
    )
    seen = []

    def hidden(label):
        seen.append(label)
        return next(values)

    monkeypatch.setattr(oneclick, "_hidden_prompt", hidden)
    assert oneclick._prompt_gui_password() == "my final secure password"
    assert seen == [
        "New Potbot GUI password: ",
        "Confirm Potbot GUI password: ",
        "New Potbot GUI password: ",
        "Confirm Potbot GUI password: ",
    ]
    assert "my first secure password" not in std_err.getvalue()
    assert "my final secure password" not in std_err.getvalue()
    assert "did not match" in std_err.getvalue()


def test_no_gui_password_is_automatically_generated_in_headless_mode(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        oneclick,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(isatty=lambda: False),
            stderr=SimpleNamespace(isatty=lambda: False),
        ),
    )
    monkeypatch.setattr(
        oneclick, "_hidden_prompt", lambda *_: pytest.fail("No hidden input on headless host")
    )
    with pytest.raises(InvalidInputError, match="interactive terminal"):
        oneclick._prompt_gui_password()


def test_mismatched_passwords_do_not_change_the_native_vault(monkeypatch):
    from io import StringIO
    from types import SimpleNamespace

    stdin, stderr = StringIO(), StringIO()
    stdin.isatty = stderr.isatty = lambda: True
    monkeypatch.setattr(oneclick, "sys", SimpleNamespace(stdin=stdin, stderr=stderr))
    monkeypatch.setattr(
        oneclick,
        "_hidden_prompt",
        lambda label: "correct long string" if label.startswith("New") else "incorrect long string",
    )
    changes = []
    with pytest.raises(InvalidInputError, match="not confirmed"):
        oneclick._set_gui_password(SimpleNamespace(set=lambda *args: changes.append(args)))
    assert changes == []


def test_prompt_is_not_skipped_when_gui_is_already_healthy(monkeypatch, tmp_path):
    from potbot import native_postgres, service

    path = tmp_path / "service.json"
    path.write_text("configured")
    config = SimpleNamespace(frontend_enabled=True, scoped_worker_launcher=False)
    seen = []
    monkeypatch.setattr(oneclick, "project_root", lambda: tmp_path)
    monkeypatch.setattr(native_postgres, "is_managed", lambda _: False)
    monkeypatch.setattr(service, "config_path", lambda: path)
    monkeypatch.setattr(service.ServiceConfig, "load", lambda _: config)
    monkeypatch.setattr(oneclick, "_ensure_secrets", lambda: seen.append("prompt") or True)
    monkeypatch.setattr(oneclick, "_healthy", lambda _: True)
    monkeypatch.setattr(oneclick, "_gui_accepts_current_password", lambda: True)
    monkeypatch.setattr(oneclick, "_install_or_upgrade", lambda _: (config, False, False))
    monkeypatch.setattr(service, "manager_action", lambda action: seen.append(action))
    monkeypatch.setattr(service, "install_manager", lambda **kw: seen.append("install"))
    monkeypatch.setattr(oneclick.sys.stdout, "isatty", lambda: False)
    assert oneclick._start() == 0
    assert seen == ["prompt", "stop", "install", "start"]


def test_change_password_command_restarts_native_service(monkeypatch, tmp_path):
    from potbot import service

    path = tmp_path / "config.json"
    path.write_text("configured")
    events = []
    monkeypatch.setattr(service, "config_path", lambda: path)
    monkeypatch.setattr(oneclick, "_set_gui_password", lambda store: events.append("chosen"))
    monkeypatch.setattr(service, "manager_action", events.append)
    monkeypatch.setattr(
        service.ServiceConfig, "load", lambda path: SimpleNamespace(frontend_enabled=True)
    )
    monkeypatch.setattr(oneclick, "_healthy", lambda config: True)
    monkeypatch.setattr(oneclick, "_gui_accepts_current_password", lambda: True)
    assert oneclick.main(["change-password"]) == 0
    assert events == ["chosen", "stop", "start"]


def test_password_change_route_is_forwarded_by_startup(monkeypatch):
    calls = []
    monkeypatch.setattr(oneclick, "main", lambda arguments: calls.append(arguments) or 0)
    assert startup.main(["change-password"]) == 0
    assert calls == [["change-password"]]


def test_legacy_password_command_does_not_display_chosen_password(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(oneclick, "_change_password", lambda: calls.append("prompt") or 0)
    assert oneclick.main(["ui-password"]) == 0
    assert calls == ["prompt"]
    assert capsys.readouterr().out == ""


def test_gui_login_is_probed_on_loopback_using_vault_and_exact_origin(monkeypatch):
    from types import SimpleNamespace

    secret = "my chosen local-only password"
    monkeypatch.setattr(
        oneclick, "CredentialStore", lambda profile: SimpleNamespace(get=lambda key: secret)
    )
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, maximum):
            assert maximum == 4097
            return b'{"authenticated":true}'

    monkeypatch.setattr(
        oneclick.urllib.request,
        "build_opener",
        lambda *parts: SimpleNamespace(
            open=lambda req, timeout: (requests.append((req, timeout)), Response())[1]
        ),
    )
    assert oneclick._gui_accepts_current_password() is True
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "http://127.0.0.1:3100/api/ui/auth/login"
    assert request.get_method() == "POST"
    assert request.get_header("Origin") == "http://127.0.0.1:3100"
    assert json.loads(request.data)["password"] == secret


def test_gui_login_rejects_stale_process_without_exposing_password(monkeypatch):
    import urllib.error
    from types import SimpleNamespace

    secret = "another private passphrase"
    monkeypatch.setattr(
        oneclick, "CredentialStore", lambda profile: SimpleNamespace(get=lambda key: secret)
    )

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("http://127.0.0.1:3100/", 401, "invalid password", {}, None)

    monkeypatch.setattr(
        oneclick.urllib.request, "build_opener", lambda *parts: SimpleNamespace(open=fail)
    )
    assert oneclick._gui_accepts_current_password() is False
    with pytest.raises(CapabilityUnavailableError) as result:
        oneclick._require_gui_login()
    assert secret not in str(result.value)
    assert "port 3100" in str(result.value)


def test_confirmed_password_reused_without_prompt(monkeypatch):
    secret = "operator-chosen-v1"
    memory = {
        "api.access_token": "a" * 42,
        "api.setup_token": "s" * 42,
        "ui.password": "my own chosen password",
        "ui.cookie_secret": "c" * 48,
        "ui.password_confirmed": secret,
    }

    class Store:
        def __init__(self, _profile):
            pass

        def get(self, key):
            return memory.get(key)

        def set(self, key, value):
            memory[key] = value

    monkeypatch.setattr(oneclick, "CredentialStore", Store)
    monkeypatch.setattr(
        oneclick,
        "_prompt_gui_password",
        lambda: pytest.fail("Must not re-prompt confirmed password"),
    )
    assert oneclick._ensure_secrets() is False
    assert len(memory) == 5


def test_interrupted_password_rotation_requires_fresh_prompt(monkeypatch):
    memory = {
        "api.access_token": "a" * 42,
        "api.setup_token": "s" * 42,
        "ui.password": "incomplete partially written password",
        "ui.cookie_secret": "c" * 48,
        "ui.password_confirmed": "operator-updating-v1",
    }

    class Store:
        def __init__(self, _profile):
            pass

        def get(self, key):
            return memory.get(key)

        def set(self, key, value):
            memory[key] = value

    monkeypatch.setattr(oneclick, "CredentialStore", Store)
    monkeypatch.setattr(oneclick, "_prompt_gui_password", lambda: "new explicit choice here")
    assert oneclick._ensure_secrets() is True
    assert memory["ui.password"] == "new explicit choice here"
    assert memory["ui.password_confirmed"] == "operator-chosen-v1"


def test_failed_password_vault_write_cannot_leave_confirmed_marker(monkeypatch):
    from types import SimpleNamespace

    writes = []
    secret = "new chosen password now"
    monkeypatch.setattr(oneclick, "_prompt_gui_password", lambda: secret)

    def write(key, value):
        writes.append(key)
        if key == "ui.password":
            raise CapabilityUnavailableError("vault write failed")

    with pytest.raises(CapabilityUnavailableError):
        oneclick._set_gui_password(SimpleNamespace(set=write))
    assert writes == ["ui.password_confirmed", "ui.cookie_secret", "ui.password"]
