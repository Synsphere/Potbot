"""Native desktop milestone contracts that do not require a graphical test runner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from potbot import desktop, startup
from potbot.desktop_state import DesktopStatus
from potbot.domain.errors import InvalidInputError


def test_workspace_rendering_is_independent_of_xpra_or_backend() -> None:
    status = DesktopStatus(
        configured=True,
        backend=True,
        xpra=False,
        web=False,
        workspace_id="00000000-0000-4000-8000-000000000001",
    )
    assert status.workspace_renderable is True
    assert "starting Xpra" in status.banner
    empty = DesktopStatus(False, False, False, False)
    assert empty.workspace_renderable is True
    assert "Preparing" in empty.banner


def test_xpra_status_requires_fresh_matching_workspace(tmp_path, monkeypatch):
    import json

    from potbot import desktop_state

    monkeypatch.setattr(desktop_state.time, "time", lambda: 100.0)
    path = tmp_path / "xpra-status.json"
    document = {"workspace": "a", "stage": "ready", "ready": True, "updated": 99}
    path.write_text(json.dumps(document), encoding="utf-8")
    assert desktop_state._xpra_status(path, "a") == (True, "", False)
    assert desktop_state._xpra_status(path, "b") == (False, "", False)
    document.update(updated=50)
    path.write_text(json.dumps(document), encoding="utf-8")
    assert desktop_state._xpra_status(path, "a")[2]
    document.update(updated=99, stage="custody-tcp-failed", ready=False)
    path.write_text(json.dumps(document), encoding="utf-8")
    ready, detail, failed = desktop_state._xpra_status(path, "a")
    assert not ready and failed and "cannot reach" in detail


def test_native_window_is_shown_before_background_startup_begins() -> None:
    source = Path(desktop.__file__).read_text(encoding="utf-8")
    assert source.index("window.show()") < source.index("QTimer.singleShot(0, window.start_stack)")
    assert "No X applications are running yet. The workspace remains available." in source
    assert "start_services(open_browser=False, stop_event=lease.stopped)" in source


def test_local_ui_defaults_to_native_but_headless_linux_keeps_web_mode() -> None:
    assert startup._local_ui_mode({}, "win32") == "native"
    assert startup._local_ui_mode({}, "darwin") == "native"
    assert startup._local_ui_mode({"DISPLAY": ":0"}, "linux") == "native"
    assert startup._local_ui_mode({}, "linux") == "web"
    assert startup._local_ui_mode({"POTBOT_LOCAL_UI": "web"}, "win32") == "web"
    assert startup._local_ui_mode({"POTBOT_LOCAL_UI": "native"}, "linux") == "native"
    with pytest.raises(InvalidInputError):
        startup._local_ui_mode({"POTBOT_LOCAL_UI": "browser"}, "win32")


def test_pyside_is_lazy_imported_so_non_desktop_commands_stay_lightweight() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PySide6" or fullname.startswith("PySide6."):
            raise ImportError("Qt deliberately unavailable in this child process")
sys.meta_path.insert(0, NoQt())
from potbot import desktop, desktop_settings, desktop_settings_client, desktop_settings_state
from potbot.domain.errors import CapabilityUnavailableError
assert not any(name.startswith("PySide6") for name in sys.modules)
try:
    desktop.main()
except CapabilityUnavailableError:
    print("optional-desktop-boundary-passed")
else:
    raise AssertionError("Missing Qt did not fail qualification")
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "optional-desktop-boundary-passed"
