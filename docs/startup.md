# Startup, dependency downloads and credentials

**Default launch now starts the complete application** (backend, worker and
Next.js GUI) and opens <http://127.0.0.1:3100>. See
[one-command startup](implementation/one-click-startup.md) for first-run setup,
secure credentials, GUI installation and service controls.

Potbot 0.4 adds a common startup flow for Windows BAT, Windows PowerShell and Bash.
The entry scripts live at the repository root. They work from another current directory
and from a checkout path containing spaces. Arguments, stdin and exit codes pass
through to the same Python entry point, `potbot.startup`.

## Starting the application

| Platform | Entry point |
| --- | --- |
| Windows Command Prompt | `start.bat` |
| Windows PowerShell 5.1 or PowerShell 7 | `.\start.ps1` |
| Linux or macOS with Bash | `bash ./start.sh` |

Each script reports four stages: find or install uv 0.12.23; obtain managed Python
3.12; synchronize `uv.lock` and every Python runtime extra; start Potbot. uv's own
download/install progress remains visible. The scripts use the project `.venv`,
with bootstrap binaries and caches under `.potbot/`. They do not change the user's
global PATH. `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` can select preexisting caches.

The runtime installation is equivalent to:

```bash
uv python install 3.12 --no-bin --no-registry
uv sync --locked --all-extras --no-dev --python 3.12 --managed-python
uv run --no-sync python -m potbot.startup
```

The actual scripts also pass the project path explicitly. The Linux/Windows lock
selects CPU PyTorch. An ordinary first startup installs Python libraries, including
the embedding runtime, but does not download model weights. The scripted Phase 4
patch workflow needs no model weights. Developers can add tools separately:

```bash
uv sync --locked --extra postgres --extra semantics --extra credentials --dev
```

`uv sync --locked` fails if the project metadata and lock disagree. Fix or update the
lock deliberately; the launcher does not silently resolve a new dependency set.
The scripts propagate failed installation and application exit codes.

Startup reports readiness only after the authenticated backend, GUI and Xpra
bridge are ready. Xpra status must belong to the live supervisor and current
workspace. A terminal custody error or exhausted Xpra restart budget ends the
startup wait with a diagnostic. During WSL preparation, the launcher also checks
that the system Python interpreter can import the staged Secret Service package
and construct its D-Bus interfaces as the service account.

## Windows firewall and instance control

`start.bat` and `start.ps1` check the Windows Firewall when the managed custody
authority is listening. The check uses the live listener, the custody virtualenv
and base Python executable, and the current WSL host and guest IPv4 addresses.
Custody continues to use its authenticated TCP connection to the Windows DPAPI
authority.

If the WSL TCP probe succeeds, startup does not request administrator access.
When a repair is needed, only a separate firewall helper requests UAC elevation.
The desktop, service and keystore authority keep the original user's identity.
The helper rechecks the live endpoint and effective firewall policy after UAC
approval. It can disable ordinary local inbound TCP application block rules
matching the actual listener executable, and creates or updates a Potbot rule
allowing only that executable from the current WSL guest to its Windows host
address. The rule permits TCP ports for that executable/address pair because
custody selects a fresh port each launch; a port change alone needs no new UAC
prompt. An interpreter or WSL address change can require another repair.

Custom, generic or centrally managed block rules are reported for administrator
review. Firewall profiles and the default inbound policy are not disabled.
Declining UAC reports a specific failure and does not produce a prompt loop.
After a repair, startup checks WSL TCP connectivity and the existing authenticated
custody health response before admitting the WSL runtime.

Stop from either Command Prompt or PowerShell, including when startup is incomplete:

```powershell
.\stop.bat
# Equivalent:
.\stop.ps1
```

`start.bat stop` uses the same stop script. Stopping uses the installed
`.venv\Scripts\python.exe`; it does not download Python, run `uv sync`, or build
the frontend. The optional `stop.ps1 -TimeoutSeconds 90` controls how long it
waits for the launcher. An in-flight dependency installation is allowed to finish
its current command, then cancelled before the application starts.

Ctrl+C in the native desktop's launch terminal requests the same complete stop
after the desktop releases its instance lock. Repeated Ctrl+C does not interrupt
that cleanup.

