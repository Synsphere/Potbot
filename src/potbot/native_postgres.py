"""User-owned native PostgreSQL/pgvector for first-run Potbot installations.

No Docker daemon, Windows service administrator rights or globally installed
PostgreSQL required. A pinned micromamba binary installs compatible conda-forge
server and extension packages into Potbot's private directory. PostgreSQL runs
as the current user, confined to loopback, and keeps its data across restarts.

This owner NEVER adopts an unknown data directory, password or existing server.
Explicit POTBOT_ADMIN_DSN / native-vault external PostgreSQL remain supported.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import quote

from potbot.credentials import CredentialStore, credential_profile, validate_connection
from potbot.dependencies import project_root
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

PORT = 55436
VERSION = "potbot-native-postgres/v1"
MAMBA_VERSION = "2.9.0-0"
MAMBA_RELEASE = f"https://github.com/mamba-org/micromamba-releases/releases/download/{MAMBA_VERSION}/"
# SHA-256 digests of the official immutable micromamba GitHub release artifacts.
MAMBA_RELEASES = {
    "win-64": "a6d804394b2418991c4e29562853eaace2f2ce9d9da661a98e74e02e8dbb44b0",
    "linux-64": "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3",
    "linux-aarch64": "9f93b974adcb4d166996af969b6cd371287d1a3e52733704727884d9b74cb7a7",
    "osx-64": "1e71054bb3ac9a076e21f7ec48acfef536f9b3f1408f371a942784bf5ef83d8a",
    "osx-arm64": "ec2a072f028e1a7cf20f3e2e74d5a8127cf5a5f27636375b5359811565f4e5be",
}


def _notice(message: str) -> None:
    print("[Potbot database] " + message, file=sys.stderr, flush=True)


def _platform() -> str:
    name = "win" if sys.platform == "win32" else "osx" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else ""
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"aarch64", "arm64"} else "64" if machine in {"amd64", "x86_64"} else ""
    if name == "osx" and arch == "aarch64":
        arch = "arm64"
    result = f"{name}-{arch}"
    if result not in MAMBA_RELEASES:
        raise CapabilityUnavailableError("Native PostgreSQL supports Windows x64, Linux x64/ARM64 and macOS Intel/Apple Silicon")
    return result


def _private(path: Path) -> None:
    # Never follow a preexisting symlink that could redirect secrets or data.
    if path.is_symlink():
        raise InvalidInputError("Native PostgreSQL paths cannot be symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    else:
        # A Windows user-owned directory may inherit permissive checkout ACLs.
        # Make its inherited ACL private before writing the cluster or credentials.
        import csv
        import io

        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, timeout=10, check=False)
        try:
            records = list(csv.reader(io.StringIO(result.stdout)))
            sid = records[0][1].strip() if result.returncode == 0 and len(records) == 1 else ""
        except (IndexError, csv.Error):
            sid = ""
        if not sid.startswith("S-1-") or any(char not in "S-0123456789" for char in sid):
            raise CapabilityUnavailableError("Cannot verify the current Windows account for a private PostgreSQL directory")
        locked = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                                capture_output=True, timeout=15, check=False)
        if locked.returncode != 0:
            raise CapabilityUnavailableError("Cannot secure the native PostgreSQL directory ACL")


def _paths(root: Path | None = None) -> tuple[Path, Path, Path, Path]:
    project = project_root() if root is None else root
    parent = project / ".potbot" / "native-postgres"
    return parent, parent / "packages", parent / "data", parent / "managed.json"


def is_managed(root: Path | None = None) -> bool:
    return _paths(root)[3].is_file()


def _manifest(root: Path | None = None) -> dict[str, object]:
    parent, prefix, data, marker = _paths(root)
    if not marker.is_file() or marker.is_symlink() or parent.is_symlink() or data.is_symlink() or prefix.is_symlink():
        raise InvalidInputError("The managed native database marker is missing or unsafe")
    try:
        if marker.stat().st_size > 512:
            raise ValueError
        value = json.loads(marker.read_text("utf-8"))
    except (ValueError, OSError, UnicodeError):
        raise InvalidInputError("The native database marker is invalid; refusing to adopt an unknown cluster") from None
    expected_major = 16 if sys.platform == "win32" else 18
    if value != {"schema": VERSION, "port": PORT, "platform": _platform(), "postgres_major": expected_major}:
        raise InvalidInputError("The native database installation differs from its saved version and host")
    version = data / "PG_VERSION"
    if version.is_symlink() or not version.is_file():
        raise InvalidInputError("The managed PostgreSQL data directory is incomplete")
    try:
        if version.stat().st_size > 16 or version.read_text("ascii").strip() != str(expected_major):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        raise InvalidInputError("The managed PostgreSQL major version is unexpected") from None
    return value


def _tools(prefix: Path) -> tuple[Path, Path, Path]:
    bin_dir = prefix / "Library" / "bin" if os.name == "nt" else prefix / "bin"
    suffix = ".exe" if os.name == "nt" else ""
    initdb, pg_ctl, pg_config = (bin_dir / (stem + suffix) for stem in ("initdb", "pg_ctl", "pg_config"))
    if not all(p.is_file() and not p.is_symlink() for p in (initdb, pg_ctl, pg_config)):
        raise CapabilityUnavailableError("The native PostgreSQL package is missing server binaries")
    return initdb, pg_ctl, pg_config


def _package_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MAMBA_ROOT_PREFIX"] = str(root / ".potbot" / "native-postgres" / "mamba-root")
    env["MAMBA_NO_BANNER"] = "1"
    # PostgreSQL on Windows loads DLLs from the conda package environment.
    # The copied micromamba executable is pinned separately; don't globally
    # modify PATH or leak package DLLs into unrelated Potbot workers.
    prefix = _paths(root)[1]
    native_bins = (
        [prefix / "Library" / "bin", prefix / "Scripts", prefix / "bin"]
        if os.name == "nt" else [prefix / "bin"]
    )
    env["PATH"] = os.pathsep.join([*(str(p) for p in native_bins), env.get("PATH", "")])
    # Host PostgreSQL and conda variables must not redirect the local installation.
    for key in list(env):
        if key.startswith(("PG", "CONDA_", "MAMBA_")) and key not in {"MAMBA_ROOT_PREFIX", "MAMBA_NO_BANNER"}:
            env.pop(key)
    return env


def _mamba(root: Path) -> Path:
    release = _platform()
    parent, _, _, _ = _paths(root)
    bundle = parent / "mamba-root" / ("micromamba.exe" if os.name == "nt" else "micromamba")
    _private(bundle.parent)
    digest = MAMBA_RELEASES[release]
    if bundle.is_file() and not bundle.is_symlink():
        with bundle.open("rb") as reader:
            if hashlib.file_digest(reader, "sha256").hexdigest() == digest:
                return bundle
    _notice("Downloading the pinned native PostgreSQL package manager")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=bundle.parent, prefix="download-", delete=False) as writer:
            temporary = Path(writer.name)
            sha = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(MAMBA_RELEASE + "micromamba-" + release, timeout=30) as response:
                if response.url.split(":", 1)[0] != "https":
                    raise CapabilityUnavailableError("Package download left HTTPS")
                while block := response.read(1 << 20):
                    size += len(block)
                    if size > 45 * 1024 * 1024:
                        raise CapabilityUnavailableError("Package manager download exceeded its size bound")
                    sha.update(block)
                    writer.write(block)
            if sha.hexdigest() != digest:
                raise CapabilityUnavailableError("Package manager digest differs from the pinned official release")
        if os.name != "nt":
            temporary.chmod(0o700)
        os.replace(temporary, bundle)
        return bundle
    except (OSError, TimeoutError, ValueError):
        raise CapabilityUnavailableError("Cannot download the pinned native PostgreSQL package manager") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ensure_packages(root: Path) -> Path:
    parent, prefix, data, _ = _paths(root)
    if prefix.is_symlink():
        raise InvalidInputError("Refusing a redirected database package directory")
    if prefix.exists():
        # Only reuse a fully installed package set. Never delete a preexisting
        # directory merely because it does not match the expected profile.
        _tools(prefix)
        return prefix
    mamba = _mamba(root)
    _private(parent)
    # Windows pgvector 0.8.6 requires libpq 16.15; the other supported
    # conda-forge builds are compiled against PostgreSQL/libpq 18.6.
    version = "16.15" if sys.platform == "win32" else "18.6"
    spec = ["postgresql=" + version, "libpq=" + version, "pgvector=0.8.6"]
    _notice("Installing native PostgreSQL " + version + " with pgvector (no Docker)")
    try:
        result = subprocess.run([
            str(mamba), "create", "--yes", "--override-channels", "-c", "conda-forge",
            "--strict-channel-priority", "--prefix", str(prefix), *spec,
        ], env=_package_environment(root), cwd=root, timeout=900, check=False)
        if result.returncode != 0:
            raise CapabilityUnavailableError("Native PostgreSQL/pgvector package installation failed")
        _tools(prefix)
        config = (prefix / "Library" / "bin" if os.name == "nt" else prefix / "bin") / (
            "pg_config.exe" if os.name == "nt" else "pg_config"
        )
        sharedir = subprocess.run([str(config), "--sharedir"], capture_output=True,
                                text=True, timeout=20, env=_package_environment(root), check=False)
        if sharedir.returncode or not (Path(sharedir.stdout.strip()) / "extension" / "vector.control").is_file():
            raise CapabilityUnavailableError("The installed native server lacks pgvector extension files")
        return prefix
    except subprocess.TimeoutExpired:
        raise CapabilityUnavailableError("Native PostgreSQL package installation timed out") from None
    except OSError:
        raise CapabilityUnavailableError("Native PostgreSQL package install could not launch") from None
    # A failed first-time package install is retried by the operator after a
    # diagnostic. The cluster's data are always separate and never erased.


def _db_url(password: str, database: str) -> str:
    return "postgresql://potbot_admin:" + quote(password, safe="") + f"@127.0.0.1:{PORT}/{database}?sslmode=disable"


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def _command(command: list[str], root: Path, *, label: str) -> None:
    try:
        result = subprocess.run(command, cwd=root, env=_package_environment(root),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CapabilityUnavailableError("Native PostgreSQL " + label + " could not complete") from None
    if result.returncode:
        raise CapabilityUnavailableError("Native PostgreSQL " + label + " failed; inspect its local logfile")


def _start_cluster(root: Path, password: str) -> None:
    parent, prefix, data, _marker = _paths(root)
    _initdb, pg_ctl, _config = _tools(prefix)
    try:
        status = subprocess.run([str(pg_ctl), "status", "-D", str(data)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=_package_environment(root), timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CapabilityUnavailableError("Cannot inspect the managed PostgreSQL process") from None
    if status.returncode == 0:
        validate_connection(_db_url(password, "postgres"))
        return
    if _port_in_use():
        raise CapabilityUnavailableError("Native PostgreSQL port 55436 belongs to another process; refusing to adopt it")
    _notice("Starting the private native PostgreSQL server")
    _command([str(pg_ctl), "start", "-w", "-t", "60", "-D", str(data),
              "-l", str(parent / "postgres.log"), "-o", "-c listen_addresses=127.0.0.1 -c port=55436"],
             root, label="startup")
    validate_connection(_db_url(password, "postgres"))


def _create_database(password: str) -> None:
    import psycopg

    # No SQL input is taken from models or users. CREATE DATABASE uses autocommit.
    try:
        with psycopg.connect(_db_url(password, "postgres"), autocommit=True, connect_timeout=5) as connection:
            exists = connection.execute("SELECT 1 FROM pg_database WHERE datname='potbot'").fetchone()
            if exists is None:
                connection.execute("CREATE DATABASE potbot")
        with psycopg.connect(_db_url(password, "potbot"), connect_timeout=5) as connection:
            available = connection.execute("SELECT 1 FROM pg_available_extensions WHERE name='vector'").fetchone()
            if available is None:
                raise CapabilityUnavailableError("Native PostgreSQL is missing the pgvector extension")
    except psycopg.Error:
        raise CapabilityUnavailableError("Native PostgreSQL database creation or inspection failed") from None


def _create_cluster(root: Path) -> None:
    parent, prefix, data, marker = _paths(root)
    if data.exists() or marker.exists():
        raise InvalidInputError("A native PostgreSQL cluster already exists without a valid marker; refusing to overwrite it")
    _private(parent)
    initdb, _ctl, _config = _tools(prefix)
    store = CredentialStore(credential_profile())
    if store.get("postgres.admin") is not None:
        raise InvalidInputError("A database credential already exists; refusing to overwrite the external database")
    password = secrets.token_urlsafe(42)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=parent, prefix="cluster-password-", delete=False) as writer:
            tmp = Path(writer.name)
            writer.write((password + "\n").encode("utf-8"))
            writer.flush()
        if os.name != "nt":
            tmp.chmod(0o600)
        _notice("Initializing private PostgreSQL data and password authentication")
        _command([str(initdb), "-D", str(data), "-U", "potbot_admin", "--pwfile", str(tmp),
                  "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--encoding=UTF8"],
                 root, label="initialisation")
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    if not (data / "PG_VERSION").is_file():
        raise CapabilityUnavailableError("Native database initialisation did not create a PostgreSQL cluster")
    with (data / "postgresql.conf").open("a", encoding="utf-8") as writer:
        writer.write("\n# Potbot private loopback connection\nlisten_addresses = '127.0.0.1'\nport = 55436\n")
    # Commit the credential before starting. If a host crash occurs after this,
    # the next run can reattach using this marker without losing the password.
    store.set("postgres.admin", _db_url(password, "potbot"))
    manifest = {"schema": VERSION, "port": PORT, "platform": _platform(),
                "postgres_major": 16 if sys.platform == "win32" else 18}
    tmp_marker = parent / "managed.pending"
    tmp_marker.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    if os.name != "nt":
        tmp_marker.chmod(0o600)
    os.replace(tmp_marker, marker)
    _start_cluster(root, password)
    _create_database(password)
    _notice("Native PostgreSQL and pgvector are ready; data persists across restarts")


def _password_from_vault() -> str:
    from urllib.parse import urlsplit, unquote
    dsn = CredentialStore(credential_profile()).get("postgres.admin")
    if dsn is None:
        raise CapabilityUnavailableError("Managed PostgreSQL password is missing from the native vault")
    parsed = urlsplit(dsn)
    if (parsed.scheme != "postgresql" or parsed.hostname != "127.0.0.1" or
        parsed.port != PORT or parsed.username != "potbot_admin" or parsed.path != "/potbot"):
        raise InvalidInputError("Managed PostgreSQL credential differs from its saved identity")
    if not parsed.password:
        raise InvalidInputError("Managed PostgreSQL credential has no password")
    return unquote(parsed.password)


def ensure_running(root: Path | None = None) -> None:
    """Start only the cluster created by Potbot; do not adopt a different server."""
    root = project_root() if root is None else root
    _manifest(root)
    secret = _password_from_vault()
    _start_cluster(root, secret)
    _create_database(secret)


def provision_if_needed(root: Path | None = None) -> bool:
    """Auto-provision when there is no external admin DSN and no managed cluster."""
    root = project_root() if root is None else root
    store = CredentialStore(credential_profile())
    if is_managed(root):
        ensure_running(root)
        return True
    if os.environ.get("POTBOT_ADMIN_DSN") or store.get("postgres.admin"):
        return False
    _ensure_packages(root)
    _create_cluster(root)
    return True


def stop_if_managed(root: Path | None = None) -> None:
    if not is_managed(root):
        return
    root = project_root() if root is None else root
    _manifest(root)
    _initdb, pg_ctl, _config = _tools(_paths(root)[1])
    status = subprocess.run([str(pg_ctl), "status", "-D", str(_paths(root)[2])],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=_package_environment(root), timeout=15, check=False)
    if status.returncode == 0:
        _command([str(pg_ctl), "stop", "-w", "-t", "30", "-m", "fast", "-D", str(_paths(root)[2])],
                 root, label="shutdown")
