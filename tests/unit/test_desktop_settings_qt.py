"""Actual offscreen Qt interaction; synthetic API records are explicitly fixtures."""

# ruff: noqa: E402

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("POTBOT_REQUIRE_DESKTOP_TESTS") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6", reason="Qt desktop runtime has its own required CI lane")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidget, QMainWindow

from potbot.desktop_settings import create_settings_widget
from potbot.desktop_settings_client import NativeConnection, NativeSettingsClient
from potbot.desktop_settings_state import RevocationJournal


class FixtureAPI:
    def __init__(self, workspace=None, name="Fixture model"):
        path = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/settings-http.json"
        self.wire = json.loads(path.read_text(encoding="utf-8"))
        if workspace:
            for value in self.wire.values():
                if isinstance(value, dict) and value.get("schema") == "potbot-settings/v1":
                    value["workspaceId"] = workspace
        self.wire["swarm"]["nodes"] = [
            {
                "id": "88888888-8888-4888-8888-888888888888",
                "label": "node.fixture.invalid",
                "generation": 1,
                "state": "registered",
                "sshFingerprint": "SHA256:" + "A" * 43,
                "registeredAt": "2026-11-24T21:00:00+00:00",
                "lastAuthenticatedContact": None,
                "runtimeQualified": False,
            }
        ]
        self.wire["models"]["items"][0]["name"] = name
        self.preferences = {"revision": 1, "settings": {"theme": "dark", "graphDisplay": "3d"}}
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.delay_models = False
        self.lose_reply = False

    def client(self, name="Home fixture"):
        return NativeSettingsClient(
            NativeConnection(
                self.wire["catalogue"]["workspaceId"],
                name,
                "http://127.0.0.1:8765",
                "fixture-native-access-" * 3,
                principal_id=self.wire["catalogue"]["principalId"],
                setup=lambda: "fixture-setup-" * 4,
            ),
            self.transport,
        )

    def transport(self, method, url, headers, body):
        resource = url.rsplit("/", 1)[-1]
        resource = "swarm" if resource == "nodes" else resource
        value = json.loads(body) if body else None
        self.calls.append((method, resource, value))
        if resource == "models" and self.delay_models:
            self.entered.set()
            assert self.release.wait(4), "test did not release delayed model read"
        if resource == "preferences":
            if method == "PUT":
                if value["revision"] != self.preferences["revision"]:
                    return 409, b"PRIVATE conflict diagnostic"
                self.preferences = {
                    "revision": value["revision"] + 1,
                    "settings": value["settings"],
                }
            return 200, json.dumps(self.preferences).encode()
        if resource == "revocations":
            requests = self.wire["swarm"]["requests"]
            if not any(row["requestId"] == value["requestId"] for row in requests):
                requests.append(
                    {
                        "requestId": value["requestId"],
                        "grantId": value["grantId"],
                        "expectedRevision": value["expectedRevision"],
                        "state": "requested",
                        "recordedAt": "2026-11-24T21:00:00+00:00",
                    }
                )
            if self.lose_reply:
                self.lose_reply = False
                raise OSError("PRIVATE simulated lost reply after commit")
            return 202, json.dumps(
                {**self.wire["catalogue"], "requestId": value["requestId"], "state": "requested"}
            ).encode()
        return 200, json.dumps(copy.deepcopy(self.wire[resource])).encode()


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # Windows' offscreen QPA plugin has no automatic system font discovery.
    # Load an existing OS font into this isolated test application, never into the user profile.
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            identity = QFontDatabase.addApplicationFont(str(candidate))
            assert identity >= 0
            families = QFontDatabase.applicationFontFamilies(identity)
            assert families
            app.setFont(QFont(families[0], 10))
            break
    assert app.font().family()
    return app


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert predicate(), "native Qt condition was not reached"


@pytest.fixture
def native(application, tmp_path):
    widgets = []

    def make(clients=None, api=None):
        api = api or FixtureAPI()
        clients = clients or [api.client()]
        widget = create_settings_widget(
            client_loader=lambda: clients,
            journal=RevocationJournal(tmp_path / "private" / "revocations"),
        )
        widget.resize(1280, 860)
        widget.show()
        widgets.append(widget)
        until(lambda: bool(widget._catalogue) and not widget._busy)
        return widget, api

    yield make
    for widget in widgets:
        widget.close()
        assert widget.pool.waitForDone(6000)
        widget.deleteLater()
    QApplication.processEvents()


