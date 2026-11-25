"""Exercise the src-layout daemon in an interpreter with no installed Potbot."""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import venv
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Real Linux Secret Service launch")


def _stage_source(root: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    stage = root / "staged source with 'quotes'"
    for relative in (
        "src/potbot/__init__.py",
        "src/potbot/syn_release/__init__.py",
        "src/potbot/syn_release/parent_keystore_client.py",
        "src/potbot/syn_release/secret_service_proxy.py",
    ):
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
    return stage


def _isolated_proxy(tmp_path):
    from potbot import wsl_core_runtime as runtime

    dbus = importlib.util.find_spec("dbus_next")
    assert dbus is not None and dbus.origin, "Install the locked dbus-next dev dependency"
    stage = _stage_source(tmp_path)
    interpreter_home = tmp_path / "isolated-python"
    venv.EnvBuilder(with_pip=False).create(interpreter_home)
    python = interpreter_home / "bin/python"
    # Only dbus-next is installed, matching the system interpreter's role in
    # Ubuntu. A checkout editable install must not hide the wrong import path.
    site = subprocess.check_output(
        [str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True, timeout=10,
    ).strip()
    shutil.copytree(Path(dbus.origin).parent, Path(site) / "dbus_next")
    absent = subprocess.run(
        [str(python), "-I", "-B", "-c",
         "import importlib.util; assert importlib.util.find_spec('potbot') is None"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert absent.returncode == 0, absent.stderr

    command = runtime._secret_service_command(stage)
    assert command[:4] == ["/usr/bin/python3.12", "-I", "-B", "-c"]
    command[0] = str(python)
    return stage, command


def test_isolated_command_imports_actual_proxy_without_installed_potbot(tmp_path):
    stage, command = _isolated_proxy(tmp_path)
    # Reach the real daemon entry point with no user bus. It must report a bus
    # connection error, not a package-import traceback. This runs even in
    # sandboxes that prohibit creating Unix-domain sockets.
    environment = {
        **os.environ,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-potbot-test-bus-" + uuid4().hex,
        "PYTHONPATH": str(tmp_path / "ignored-PYTHONPATH"),
    }
    result = subprocess.run(
        command, env=environment, cwd=tmp_path,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith("Parent Secret Service proxy failed: "), result.stderr
    assert "No module named" not in result.stderr

    # Execute the exact bootstrap probe too, with real dbus-next decorators.
    from potbot.windows_core_runtime import _ROOT_BOOTSTRAP

    probe = next(line.strip().strip("'") for line in _ROOT_BOOTSTRAP.splitlines()
                 if "sys.path.insert(0, sys.argv[1])" in line).removesuffix(" \\")
    probe = probe.strip("'")
    checked = subprocess.run(
        [command[0], "-I", "-B", "-c", probe, str(stage / "src")],
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_isolated_system_style_interpreter_starts_actual_proxy(monkeypatch, tmp_path):
    from potbot import wsl_core_runtime as runtime

    daemon, busctl = shutil.which("dbus-daemon"), shutil.which("busctl")
    if not daemon or busctl != "/usr/bin/busctl":
        pytest.skip("The real daemon launch needs dbus-daemon and /usr/bin/busctl")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("This environment prohibits Unix-domain sockets; isolated import runs separately")
    stage, command = _isolated_proxy(tmp_path)
    # A private bus has no user credentials and no existing Secret Service.
    address = "unix:abstract=potbot-secret-service-test-" + uuid4().hex
    environment = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": address}
    environment["PYTHONPATH"] = str(tmp_path / "ignored-PYTHONPATH")
    bus = subprocess.Popen(
        [daemon, "--session", "--nofork", "--address=" + address],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    proxy = None
    try:
        deadline = time.monotonic() + 5
        while True:
            if bus.poll() is not None:
                pytest.fail("Private D-Bus daemon exited: " + repr(bus.communicate()[1]))
            probe = subprocess.run(
                [busctl, "--user", "--quiet", "list"], env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False,
            )
            if probe.returncode == 0:
                break
            assert time.monotonic() < deadline, "Private D-Bus daemon did not start"
            time.sleep(0.05)
        # This is the production launcher and real health probe. Only the
        # interpreter location differs from Ubuntu's /usr/bin/python3.12.
        monkeypatch.setattr(runtime, "_secret_service_command", lambda source: command)
        proxy = runtime._start_secret_service(stage, environment, threading.Event())
        assert proxy.poll() is None
        owner = subprocess.run(
            [busctl, "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
             "org.freedesktop.DBus", "GetConnectionUnixProcessID", "s", "org.freedesktop.secrets"],
            env=environment, capture_output=True, text=True, timeout=5, check=False,
        )
        assert owner.returncode == 0, owner.stderr
        assert owner.stdout.strip() == f"u {proxy.pid}"
    finally:
        for process in (proxy, bus):
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if bus.stderr is not None:
            bus.stderr.close()


def test_missing_staged_package_fails_before_spawning(monkeypatch, tmp_path):
    from potbot import wsl_core_runtime as runtime
    from potbot.domain.errors import CapabilityUnavailableError

    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("invalid source"))
    with pytest.raises(CapabilityUnavailableError, match="source package is missing or invalid"):
        runtime._start_secret_service(tmp_path, {}, threading.Event())


def test_linked_import_directory_is_rejected(tmp_path):
    from potbot import wsl_core_runtime as runtime
    from potbot.domain.errors import CapabilityUnavailableError

    stage = _stage_source(tmp_path)
    (stage / "src").rename(stage / "real-src")
    (stage / "src").symlink_to(stage / "real-src", target_is_directory=True)
    with pytest.raises(CapabilityUnavailableError, match="source package is missing or invalid"):
        runtime._secret_service_command(stage)
