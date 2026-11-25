"""Default start.* behaviour: provision once, supervise API/workers/GUI, open GUI.

This is intentionally separate from the advanced potbot service/admin CLI.
It never persists UI passwords or API capabilities in frontend source files.
A preexisting workspace is reused when unambiguous; destructive database
initialisation and unsupported host package installation are never guessed.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from potbot.credentials import (
    CredentialStore,
    _hidden_prompt,
    credential_profile,
    resolve_postgres_dsn,
)
from potbot.dependencies import project_root
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    ScopeDeniedError,
)
from potbot.instance import InstanceLease, is_running, owner_root, request_stop, wait_stopped

_GUI_URL = "http://127.0.0.1:3100"
_STARTUP_HEALTH_ATTEMPTS = 1800
_STOP_LISTENER_WAIT_SECONDS = 10


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
    return urlunsplit(
        (
            parts.scheme,
            f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}",
            parts.path,
            parts.query,
            "",
        )
    )


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
    login = (
        verified_role
        if verified_role is not None
        else _verified_application_role(app_dsn, admin_dsn, schema)
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
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS NOINHERIT"
            ).format(sql.Identifier(role), sql.Literal(password))
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
            _notice(
                "Saved application connection targets another database; configuring a local restricted login"
            )
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
            _notice(
                "Saved application credential is unsuitable; creating a private restricted login"
            )
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


def _prompt_gui_password() -> str:
    """Ask at an interactive console; never display, log, or generate a GUI password."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise InvalidInputError(
            "Choose the Potbot GUI password in an interactive terminal: run start.bat "
            "or start.ps1 without redirecting input (on Linux use start.sh)."
        )
    _notice("Choose a password for the Potbot GUI (12-256 characters)")
    for _ in range(3):
        first = _hidden_prompt("New Potbot GUI password: ")
        second = _hidden_prompt("Confirm Potbot GUI password: ")
        if (
            not 12 <= len(first) <= 256
            or not first.strip()
            or any(char in first for char in ("\x00", "\n", "\r"))
        ):
            _notice("Password must contain 12-256 characters and no line breaks; try again")
        elif not hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8")):
            _notice("Passwords did not match; try again")
        else:
            return first
    raise InvalidInputError("GUI password was not confirmed; no password was changed")


def _set_gui_password(store: CredentialStore) -> None:
    """Persist an explicitly chosen password; rotate sessions on replacement."""
    password = _prompt_gui_password()
    # The OS vault is not a multi-key transaction. Fence partial writes so a
    # crash between replacing the password and session key forces a fresh prompt
    # rather than silently accepting an incompletely rotated login.
    store.set("ui.password_confirmed", "operator-updating-v1")
    # A new session signing key invalidates old logins when Next.js reloads.
    store.set("ui.cookie_secret", secrets.token_urlsafe(48))
    store.set("ui.password", password)
    # Older automatic passwords have no marker, so the first upgraded launch
    # asks the operator even when a password was previously generated.
    store.set("ui.password_confirmed", "operator-chosen-v1")


def _ensure_secrets() -> bool:
    """Keep internal tokens automatic, but require a user-chosen GUI password."""
    store = CredentialStore(credential_profile())
    for key in ("api.access_token", "api.setup_token"):
        if store.get(key) is None:
            store.set(key, secrets.token_urlsafe(38))
    confirmed = store.get("ui.password_confirmed")
    password = store.get("ui.password")
    cookie_secret = store.get("ui.cookie_secret")
    if confirmed == "operator-chosen-v1" and password:
        # Older one-click releases accepted any non-empty cookie secret, while
        # the Next.js auth boundary correctly requires at least 32 characters.
        # Rotate only the session signing key: retain the operator's explicitly
        # chosen password and invalidate any stale browser sessions.
        if not cookie_secret or len(cookie_secret) < 32:
            store.set("ui.cookie_secret", secrets.token_urlsafe(48))
            _notice("Upgraded the GUI session signing key")
            return True
        return False
    if confirmed not in (None, "operator-chosen-v1", "operator-updating-v1"):
        raise InvalidInputError("Unknown GUI password confirmation marker in the OS vault")
    if confirmed == "operator-updating-v1":
        _notice("An earlier GUI password change was interrupted; choose a password again")
    _set_gui_password(store)
    return True


