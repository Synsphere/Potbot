# Potbot one-command startup (Windows, Linux and macOS)

Run `start.bat`, `./start.ps1`, or `bash ./start.sh` **without arguments**. The
launchers still bootstrap the exact Python/uv runtime, but now run the complete
application, rather than printing command help.

On the first launch, Potbot:

1. Reuses an admin PostgreSQL URL stored in the native OS keyring. Where none
   exists, a machine with an interactive, running Docker daemon can opt into a
   persistent, **loopback-only** local `pgvector/pgvector:pg17` database; otherwise
   the normal hidden PostgreSQL admin URL prompt is used. An existing container
   with the reserved name is never destroyed or adopted without its own key.
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

**Security and system boundaries:** Potbot cannot silently assume administrator
rights, access an absent Docker daemon, or start a database on an already-used
port. If neither a configured PostgreSQL admin connection nor an available local
Docker daemon exists, first run asks for the admin connection and reports a
specific failure if none can be supplied. The opt-in local database container
uses a persistent named volume and loopback binding; Docker administrators can
inspect its environment. It is a developer convenience, **not** qualified
production PostgreSQL. Installed native Syn/Cog operations still require their
independent released host/WSL/isolation qualification; startup does not bypass
those gates. The service does not silently grant paid inference access.