Shutdown signals the startup worker, desktop and service before waiting for them
to release their OS locks. It also handles a verified older scheduled task and
its process tree, closes the owned `Potbot-SynOperator` WSL distribution, and
stops Potbot's managed PostgreSQL through `pg_ctl`. It does not unregister WSL,
delete PostgreSQL data, or change stored credentials. Other WSL distributions
and unidentified processes are not stopped. If a live owner or listener remains,
the command fails with a diagnostic instead of claiming that shutdown completed.

Windows launch, desktop and service ownership is per user, across checkouts.
Run stop from the checkout that owns the instance. Duplicate starts are refused
before a second supervisor can run, including direct `service run` invocations.
Kernel locks are released after a crash, so stale PID files cannot block startup
or cause another process to be killed. A Windows process job contains supervised
descendants; a separate empty lifetime pipe tells the WSL core to clean up when
its Windows owner disappears. That pipe carries no keystore requests or secrets.

For an upgrade while an old version is running, apply the patch, run `stop.bat`,
and start again only after stop succeeds. If an unrelated application occupies
a required port, stop identifies the port and leaves that application alone.

## PostgreSQL on first startup

The default launcher now configures the application rather than printing CLI help.
It uses a stored PostgreSQL admin connection or automatically installs a
native, user-owned PostgreSQL/pgvector server (no Docker). The admin
connection applies migrations and creates a restricted workspace login; its
privileges are never used by running jobs. Once configured, subsequent startups
reuse the retained workspace and native-vault secrets. See the guide above.

Explicit database commands continue to resolve the application connection in
this order: `POTBOT_DSN` if set, stored `postgres.application`, then a hidden
interactive URL prompt. The administrative connection is separate.

A newly entered value is syntax checked, tested with a bounded read-only database
connection, and saved only after validation succeeds. Storage is read back to confirm
persistence. The URL and password are never printed in normal status or failure
messages. An unsuccessful connection is not saved. Environment overrides are used
for that process and are not automatically copied into the vault.

The administrative connection is independent. `potbot admin migrate` resolves
`POTBOT_ADMIN_DSN`, then `postgres.admin` in the vault, then its own hidden prompt.
An application URL is never substituted for a missing administrative URL.

| Platform | Explicitly selected secure backend |
| --- | --- |
| Windows | Windows Credential Manager through keyring's `WinVaultKeyring` |
| macOS | Keychain through keyring's macOS backend |
| Linux desktop | Secret Service through keyring's SecretService backend |

`POTBOT_CREDENTIAL_PROFILE` selects a bounded profile name; the default is `default`.
Profiles have separate application/admin accounts. The backend is selected explicitly,
so a third-party keyring configuration cannot silently switch persistence to a
plaintext file or a null backend.

Manage a saved URL without placing it in shell history:

```bash
bash ./start.sh credentials status
bash ./start.sh credentials set --purpose application
bash ./start.sh credentials set --purpose admin
bash ./start.sh credentials --profile staging status
bash ./start.sh credentials delete --purpose application
```

`status` reports whether each account exists; it does not return values. `set` uses
the hidden prompt and connection check. `delete` removes only the selected account.
The corresponding Windows commands use `start.bat` or `.\start.ps1` with the same
arguments.

On a headless machine without an unlocked native vault, inject `POTBOT_DSN` and,
when needed, `POTBOT_ADMIN_DSN` through the host's secret-management facility. Missing
configuration in a noninteractive process is an explicit error. Potbot never writes
a URL to a repository file as a fallback. Help, version, `info`, offline catalogs,
and `dependencies` commands do not prompt for database credentials.

## Integrated model downloader