def _install_or_upgrade(root: Path) -> tuple[Any, bool, bool]:
    from potbot import service
    from potbot.frontend_worker import prepare_frontend

    current = service.config_path()
    first_setup = not current.is_file()
    if first_setup:
        scope = _provision_database(root)
        policy = _policy_path(root)
        config = service.ServiceConfig(
            str(scope.workspace_id),
            str(scope.principal_id),
            str(policy.resolve()),
            frontend_enabled=True,
        )
    else:
        config = service.ServiceConfig.load(current)
        # Configuration may already include an operator manifest and tested
        # native Cog profiles. Preserve all those pins rather than reinstalling.
        if not config.frontend_enabled:
            config = replace(config, frontend_enabled=True)
    password_chosen = _ensure_secrets()
    # This work happens before touching the existing service manager: a failed
    # Node download/npm build must not take down a working backend.
    prepare_frontend(root)
    if (
        first_setup
        or not current.is_file()
        or not service.ServiceConfig.load(current).frontend_enabled
    ):
        service._atomic_configuration(config)
    return config, password_chosen, first_setup


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
    return (
        _probe(_GUI_URL + "/api/ui/auth/status", json_key="configured")
        and _xpra_readiness(config)[0]
    )


def _xpra_readiness(config: Any) -> tuple[bool, str]:
    """Only the live owner's authenticated Xpra result can complete startup."""
    if not config.frontend_enabled:
        return True, ""
    from potbot.desktop_state import _XPRA_MESSAGES, _xpra_status
    from potbot.instance import runtime_home

    root = project_root()
    receipt = runtime_home(root) / "service.json"
    status = root / ".potbot" / "service" / "xpra-status.json"
    try:
        if owner_root("service", root=root) != root.resolve():
            return False, ""
        with receipt.open("rb") as stream:
            generation = stream.read(8193)
        if not generation or len(generation) > 8192:
            return False, ""
        # The lease receipt is atomically replaced at each supervisor start.
        # A previous generation's recently-ready/failed file must not count
        # while its replacement is still preparing WSL.
        if status.stat().st_mtime_ns < receipt.stat().st_mtime_ns:
            return False, ""
        ready, detail, failed = _xpra_status(status, config.workspace_id)
        with receipt.open("rb") as stream:
            unchanged = stream.read(8193) == generation
        if not unchanged or not is_running("service", root=root):
            return False, ""
    except (DomainError, OSError, ValueError):
        return False, ""
    terminal = {
        _XPRA_MESSAGES[stage] for stage in (
            "custody-tcp-failed", "custody-auth-failed", "custody-firewall-failed",
            "custody-firewall-declined", "restart-limit", "startup-timeout",
        )
    }
    # runtime-failed still has a supervisor retry budget; bridge forwarding can
    # also be transient. Abort only when the supervisor has blocked startup.
    return ready, detail if failed and detail in terminal else ""


def _raise_xpra_failure(config: Any) -> None:
    _, failure = _xpra_readiness(config)
    if failure:
        raise CapabilityUnavailableError("Potbot Xpra startup failed: " + failure)


def _gui_accepts_current_password() -> bool:
    """One local end-to-end login probe, not a health check or retry loop.

    /auth/status only establishes that some GUI is listening; an orphaned Next.js
    process may still hold credentials from before the user's password change.
    The existing login route verifies the actual in-memory password and origin.
    Never print or log the password, response body, or returned session cookie.
    """
    password = CredentialStore(credential_profile()).get("ui.password")
    if not password:
        return False
    payload = json.dumps({"password": password}).encode("utf-8")
    request = urllib.request.Request(
        _GUI_URL + "/api/ui/auth/login",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Origin": _GUI_URL},
    )
    try:
        # Do not allow a machine-level HTTP proxy to observe the local password.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            data = response.read(4097)
            return (
                response.status == 200
                and len(data) <= 4096
                and json.loads(data).get("authenticated") is True
            )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _require_gui_login() -> None:
    if not _gui_accepts_current_password():
        raise CapabilityUnavailableError(
            "The GUI on port 3100 did not accept the password saved in the native vault. "
            "Another or stale GUI process may still be running; close that process and "
            "restart Potbot. Open http://127.0.0.1:3100 (not localhost)."
        )


