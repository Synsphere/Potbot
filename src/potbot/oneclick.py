"""Default start.* behaviour: provision once, supervise API/workers/GUI, open GUI.

This is intentionally separate from the advanced potbot service/admin CLI.
It never persists UI passwords or API capabilities in frontend source files.
A preexisting workspace is reused when unambiguous; destructive database
initialisation and unsupported host package installation are never guessed.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

from potbot.credentials import (CredentialStore, credential_profile, resolve_postgres_dsn,
                                validate_connection)
from potbot.dependencies import project_root
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError, DomainError, InvalidInputError, ScopeDeniedError,
)

_GUI_URL = "http://127.0.0.1:3100"


def _notice(value: str) -> None:
    print("[Potbot] " + value, file=sys.stderr, flush=True)


def _application_url(admin_dsn: str, user: str, password: str) -> str:
    """Build a distinct restricted login URL without leaking its password."""
    parts = urlsplit(admin_dsn)
    if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
        raise InvalidInputError(
            "Automatic database setup needs a PostgreSQL admin URL with a named host"
        )
    host = parts.hostname
    if ":" in host:
        host = "[" + host + "]"
    port = ":" + str(parts.port) if parts.port else ""
    return urlunsplit((
        parts.scheme, f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}",
        parts.path, parts.query, "",
    ))


def _is_native_database_url(dsn: str) -> bool:
    """Match the managed cluster exactly, rejecting URI query endpoint overrides.

    The native installer stores a loopback-only URI with sslmode=disable. A
    hostname in the URI is not enough: libpq accepts host/port overrides in
    query parameters, which must not qualify as the Potbot-owned cluster.
    """
    from potbot.native_postgres import PORT

    try:
        parsed = urlsplit(dsn)
        return (
            parsed.scheme == "postgresql"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == PORT
            and parsed.path == "/potbot"
            and parsed.fragment == ""
            and parsed.query in {"", "sslmode=disable"}
        )
    except (TypeError, ValueError):
        return False


def _verified_application_role(app_dsn: str, admin_dsn: str, schema: str) -> str:
    """Check the existing login BEFORE allocating any workspace for it.

    A historic no-argument startup could save an administrative URL under
    postgres.application. Authentication alone cannot make it an application
    identity. Use the same restrictive database check as every API transaction,
    rather than treating the connected user's name as permission to bind it.
    """
    import psycopg
    from potbot.persistence.database import _restricted_login, admin_connection

    try:
        with psycopg.connect(app_dsn, connect_timeout=5) as connection:
            identity = connection.execute("SELECT session_user").fetchone()
    except psycopg.Error:
        # PostgreSQL errors can include full DSNs and private host details.
        raise CapabilityUnavailableError(
            "The stored PostgreSQL application login could not authenticate"
        ) from None
    if identity is None or not isinstance(identity[0], str):
        raise CapabilityUnavailableError("The application login identity is unavailable")
    role: str = identity[0]
    with admin_connection(admin_dsn, schema) as connection:
        _restricted_login(connection, schema, role)
    return role


def _existing_context(
    app_dsn: str, admin_dsn: str, schema: str, *, verified_role: str | None = None
) -> WorkspaceContext | None:
    from potbot.persistence.database import admin_connection

    # Never allocate a workspace using an unchecked application identity.
    # A trusted database session supplies the role, not a browser/model input.
    login = verified_role if verified_role is not None else _verified_application_role(
        app_dsn, admin_dsn, schema
    )
    with admin_connection(admin_dsn, schema) as connection:
        rows = connection.execute(
            "SELECT p.id AS principal_id,m.workspace_id FROM principals p "
            "JOIN workspace_memberships m ON m.principal_id=p.id "
            "WHERE p.database_role=%s ORDER BY m.workspace_id LIMIT 2",
            (login,),
        ).fetchall()
    if len(rows) > 1:
        raise InvalidInputError(
            "The existing database login belongs to several workspaces; select one "
            "through the explicit 'service install' command before one-click startup"
        )
    return WorkspaceContext(rows[0]["workspace_id"], rows[0]["principal_id"]) if rows else None


def _new_workspace(admin_dsn: str, schema: str, role: str, root: Path) -> WorkspaceContext:
    from potbot.persistence.database import provision_principal, provision_workspace

    workspace = uuid4()
    sandbox = (root / ".potbot" / "workspaces" / str(workspace)).absolute()
    provision_workspace(admin_dsn, schema, "Potbot", workspace, sandbox_root=sandbox)
    return provision_principal(admin_dsn, schema, role, workspace)


def _create_restricted_login(admin_dsn: str, schema: str, root: Path) -> WorkspaceContext:
    from psycopg import sql
    from potbot.persistence.database import Database, admin_connection

    store = CredentialStore(credential_profile())
    role = "potbot_app_" + secrets.token_hex(6)
    password = secrets.token_urlsafe(36)
    app_dsn = _application_url(admin_dsn, role, password)
    # The new role has no owner privileges, inherited roles or access outside
    # the grants provisioned by Potbot's existing schema authority.
    with admin_connection(admin_dsn, schema) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS NOINHERIT").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    scope = _new_workspace(admin_dsn, schema, role, root)
    with Database(app_dsn, schema).transaction(scope):
        pass
    store.set("postgres.application", app_dsn)
    _notice("Created a restricted database login and private Potbot workspace")
    return scope



def _provision_database(root: Path) -> WorkspaceContext:
    """Migrate with separate admin authority; never use admin login for workers."""
    from potbot.persistence.database import Database, migrate

    _notice("Checking PostgreSQL and applying Potbot's versioned schema")
    from potbot.native_postgres import provision_if_needed

    managed = provision_if_needed(root)
    admin_dsn = resolve_postgres_dsn("admin")
    schema = "potbot"
    migrate(admin_dsn, schema)
    store = CredentialStore(credential_profile())
    # A past startup may have saved a superuser connection as the *application*
    # credential. That must never become an API role or an owned workspace.
    # If this is our verified native cluster, recover automatically by creating
    # a fresh restricted login and replacing the vault key only after verification.
    # An explicit POTBOT_DSN override is authoritative: never ignore it.
    explicit = os.environ.get("POTBOT_DSN")
    application = explicit or store.get("postgres.application")
    native = managed and _is_native_database_url(admin_dsn)
    if application:
        if native and not _is_native_database_url(application):
            if explicit:
                raise InvalidInputError(
                    "POTBOT_DSN points outside Potbot's native database. Remove the override "
                    "to use automatic native PostgreSQL setup."
                )
            _notice("Saved application connection targets another database; configuring a local restricted login")
            return _create_restricted_login(admin_dsn, schema, root)
        try:
            role = _verified_application_role(application, admin_dsn, schema)
        except (ScopeDeniedError, CapabilityUnavailableError):
            if explicit:
                raise InvalidInputError(
                    "POTBOT_DSN must authenticate as a restricted application login, not the "
                    "database administrator. Remove the override for automatic setup."
                ) from None
            if not native:
                raise InvalidInputError(
                    "The saved application login is unavailable or privileged. Select a "
                    "restricted login for the configured external PostgreSQL database."
                ) from None
            _notice("Saved application credential is unsuitable; creating a private restricted login")
            return _create_restricted_login(admin_dsn, schema, root)
        scope = _existing_context(application, admin_dsn, schema, verified_role=role)
        if scope is None:
            scope = _new_workspace(admin_dsn, schema, role, root)
        with Database(application, schema).transaction(scope):
            pass
        return scope
    return _create_restricted_login(admin_dsn, schema, root)


def _policy_path(root: Path) -> Path:
    from potbot.models.hosted_policy import HostedPolicy

    path = root / ".potbot" / "service" / "providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        contents = json.dumps(HostedPolicy().to_dict(), indent=2) + "\n"
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(contents)
        _notice("Created a provider policy; paid hosted inference remains OFF until enabled")
    HostedPolicy.read(path)
    return path


def _ensure_secrets() -> str | None:
    """Keep all operator and UI secrets in the native OS credential store."""
    store = CredentialStore(credential_profile())
    generated_password: str | None = None
    for key in ("api.access_token", "api.setup_token", "ui.password", "ui.cookie_secret"):
        if store.get(key) is not None:
            continue
        value = secrets.token_urlsafe(38 if key != "ui.cookie_secret" else 48)
        store.set(key, value)
        if key == "ui.password":
            generated_password = value
    return generated_password


def _install_or_upgrade(root: Path) -> tuple[Any, str | None, bool]:
    from potbot import service
    from potbot.frontend_worker import prepare_frontend

    current = service.config_path()
    first_setup = not current.is_file()
    if first_setup:
        scope = _provision_database(root)
        policy = _policy_path(root)
        config = service.ServiceConfig(
            str(scope.workspace_id), str(scope.principal_id), str(policy.resolve()),
            frontend_enabled=True,
        )
    else:
        config = service.ServiceConfig.load(current)
        # Configuration may already include an operator manifest and tested
        # native Cog profiles. Preserve all those pins rather than reinstalling.
        if not config.frontend_enabled:
            config = replace(config, frontend_enabled=True)
    password = _ensure_secrets()
    # This work happens before touching the existing service manager: a failed
    # Node download/npm build must not take down a working backend.
    prepare_frontend(root)
    if first_setup or not current.is_file() or not service.ServiceConfig.load(current).frontend_enabled:
        service._atomic_configuration(config)
    return config, password, first_setup


def _probe(url: str, *, json_key: str | None = None) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status != 200:
                return False
            if json_key:
                body = response.read(4097)
                return len(body) <= 4096 and json.loads(body).get(json_key) is True
            return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _healthy(config: Any) -> bool:
    # A generic /health result on a reused port does not prove the correct
    # authenticated Potbot installation is behind that listener.
    try:
        token = CredentialStore(credential_profile()).get("api.access_token")
        if not token:
            return False
        host = f"[{config.host}]" if ":" in config.host else config.host
        url = f"http://{host}:{config.port}/v1/ui/workspace"
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=2) as response:
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096:
                return False
            workspace = json.loads(raw).get("workspace", {})
            if not isinstance(workspace, dict) or workspace.get("id") != config.workspace_id:
                return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return _probe(_GUI_URL + "/api/ui/auth/status", json_key="configured")


def _open_browser() -> None:
    # WSL's preferred GUI browser is often the Windows host browser.
    if "microsoft" in platform_release().lower() or "WSL_INTEROP" in os.environ:
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", _GUI_URL],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open_new_tab(_GUI_URL)


def platform_release() -> str:
    import platform

    return platform.release()


def _foreground(config: Any) -> int:
    """WSL/headless fallback when no user service manager is available."""
    _notice("Native service manager unavailable; supervising Potbot in this terminal")
    child = subprocess.Popen([sys.executable, "-m", "potbot.startup", "service", "run"])
    try:
        for _ in range(90):
            if child.poll() is not None:
                raise CapabilityUnavailableError("Backend or GUI supervisor exited; inspect logs")
            if _healthy(config):
                _notice("Potbot is running at " + _GUI_URL)
                if sys.stdout.isatty():
                    _open_browser()
                return child.wait()
            time.sleep(1)
        raise CapabilityUnavailableError("Backend/GUI did not become healthy; inspect service logs")
    except KeyboardInterrupt:
        return 0
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def _start() -> int:
    from potbot import service
    from potbot.native_postgres import ensure_running, is_managed

    root = project_root()
    # A previously managed native cluster may be stopped after Windows login.
    # Start it before any backend health check or service-manager activation.
    if is_managed(root):
        ensure_running(root)
    # Opening the app again must not run npm ci or replace a live Next.js
    # build. Explicit restart stops the managed service before rebuilding.
    saved = service.config_path()
    if saved.is_file():
        current = service.ServiceConfig.load(saved)
        if current.frontend_enabled and _healthy(current):
            _notice("Potbot is already running: " + _GUI_URL)
            if sys.stdout.isatty():
                _open_browser()
            return 0
    config, new_password, first_setup = _install_or_upgrade(root)
    if new_password:
        if sys.stdout.isatty():
            print("\nPotbot GUI password (shown once): " + new_password + "\n", flush=True)
        else:
            _notice("New UI password saved in the OS vault; use an interactive terminal to reveal it")
    if not first_setup and _healthy(config):
        _notice("Potbot is already running: " + _GUI_URL)
        if sys.stdout.isatty():
            _open_browser()
        return 0
    # A new install registers the existing per-user manager. Refresh on upgrades
    # so Windows scheduled tasks and systemd/launchd keep their old privileges.
    try:
        if not first_setup:
            try:
                service.manager_action("stop")
            except (OSError, DomainError):
                pass
        service.install_manager(scoped_worker_launcher=config.scoped_worker_launcher)
        service.manager_action("start")
    except (OSError, DomainError):
        return _foreground(config)
    for _ in range(90):
        if _healthy(config):
            _notice("Backend and GUI are ready: " + _GUI_URL)
            if sys.stdout.isatty():
                _open_browser()
            return 0
        time.sleep(1)
    raise CapabilityUnavailableError("Potbot service did not become healthy; run start.* logs")


def main(arguments: list[str] | None = None) -> int:
    arguments = [] if arguments is None else arguments
    if arguments not in ([], ["start"], ["stop"], ["restart"], ["status"], ["logs"], ["ui-password"]):
        raise InvalidInputError("Unsupported simple launch command")
    try:
        from potbot import service

        action = arguments[0] if arguments else "start"
        if action == "start":
            return _start()
        if action == "status":
            path = service.config_path()
            config = service.ServiceConfig.load(path) if path.is_file() else None
            print(json.dumps({"configured": config is not None,
                              "backend_and_gui_healthy": bool(config and _healthy(config)),
                              "gui_url": _GUI_URL if config else None}))
            return 0
        if action == "logs":
            return service.main(["logs"])
        if action == "ui-password":
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise InvalidInputError("GUI password display requires an interactive terminal")
            password = CredentialStore(credential_profile()).get("ui.password")
            if not password:
                raise CapabilityUnavailableError("No GUI password is configured")
            print("Potbot GUI password: " + password)
            return 0
        if action in {"stop", "restart"}:
            if not service.config_path().is_file():
                raise InvalidInputError("Potbot has not been configured")
            service.manager_action("stop")
            _notice("Stopped the Potbot service")
            if action == "stop":
                from potbot.native_postgres import stop_if_managed

                stop_if_managed(project_root())
                return 0
            return _start()
        return 1
    except (DomainError, OSError, ValueError) as error:
        # Credential / DB helpers already sanitize exception messages. Other
        # external stderr (node, docker, npm, OS manager) is never reflected here.
        _notice(str(error) if isinstance(error, DomainError) else
                "Startup failed. Check the native credential vault and Potbot service logs")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
