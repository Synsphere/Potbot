"""Windows WSL host setup must never fall back to Linux Secret Service custody."""

from __future__ import annotations

from pathlib import Path

from potbot import windows_core_runtime
from potbot.syn_release import host


def test_windows_wsl_prerequisites_do_not_install_linux_secret_service() -> None:
    assert "gnome-keyring" not in host.PROVISION_UBUNTU
    assert "libsecret-tools" not in host.PROVISION_UBUNTU
    assert "dbus-user-session" in host.PROVISION_UBUNTU


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
        return ""

    monkeypatch.setattr(host, "run_host", run)
    ready = manager.setup()

    assert ready["custody"] == "windows-dpapi-host"
    assert ready["prerequisite_revision"] == 3
    assert calls == []


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
