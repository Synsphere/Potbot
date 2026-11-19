from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot.domain.errors import StateMismatchError
from potbot.syn_release import system_service, windows_custody, windows_service
from potbot.syn_release.commands import SynPublicCommands


def custody_policy() -> dict[str, object]:
    return {
        "repository": "Synsphere/syn",
        "version": "0.11.0",
        "minimum_version": "0.11.0",
        "source_commit": windows_custody.SYN_HOST_CUSTODY_SOURCE_COMMIT,
        "manifest_sha256": windows_custody.SYN_HOST_CUSTODY_MANIFEST_SHA256,
        "public_key": windows_custody.SYN_RELEASE_PUBLIC_KEY,
        "release_key_sha256": windows_custody.SYN_RELEASE_KEY_SHA256,
        "mode": "operator",
    }


def test_windows_custody_pins_exact_signed_syn_0110_release() -> None:
    assert windows_custody.SYN_HOST_CUSTODY_VERSION == "0.11.0"
    assert (
        windows_custody.SYN_HOST_CUSTODY_SOURCE_COMMIT
        == "3ed8a43c7e08480b4e5ce13c9e4720c3001b70dc"
    )
    assert (
        windows_custody.SYN_HOST_CUSTODY_MANIFEST_SHA256
        == "bb496a73ebd9d37018f6274a6dc29e4e20f03b96b9ab4e687289dcf6a2ac9a6e"
    )
    assert set(windows_custody._SOURCE_FILES) == {
        "pyproject.toml",
        "uv.lock",
        "pipe-client.ps1",
        "syn_host_custody/__init__.py",
        "syn_host_custody/broker.py",
        "syn_host_custody/core.py",
        "syn_host_custody/merkle.py",
        "syn_host_custody/owner.py",
        "syn_host_custody/peer_record.py",
    }


def test_windows_custody_policy_refuses_any_other_syn_release() -> None:
    windows_custody.require_windows_custody_policy(custody_policy())
    changed = custody_policy()
    changed["version"] = "0.10.1"
    with pytest.raises(StateMismatchError, match="exact signed Syn 0.11.0"):
        windows_custody.require_windows_custody_policy(changed)


def test_git_blob_identity_matches_git_object_encoding() -> None:
    data = b"host custody\n"
    expected = hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
        usedforsecurity=False,
    ).hexdigest()
    assert windows_custody._git_blob_sha1(data) == expected


def test_windows_custody_environment_fails_closed_without_bridge(monkeypatch, tmp_path) -> None:
    class Fixture(SynPublicCommands):
        root = tmp_path
        policy = SimpleNamespace(value=custody_policy())

    monkeypatch.setenv(windows_custody.REQUIRED_MARKER, "1")
    for name in windows_custody.CUSTODY_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(StateMismatchError, match="refusing Linux keyring fallback"):
        Fixture()._environment()

    monkeypatch.setenv("SYN_HOST_CUSTODY_PIPE", "SynHostCustody-" + "a" * 32)
    monkeypatch.setenv(
        "SYN_HOST_CUSTODY_CLIENT_WIN", "/usr/local/libexec/potbot-parent-keystore-client"
    )
    monkeypatch.setenv(
        "SYN_HOST_CUSTODY_POWERSHELL", "/usr/local/libexec/potbot-parent-keystore-client"
    )
    monkeypatch.setenv("POTBOT_PARENT_KEYSTORE_HOST", "172.30.16.1")
    monkeypatch.setenv("POTBOT_PARENT_KEYSTORE_PORT", "42319")
    monkeypatch.setenv("POTBOT_PARENT_KEYSTORE_SESSION", "SynHostCustody-" + "a" * 32)
    monkeypatch.setenv("POTBOT_PARENT_KEYSTORE_TOKEN", "11" * 32)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "must-not-be-used")
    environment = Fixture()._environment()
    assert environment[windows_custody.REQUIRED_MARKER] == "1"
    assert "DBUS_SESSION_BUS_ADDRESS" not in environment
    assert set(windows_custody.CUSTODY_ENVIRONMENT) <= set(environment)