def _open_browser() -> None:
    # WSL's preferred GUI browser is often the Windows host browser.
    if "microsoft" in platform_release().lower() or "WSL_INTEROP" in os.environ:
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "", _GUI_URL],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    webbrowser.open_new_tab(_GUI_URL)


def platform_release() -> str:
    import platform

    return platform.release()


def _wait_for_existing_service(config: Any, lease: InstanceLease, *, open_browser: bool) -> int:
    _notice("An existing Potbot service is starting; waiting for it")
    for _ in range(_STARTUP_HEALTH_ATTEMPTS):
        lease.check_stop()
        if _healthy(config) and _gui_accepts_current_password():
            if open_browser and sys.stdout.isatty():
                _open_browser()
            return 0
        _raise_xpra_failure(config)
        if not is_running("service", root=lease.root):
            raise CapabilityUnavailableError(
                "The existing Potbot service exited during startup; inspect its logs."
            )
        lease.stopped.wait(1)
    raise CapabilityUnavailableError("The existing Potbot service did not become ready")


def _foreground(config: Any, *, open_browser: bool = True, lease: InstanceLease) -> int:
    """WSL/headless fallback when no user service manager is available."""
    _notice("Native service manager unavailable; supervising Potbot in this terminal")
    child = subprocess.Popen([sys.executable, "-m", "potbot.startup", "service", "run"])
    try:
        for _ in range(_STARTUP_HEALTH_ATTEMPTS):
            lease.check_stop()
            if child.poll() is not None:
                if is_running("service", root=lease.root):
                    # A queued native task may have acquired the service lease
                    # first after its manager reported a launch failure.
                    return _wait_for_existing_service(config, lease, open_browser=open_browser)
                raise CapabilityUnavailableError("Backend or GUI supervisor exited; inspect logs")
            if _healthy(config):
                _require_gui_login()
                _notice("Potbot is running at " + _GUI_URL)
                if open_browser and sys.stdout.isatty():
                    _open_browser()
                while child.poll() is None:
                    lease.check_stop()
                    lease.stopped.wait(0.5)
                return child.returncode
            _raise_xpra_failure(config)
            lease.stopped.wait(1)
        raise CapabilityUnavailableError("Backend/GUI did not become healthy; inspect service logs")
    except KeyboardInterrupt:
        return 0
    finally:
        if child.poll() is None:
            request_stop("service", root=lease.root)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)


def _start(*, open_browser: bool = True, stop_event: threading.Event | None = None) -> int:
    root = project_root()
    with InstanceLease("startup", root=root, stop_event=stop_event) as lease:
        return _start_owned(root, lease, open_browser=open_browser)


