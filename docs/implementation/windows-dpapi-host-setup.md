# Windows managed WSL host custody cleanup

The managed `Potbot-SynOperator` WSL distribution is a Linux execution host,
not the Windows Operator keystore. Windows Operator authority is retained by
Syn's `windows-dpapi-host` broker and only its bounded named-pipe bridge is
forwarded into WSL.

The Windows host bootstrap therefore installs Python, D-Bus user-session
support and build prerequisites, but it does not install, start or unlock
GNOME Keyring or `libsecret-tools`. D-Bus remains available because the WSL
user service manager and other Linux components can require a user session;
its presence does not select Secret Service as Operator custody.

Older Potbot host receipts can contain `custody: linux-secret-service` and the
legacy `native-keystore` setup checkpoint. A ready revision-3 receipt is
migrated in place to `windows-dpapi-host` without starting a Linux credential
daemon or replacing any Operator identity. The old checkpoint name remains
accepted only so interrupted installations can resume safely.

The custom WSL-kernel activation path also must not start a credential daemon
when it reboots the owned distribution. Its next ordinary WSL command is
sufficient to boot the distribution again.

Native Linux remains unchanged: it continues to use Secret Service and may
install GNOME Keyring explicitly. macOS continues to use Keychain.

## Acceptance

- Fresh Windows WSL provisioning contains no GNOME Keyring dependency.
- An existing ready Windows receipt migrates custody without invoking WSL.
- Kernel activation never calls a Linux keystore bootstrap.
- Windows Syn commands still fail closed when the DPAPI bridge is unavailable.