def capture(widget, name):
    root = os.environ.get("POTBOT_NATIVE_UI_CAPTURE_DIR")
    if root:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        QApplication.processEvents()
        assert widget.grab().save(str(path / (name + ".png")))


def test_native_widget_displays_all_settings_and_keyboard_navigation(native):
    widget, _api = native()
    assert widget.values.topLevelItemCount() == 10
    assert widget.features.topLevelItemCount() == 29
    assert widget.category.count() == 15
    widget.target.setFocus()
    QTest.keyClick(widget.target, Qt.Key.Key_Tab)
    assert widget.category.hasFocus()
    capture(widget, "native-settings-overview")


def test_native_preference_save_and_conflict_rebase_use_existing_api(native):
    widget, api = native()
    widget.open_section("appearance")
    until(lambda: widget._draft() is not None and not widget._busy)
    assert len(widget.preference_controls) == 7
    theme = widget.preference_controls["theme"]
    theme.setFocus()
    QTest.keyClick(theme, Qt.Key.Key_End)
    assert theme.currentData() == "light" and widget.save.isEnabled()
    api.preferences = {"revision": 2, "settings": {"theme": "dark", "graphDisplay": "list"}}
    QTest.mouseClick(widget.save, Qt.MouseButton.LeftButton)
    until(lambda: widget._draft().latest is not None and not widget._busy)
    assert widget.rebase.isVisible() and widget._draft().draft["theme"] == "light"
    QTest.mouseClick(widget.rebase, Qt.MouseButton.LeftButton)
    assert widget._draft().draft == {"theme": "light", "graphDisplay": "list"}
    QTest.mouseClick(widget.save, Qt.MouseButton.LeftButton)
    until(lambda: api.preferences["revision"] == 3 and not widget._busy)
    assert widget._draft().changed == []
    assert api.preferences["settings"] == {"theme": "light", "graphDisplay": "list"}
    assert "revision 3" in widget.operation.text()
    assert "#f3f7fa" in widget.styleSheet()
    capture(widget, "native-settings-appearance")


@pytest.mark.parametrize("section", ["models", "tools"])
def test_native_inventory_is_a_real_searchable_tree_with_separate_evidence(native, section):
    widget, _api = native()
    widget.open_section(section)
    until(lambda: widget._document and widget._document.get("kind") == section and not widget._busy)
    assert widget.inventory.topLevelItemCount() >= 1
    row = widget.inventory.topLevelItem(0)
    widget.inventory.setCurrentItem(row)
    assert "not_evaluated" in widget.inventory_details.toPlainText()
    assert "unknown" in widget.inventory_details.toPlainText()
    widget.search.setText("no such record")
    assert row.isHidden()
    widget.search.clear()
    assert not row.isHidden()
    capture(widget, "native-settings-" + section)
    widget.resize(900, 680)
    QApplication.processEvents()
    assert widget.category.isVisible() and widget.refresh.isVisible()


def test_native_scope_switch_discards_late_read_and_preserves_home_context(native):
    home, other = FixtureAPI(name="HOME-ONLY"), FixtureAPI(str(uuid4()), "OTHER-ONLY")
    home.delay_models = True
    widget, _api = native(clients=[home.client("Home fixture"), other.client("Other fixture")])
    widget.open_section("models")
    until(home.entered.is_set)
    widget.target.setCurrentIndex(1)
    until(
        lambda: (
            widget._document
            and widget._document.get("workspaceId") == other.wire["catalogue"]["workspaceId"]
            and not widget._busy
        )
    )
    home.release.set()
    until(lambda: not widget._jobs)
    assert widget.inventory.topLevelItem(0).text(0) == "OTHER-ONLY"
    assert widget._home == home.wire["catalogue"]["workspaceId"]
    assert "Home fixture" in widget.home_label.text()