def test_host_custody_systemd_unit_has_no_gnome_keyring_fallback() -> None:
    raw = system_service._linux_unit(["/verified/potbot"], 1000, host_custody=True).decode()
    assert "gnome-keyring" not in raw
    assert "Environment=POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED=1" in raw
    assert (
        "PassEnvironment=SYN_HOST_CUSTODY_PIPE SYN_HOST_CUSTODY_CLIENT_WIN "
        "SYN_HOST_CUSTODY_POWERSHELL POTBOT_PARENT_KEYSTORE_HOST "
    ) in raw
    assert "POTBOT_PARENT_KEYSTORE_TOKEN" in raw
    native = system_service._linux_unit(["/verified/potbot"], 1000).decode()
    assert "Requires=gnome-keyring-daemon.service" in native


def test_lifetime_runner_imports_bridge_and_holds_broker_for_operator() -> None:
    source = windows_custody.RUNNER_SOURCE
    assert '"import-environment", *ENV_NAMES' in source
    assert '"--wait", "restart", "potbot-syn-operator.service"' in source
    assert '"POTBOT_WINDOWS_HOST_CUSTODY_REQUIRED": "1"' in source
    assert "--owner-created" in source


def test_live_parent_keystore_quiesces_the_exact_legacy_task_before_vault_open() -> None:
    source = Path(windows_custody.__file__).read_text(encoding="utf-8")
    script = windows_custody._QUIESCE_WINDOWS_LIFETIME
    assert "Stop-ScheduledTask -TaskName $taskName" in script
    assert "Potbot managed Syn operator with Windows DPAPI custody; host " in script
    assert "Potbot managed Syn operator; host " in script
    assert '"is-active", "--quiet", "potbot-syn-operator.service"' in source
    assert '"stop", "potbot-syn-operator.service"' in source
    assert "_quiesce_windows_lifetime(manager)" in source
    assert source.index("_quiesce_windows_lifetime(manager)") < source.index(
        "runtime = prepare_windows_host_custody(manager.root)",
        source.index("def windows_host_custody"),
    )
    assert 'manager.root / "parent-keystore-authority.log"' in source


def test_parent_keystore_server_waits_for_retired_vault_owner() -> None:
    source = (
        Path(windows_custody.__file__).with_name("parent_keystore_server.py")
    ).read_text(encoding="utf-8")
    assert "except VaultInUseError:" in source
    assert "deadline = time.monotonic() + 10.0" in source
    assert "Parent keystore vault is still owned by another Syn authority" in source


def test_parent_keystore_server_restores_only_verified_source_under_isolated_python() -> None:
    source = (
        Path(windows_custody.__file__).with_name("parent_keystore_server.py")
    ).read_text(encoding="utf-8")
    insert = 'sys.path.insert(0, str(_SOURCE_ROOT))'
    imported = "from syn_host_custody.broker import"
    assert "_SOURCE_ROOT = Path(__file__).resolve(strict=True).parent" in source
    assert 'raise RuntimeError("Parent keystore source root is redirected")' in source
    assert insert in source
    assert source.index(insert) < source.index(imported)


def test_windows_service_can_replace_only_the_exact_legacy_helper(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "operator-user-task.ps1"
    legacy = b"known legacy helper"
    path.write_bytes(legacy)
    monkeypatch.setattr(
        windows_service,
        "_LEGACY_INSTALL_SHA256",
        hashlib.sha256(legacy).hexdigest(),
    )
    windows_service._write_managed_script(path, b"new helper")
    assert path.read_bytes() == b"new helper"

    path.write_bytes(b"unknown helper")
    with pytest.raises(StateMismatchError, match="differs"):
        windows_service._write_managed_script(path, b"another helper")


def test_host_custody_service_install_does_not_start_before_windows_broker() -> None:
    source = Path(system_service.__file__).read_text(encoding="utf-8")
    assert '["/usr/bin/systemctl", "--user", "disable", "--now", path.name]' in source
    assert '"enabled_awaiting_host_custody"' in source