def _start_owned(root: Path, lease: InstanceLease, *, open_browser: bool) -> int:
    from potbot import service
    from potbot.native_postgres import ensure_running, is_managed

    lease.check_stop()
    running_root = owner_root("service", root=root)
    if running_root is not None and running_root != root.resolve():
        raise CapabilityUnavailableError(
            f"Potbot is running from {running_root}. Run stop.bat from that checkout first."
        )
    # A previously managed native cluster may be stopped after Windows login.
    # Start it before any backend health check or service-manager activation.
    if is_managed(root):
        ensure_running(root)
    # Opening the app again must not run npm ci or replace a live Next.js
    # build. Explicit restart stops the managed service before rebuilding.
    saved = service.config_path()
    password_chosen = _ensure_secrets() if saved.is_file() else False
    # A supervisor may still be preparing WSL. A second launcher must wait for
    # that generation instead of rebuilding assets or replacing its process.
    if saved.is_file() and not password_chosen and is_running("service", root=root):
        current = service.ServiceConfig.load(saved)
        return _wait_for_existing_service(current, lease, open_browser=open_browser)
    if saved.is_file():
        # This must run BEFORE the already-healthy fast path: older launchers
        # created random passwords without asking the operator.
        current = service.ServiceConfig.load(saved)
        if (
            current.frontend_enabled
            and not password_chosen
            and _healthy(current)
            and _gui_accepts_current_password()
        ):
            _notice("Potbot is already running: " + _GUI_URL)
            if open_browser and sys.stdout.isatty():
                _open_browser()
            return 0
        if password_chosen and is_running("service", root=root):
            service.manager_action("stop")
            lease.check_stop()
    lease.check_stop()
    config, newly_chosen, first_setup = _install_or_upgrade(root)
    lease.check_stop()
    password_chosen = password_chosen or newly_chosen
    if password_chosen:
        _notice("Your chosen GUI password is stored securely; restarting the GUI login service")
    if (
        not first_setup
        and not password_chosen
        and _healthy(config)
        and _gui_accepts_current_password()
    ):
        _notice("Potbot is already running: " + _GUI_URL)
        if open_browser and sys.stdout.isatty():
            _open_browser()
        return 0
    # A new install registers the existing per-user manager. Refresh on upgrades
    # so Windows scheduled tasks and systemd/launchd keep their old privileges.
    if os.name == "nt":
        from potbot.windows_core_runtime import stop_owned_windows_runtime
        from potbot.windows_service_control import stop_registered_task

        # Retire a pre-lease scheduled task while its process tree can still
        # be identified. A failed ownership check must never fall through to
        # another foreground supervisor.
        stop_registered_task(root)
        stop_owned_windows_runtime()
    elif not first_setup:
        try:
            service.manager_action("stop")
        except (OSError, DomainError):
            pass
    lease.check_stop()
    _require_ports_available(config)
    try:
        service.install_manager(scoped_worker_launcher=config.scoped_worker_launcher)
        lease.check_stop()
        service.manager_action("start")
    except (OSError, DomainError):
        lease.check_stop()
        # The manager can report failure after actually launching a process.
        # The supervisor's own OS lease is the final guard in this race.
        if not is_running("service", root=root):
            _require_ports_available(config)
            return _foreground(config, open_browser=open_browser, lease=lease)
    for _ in range(_STARTUP_HEALTH_ATTEMPTS):
        lease.check_stop()
        if _healthy(config):
            _require_gui_login()
            _notice("Backend, GUI and Xpra are ready: " + _GUI_URL)
            if open_browser and sys.stdout.isatty():
                _open_browser()
            return 0
        _raise_xpra_failure(config)
        lease.stopped.wait(1)
    raise CapabilityUnavailableError("Potbot service did not become healthy; run start.* logs")


def start_services(
    *, open_browser: bool = False, stop_event: threading.Event | None = None,
) -> int:
    """Start/upgrade the supervised stack for a native local client."""
    return _start(open_browser=open_browser, stop_event=stop_event)


def _busy_ports(config: Any = None) -> list[int]:
    endpoints = {("127.0.0.1", 3100), ("127.0.0.1", 8766)}
    host = getattr(config, "host", "127.0.0.1")
    host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    endpoints.add((host, getattr(config, "port", 8765)))
    busy = []
    for address, port in sorted(endpoints):
        try:
            with socket.create_connection((address, port), timeout=0.3):
                busy.append(port)
        except OSError:
            pass
    return busy


def _require_ports_available(config: Any = None) -> None:
    busy = _busy_ports(config)
    if busy:
        raise CapabilityUnavailableError(
            "Potbot cannot start another service while these ports are occupied: "
            + ", ".join(map(str, busy))
            + ". Run stop.bat first. If an older launcher or another application still "
            "owns them, close it before retrying."
        )


