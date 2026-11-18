# Potbot one-command startup (Windows, Linux and macOS)

Run `start.bat`, `./start.ps1`, or `bash ./start.sh` **without arguments**. The
launchers still bootstrap the exact Python/uv runtime, but now run the complete
application, rather than printing command help.

On the first launch, Potbot:

1. Checks for an operator-configured PostgreSQL administrator connection. If no
   external connection is configured, installs a **native** PostgreSQL and pgvector
   pair from conda-forge using a SHA-256 verified official micromamba executable.
   No Docker daemon, administrator elevation, global PostgreSQL installation or
   Visual C++ compiler is needed. The database binds only to `127.0.0.1:55436`,
   runs as the current user and stores persistent data in
   `.potbot/native-postgres/data`. Windows uses PostgreSQL 16.15 and pgvector 0.8.6;
   supported Linux/macOS builds use PostgreSQL 18.6 and pgvector 0.8.6. The
   installer retains a private cluster marker and refuses to replace/adopt
   partial or unknown database files or a competing port listener.
2. Applies versioned PostgreSQL migrations using **admin** credentials, separately
   from the worker's restricted application login. If no application login exists,
   it creates an unprivileged login and a new private workspace, stores the tested
   URL in Windows Credential Manager / macOS Keychain / Linux Secret Service, and
   retains the workspace and principal IDs in the existing service configuration.
   When exactly one existing scope belongs to the application login, it is reused.
3. Creates a default provider policy with *paid model disclosure disabled*. No
   hosted provider is enabled and no reasoning model is downloaded merely by
   opening the UI. Existing installed policies and qualified native-tool profiles
   are never overwritten.
4. Creates API, setup, GUI password and cookie-signing secrets in the OS credential
   store if absent. A newly generated GUI password is shown **once**, only on an
   interactive terminal. No secrets are placed in `.env.local`, source, arguments
   or web responses. The frontend receives them through its supervised process
   environment (visible to the local user/system process administrator).
5. Uses a supported installed Node.js/npm or downloads pinned Node.js 22.16.0 from
   the official HTTPS endpoint, verifies the release file against its HTTPS
   SHA-256 list, runs `npm ci`, builds the Next.js production UI and caches the
   source fingerprint to avoid rebuilding unchanged code.
6. Registers the existing **user-owned** service manager (Windows per-user Task
   Scheduler, launchd, or systemd user service) to supervise the API, model/Cog
   worker and Next.js UI. It opens <http://127.0.0.1:3100> once both the backend
   and frontend respond successfully. If the user service manager is unavailable,
   a foreground process runs the same supervisor until interrupted.

After the first launch, the same bare command reuses all configuration and
opens an already healthy GUI without interrupting it. An explicit `restart`
rebuilds changed UI source before bringing the services up again. Explicit advanced
commands still work unchanged, e.g. `start.bat service status`,
`start.bat admin migrate`, or `start.bat dependencies status`.

Convenient commands: `start.bat start`, `start.bat stop`, `start.bat restart`,
`start.bat status`, `start.bat logs`, `start.bat ui-password` (interactive terminal only) (and equivalent PowerShell/Bash launchers).
A foreground fallback is stopped with Ctrl-C in its original terminal.

**Security and system boundaries:** An existing configured PostgreSQL
connection takes precedence; Potbot never overwrites an operator-managed server.
The packaged native server is an actual OS PostgreSQL process, not Docker and
not an elevated Windows service. It starts automatically when the Potbot user
service starts, and the `stop` command stops the owned local cluster after the
API and workers. The cluster is private to the current user and uses SCRAM
password authentication. Its database identity, data and credentials are retained
across restarts. A port collision, corrupt cluster, missing vault credential or
partial installation fails closed instead of deleting existing data. Reinstalling
application code must not delete `.potbot/native-postgres/data`; back up this
folder with ordinary database-aware backup procedures.

The conda-forge package combination and installer digests are recorded in
`potbot.native_postgres`. The package-manager executable is SHA-256 verified
against an official versioned release; conda-forge package artifacts use their
repository metadata checksums. Installed native Syn/Cog operations still need
independent host/WSL/isolation qualification and startup never grants paid
inference access. Native installer and supervised process acceptance must still
be exercised on Windows, Linux and macOS before describing them as qualified.
