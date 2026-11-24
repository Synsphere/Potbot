"""Windows WSL host setup must never fall back to Linux Secret Service custody."""

from __future__ import annotations

from pathlib import Path

from potbot import windows_core_runtime
from potbot.syn_release import host


def test_windows_wsl_prerequisites_remove_linux_secret_service() -> None:
    assert "dbus-user-session" in host.PROVISION_UBUNTU
    assert (
        "for package in gnome-keyring libpam-gnome-keyring libsecret-tools" in host.PROVISION_UBUNTU
    )
    assert "apt-get purge --yes $legacy_packages" in host.PROVISION_UBUNTU
    assert (
        "systemctl --global mask gnome-keyring-daemon.service gnome-keyring-daemon.socket"
        in host.PROVISION_UBUNTU
    )
    assert "pkill -u synoperator -f" in host.PROVISION_UBUNTU
    assert "rm -f /etc/xdg/autostart/gnome-keyring-*.desktop" in host.PROVISION_UBUNTU
    assert "test ! -x /usr/bin/gnome-keyring-daemon" in host.PROVISION_UBUNTU
    assert ".local/share/keyrings" not in host.PROVISION_UBUNTU
    assert "[boot]\nsystemd=true" in host.PROVISION_UBUNTU
    assert "[automount]\nenabled=false\nmountFsTab=false" in host.PROVISION_UBUNTU
    assert "[interop]\nenabled=false\nappendWindowsPath=false" in host.PROVISION_UBUNTU
    assert "[user]\ndefault=synoperator" in host.PROVISION_UBUNTU
    assert "groupadd --gid 69 potbot-workspace" in host.PROVISION_UBUNTU
    assert 'usermod --gid "$workspace_group" synoperator' in host.PROVISION_UBUNTU
    assert 'test "$(id -g synoperator)" = 69' in host.PROVISION_UBUNTU
    assert host.PREREQUISITE_REVISION == 6


def test_legacy_ready_receipt_migrates_to_dpapi_without_starting_keyring(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    manager = host.WslOperatorHost(tmp_path / "managed")
    manager.root.mkdir(parents=True)
    registration = host.WslRegistration("a" * 32, manager.location, 2)
    monkeypatch.setattr(host, "_registered_distro", lambda: registration)
    manager._write_receipt(
        {
            "profile": "potbot.syn-wsl-host/1",
            "installation_id": "b" * 32,
            "distro": host.DISTRO,
            "distribution": host.UBUNTU,
            "user": host.OPERATOR_USER,
            "state": "ready",
            "location": str(manager.location),
            "registration_id": registration.identifier,
            "prerequisite_revision": 3,
            "custody": "linux-secret-service",
            "keystore_unlocked": False,
            "operator_identity_created": False,
        }
    )
    calls: list[tuple[list[str], int]] = []

    def run(argv: list[str], *, timeout: int = 30) -> str:
        calls.append((argv, timeout))
        return (
            "POTBOT_WSL_CONFIG_CHANGED=0\nsyn-native-prerequisites-ready\n"
            if argv[-1] == host.PROVISION_UBUNTU
            else ""
        )

    monkeypatch.setattr(host, "run_host", run)
    ready = manager.setup()

    assert ready["custody"] == "windows-dpapi-host"
    assert ready["prerequisite_revision"] == host.PREREQUISITE_REVISION
    assert len(calls) == 1
    assert calls[0][0][-1] == host.PROVISION_UBUNTU


def test_kernel_activation_does_not_start_linux_keyring(monkeypatch, tmp_path: Path) -> None:
    from potbot import wsl_kernel, wsl_kernel_activation

    class Manager:
        root = tmp_path
        wsl = "wsl.exe"

        @staticmethod
        def command(executable: str, arguments: list[str]) -> list[str]:
            return ["wsl.exe", executable, *arguments]

    calls: list[list[str]] = []

    def run(argv: list[str], *, timeout: int = 30) -> str:
        calls.append(argv)
        return "stock-wsl-kernel\n" if len(calls) == 1 else ""

    def download(path: Path, **_kwargs: object) -> None:
        path.write_bytes(b"verified-fixture")

    class Activation:
        def __init__(self, _manager: object) -> None:
            pass

        def activate(self, *_args: object, **_kwargs: object) -> dict[str, str]:
            return {
                "activation": "boot_verified",
                "kernel_release": windows_core_runtime.WSL_KERNEL_RELEASE,
            }

    monkeypatch.setattr(host, "run_host", run)
    monkeypatch.setattr(wsl_kernel, "download_bundle", download)
    monkeypatch.setattr(wsl_kernel_activation, "WslKernelActivation", Activation)

    windows_core_runtime._ensure_required_wsl_kernel(Manager())

    assert len(calls) == 2
    assert calls[1][-2:] == ["--terminate", host.DISTRO]