The initial download profile is the already qualified standard embedding model:
`Qwen/Qwen3-Embedding-0.6B`, revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`. Six pinned assets total **1,207,469,240
bytes**. Python packages, temporary partial files and runtime memory require additional
space.

```bash
bash ./start.sh dependencies status
bash ./start.sh dependencies download
bash ./start.sh dependencies download --model-dir /data/models/qwen3-embedding-0.6b
```

The default model path is `models/qwen3-embedding-0.6b` beneath the project selected
by the launcher. Explicit `--model-dir` is supported. JSON results go to stdout;
human progress goes to stderr and includes file names, completed bytes and percentage.

The downloader uses bounded HTTPS reads, a fixed revision, exact expected byte counts
and embedded SHA-256 digests. It serializes concurrent downloads, writes into an
owned partial directory, resumes using HTTP ranges where the server supports them,
and restarts a file when a server ignores or invalidates the range. Corrupt or wrong-sized
files never become a ready installation. The final manifest is published after every
asset is verified. Completed files and existing valid installations are reused offline.

CLI `embedding setup` uses the same downloader. CLI encoding operations (`embedding
smoke`, indexing/search and vector-backed SYN search) ensure the standard model is
present after parsing arguments and before starting the isolated worker. The encoder
itself loads local files with remote code disabled; direct Python encoder calls remain
explicit and do not trigger hidden network activity.

The five reasoning/coding catalog defaults are not downloaded automatically. Their
live deployment adapters and qualification belong to Phase 5. No custom embedding
model is introduced here.

## Host services and platform qualification

uv installs Python and Python packages. It cannot provision an operating-system
credential service, PostgreSQL server, Git, or the Linux sandbox primitives. Potbot
uses an existing PostgreSQL service with pgvector. Registered repository operations
also need Git. Qualified execution and supervised embeddings currently require Linux
Bubblewrap, libseccomp, `prlimit`, and permitted private namespaces:

```bash
sudo apt-get install bubblewrap libseccomp2 util-linux git
```

`dependencies status` reports package/model state and the host tools it can find.
Windows/macOS launchers support installation, credential handling and management
commands; the repository execution backend remains Linux-only. Use a suitable Linux
host or WSL environment for the controlled patch workflow. A WSL vault is a Linux
credential service, separate from the Windows Credential Manager.

The release tests exercise a real clean Bash startup, local HTTP download failure
fixtures, a live pinned HTTPS file, offline validation of the complete Qwen assets,
and real PostgreSQL first-use validation with an injected test vault. Windows-native
BAT/PowerShell argument and exit-code tests are included in CI. Native Windows/macOS
vaults and the Windows CI job were not executed in the Linux development environment.

## Implementation boundaries

`credentials.py` owns the OS-vault adapter, credential selection, hidden prompts and
sanitized connectivity probe. `startup.py` owns first-run orchestration and forwards
explicit commands. `dependencies.py` owns download manifests, partial transfer state,
locking, progress and verification. Database CLI handlers request credentials only
after argument parsing. Credential values are control-plane inputs; they are not
added to model packets, candidate snapshots or tool definitions by this integration.

This installation flow manages Potbot's own dependencies. Future repository-specific
dependency discovery and installation must remain part of the task's declared tool,
environment, network and budget policies.


## Diagnose a rejected PostgreSQL connection

A failed first connection is not saved. The public error now includes a fixed
diagnostic label while keeping the connection URL, password, host, database and
raw driver text private. Re-enter the corrected URL through the hidden prompt.

| Diagnostic | Check |
| --- | --- |
| host_resolution_failed | The hostname and this machine's DNS/network route. |
| connection_refused | The port, server listener and permitted connection route. |
| connection_timed_out | Network reachability, firewall and server availability. |
| tls_connection_failed | Required TLS settings, trusted CA and server certificate. Do not disable verification to silence the error. |
| authentication_or_access_rejected | The intended login, password and server access rules. |
| database_not_found | The database portion of the URL. |
| unclassified_connection_failure | Server-side diagnostics through an authorised administrator; keep secrets out of shared reports. |

These are diagnostic categories, not a claim that the server is healthy or the
login has setup privileges. A TLS label may cover negotiation or certificate
validation. A successful connection check remains read-only; provisioning is a
separate operation. The existing hidden-input and verified-native-vault rules
are unchanged. CLI status receipts are the relevant evidence; an extra screenshot
or video adds no diagnostic detail to this text-only error.

For an operator-selected local model, see [offline registered generation](local-generation.md).
Its process diagram shows the boundary between private connection setup, actual
generation and independent verification.