def test_native_revocation_reviews_exact_identity_and_survives_a_lost_reply(native):
    widget, api = native()
    widget.open_section("swarm")
    until(lambda: widget._document and "grants" in widget._document and not widget._busy)
    assert widget.nodes.topLevelItemCount() == 1
    widget.swarm_tabs.setCurrentIndex(2)
    widget.grants.setCurrentItem(widget.grants.topLevelItem(0))
    assert widget.revoke.isEnabled()
    QTest.mouseClick(widget.revoke, Qt.MouseButton.LeftButton)
    until(lambda: widget.review_dialog is not None and widget.review_dialog.isVisible())
    retained = dict(widget._review)
    capture(widget, "native-settings-swarm")
    capture(widget.review_dialog, "native-settings-revocation-review")
    QTest.keyClick(widget.review_dialog, Qt.Key.Key_Escape)
    assert not any(call[0] == "POST" for call in api.calls)
    QTest.mouseClick(widget.revoke, Qt.MouseButton.LeftButton)
    until(lambda: widget.review_dialog.isVisible())
    assert widget._review == retained
    api.lose_reply = True
    QTest.mouseClick(widget.review_submit, Qt.MouseButton.LeftButton)
    until(lambda: not widget._busy)
    assert "not confirmed" in widget.review_notice.text()
    assert "PRIVATE" not in widget.review_notice.text()
    assert len([call for call in api.calls if call[0] == "POST"]) == 1
    QTest.qWait(100)
    assert len([call for call in api.calls if call[0] == "POST"]) == 1
    QTest.mouseClick(widget.review_submit, Qt.MouseButton.LeftButton)
    until(lambda: widget.requests.topLevelItemCount() == 1 and not widget._busy)
    posts = [call[2] for call in api.calls if call[0] == "POST"]
    assert posts == [retained, retained]
    assert widget.requests.topLevelItem(0).text(3) == "requested"
    assert widget.grants.topLevelItem(0).text(1) == "active"
    assert "not revocation" in widget.operation.text()
    widget.swarm_tabs.setCurrentIndex(3)
    widget.requests.setCurrentItem(widget.requests.topLevelItem(0))
    assert retained["requestId"] in widget.swarm_details.toPlainText()
    assert "--confirm" in widget.swarm_details.toPlainText()
    capture(widget, "native-settings-pending-request")


def test_native_desktop_navigation_opens_qt_panels_without_a_browser(
    application, monkeypatch, tmp_path
):
    from potbot import desktop, desktop_settings, oneclick
    from potbot.desktop_state import DesktopStatus

    factory = desktop_settings.create_settings_widget
    api, starts, errors = FixtureAPI(), [], []
    monkeypatch.setattr(
        desktop_settings,
        "create_settings_widget",
        lambda parent: factory(
            parent,
            client_loader=lambda: [api.client()],
            journal=RevocationJournal(tmp_path / "private" / "revocations"),
        ),
    )
    monkeypatch.setattr(oneclick, "start_services", lambda **args: starts.append(args) or 0)
    monkeypatch.setattr(desktop, "probe_status", lambda: DesktopStatus(False, False, False, False))

    def inspect():
        owned = None
        try:
            owned = next(
                widget
                for widget in QApplication.topLevelWidgets()
                if isinstance(widget, QMainWindow) and widget.windowTitle() == "Potbot"
            )
            nav = owned.findChild(QListWidget, "workspaceNav")
            assert [nav.item(index).text() for index in range(nav.count())][-5:] == [
                "Models",
                "Tools",
                "Workspace swarm",
                "Settings",
                "Preferences",
            ]
            nav.setCurrentRow(4)
            assert owned.settings_panel.isVisible()
            assert owned.settings_panel._section == "tools"
            capture(owned, "native-desktop-settings-shell")
        except Exception as error:
            errors.append(error)
        finally:
            if owned:
                owned.close()
                owned.settings_panel.pool.waitForDone(6000)
            application.quit()

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(application.quit)
    timer.start(6000)
    QTimer.singleShot(200, inspect)
    assert desktop.main() == 0
    timer.stop()
    assert not errors, errors
    assert starts == [{"open_browser": False}]