def _stop(*, stop_database: bool = True, timeout: float = 60) -> int:
    """Stop known generations and verify shutdown before permitting a new start."""
    from potbot import service
    from potbot.native_postgres import stop_if_managed

    root = project_root()
    with InstanceLease("stopping", root=root):
        roles = ("startup", "desktop", "service")
        for role in roles:
            running_root = owner_root(role, root=root)
            if running_root is not None and running_root != root.resolve():
                raise CapabilityUnavailableError(
                    f"Potbot is running from {running_root}. Run stop.bat from that checkout."
                )
        # Notify every owner before waiting: the desktop may be waiting for the
        # startup worker, which in turn may be waiting for the service.
        for role in roles:
            request_stop(role, root=root)
        deadline = time.monotonic() + timeout
        for role in roles:
            if not wait_stopped(role, root=root, timeout=max(0, deadline - time.monotonic())):
                raise CapabilityUnavailableError(
                    f"Potbot {role} has not exited. Shutdown is still pending; "
                    "run stop.bat again before starting another instance."
                )
        if os.name == "nt":
            from potbot.windows_core_runtime import stop_owned_windows_runtime
            from potbot.windows_service_control import stop_registered_task

            stop_registered_task(root, include_desktop=True)
            stop_owned_windows_runtime()
        elif service.config_path().is_file():
            try:
                service.manager_action("stop")
            except (DomainError, OSError):
                # A foreground run need not have a registered manager. The OS
                # leases and listener checks remain mandatory either way.
                pass
        path = service.config_path()
        config = service.ServiceConfig.load(path) if path.is_file() else None
        deadline = time.monotonic() + _STOP_LISTENER_WAIT_SECONDS
        while _busy_ports(config) and time.monotonic() < deadline:
            time.sleep(0.25)
        busy = _busy_ports(config)
        if busy:
            raise CapabilityUnavailableError(
                "Potbot shutdown could not be verified: ports "
                + ", ".join(map(str, busy))
                + " remain occupied. Close the earlier launch terminal or the application "
                "using those ports, then run stop.bat again. No new instance was started."
            )
        if stop_database:
            stop_if_managed(root)
        _notice("Potbot has stopped; no owned startup, desktop or service instance remains")
    return 0


def _change_password() -> int:
    from potbot import service

    _set_gui_password(CredentialStore(credential_profile()))
    _notice("Your chosen GUI password was saved in the OS vault")
    if service.config_path().is_file():
        config = service.ServiceConfig.load(service.config_path())
        # The running Next.js process retains the old password in memory.
        try:
            service.manager_action("stop")
            service.manager_action("start")
        except (DomainError, OSError):
            raise CapabilityUnavailableError(
                "GUI password saved but the service could not restart; run start.bat restart"
            ) from None
        for _ in range(90):
            if _healthy(config):
                _require_gui_login()
                _notice("Potbot GUI restarted; sign in with the password you chose")
                return 0
            _raise_xpra_failure(config)
            time.sleep(1)
        raise CapabilityUnavailableError(
            "GUI password saved but the service did not become healthy"
        )
    return 0


def main(arguments: list[str] | None = None) -> int:
    arguments = [] if arguments is None else arguments
    if arguments not in (
        [],
        ["start"],
        ["stop"],
        ["restart"],
        ["status"],
        ["logs"],
        ["ui-password"],
        ["change-password"],
    ):
        raise InvalidInputError("Unsupported simple launch command")
    try:
        from potbot import service

        action = arguments[0] if arguments else "start"
        if action == "start":
            return _start()
        if action == "status":
            path = service.config_path()
            config = service.ServiceConfig.load(path) if path.is_file() else None
            print(
                json.dumps(
                    {
                        "configured": config is not None,
                        "backend_and_gui_healthy": bool(config and _healthy(config)),
                        "gui_url": _GUI_URL if config else None,
                    }
                )
            )
            return 0
        if action == "logs":
            return service.main(["logs"])
        if action == "change-password":
            return _change_password()
        if action == "ui-password":
            # Legacy alias: the operator chooses a replacement; user-chosen
            # passwords must never be printed by a diagnostic command.
            return _change_password()
        if action in {"stop", "restart"}:
            _stop(stop_database=action == "stop")
            if action == "stop":
                return 0
            return _start()
        return 1
    except (DomainError, OSError, ValueError) as error:
        # Credential / DB helpers already sanitize exception messages. Other
        # external stderr (node, docker, npm, OS manager) is never reflected here.
        _notice(
            str(error)
            if isinstance(error, DomainError)
            else "Startup failed. Check the native credential vault and Potbot service logs"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
