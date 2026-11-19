"""Native desktop milestone contracts that do not require a graphical test runner."""

from __future__ import annotations

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


def test_native_window_is_shown_before_background_startup_begins() -> None:
    source = Path(desktop.__file__).read_text(encoding="utf-8")
    assert source.index("window.show()") < source.index("QTimer.singleShot(0, window.start_stack)")
    assert "No X applications are running yet. The workspace remains available." in source
    assert "start_services(open_browser=False)" in source


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
    source = Path(desktop.__file__).read_text(encoding="utf-8")
    assert source.index("def main()") < source.index("from PySide6.QtCore import")
