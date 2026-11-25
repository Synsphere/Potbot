"""Actual Qt reviews through HTTP into SQL and an owned native worker supervisor."""

# ruff: noqa: E402
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("POTBOT_REQUIRE_DESKTOP_TESTS") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from potbot.desktop_live_settings import create_live_dialog
from potbot.desktop_settings import native_stylesheet
from potbot.desktop_settings_client import NativeConnection, NativeSettingsClient
from potbot.desktop_settings_state import RevocationJournal
from tests.integration.test_desktop_settings import wait_until
from tests.integration.test_live_controls import ORIGIN, SETUP, TOKEN, client_for
from tests.integration.test_live_controls import managed as managed

pytestmark = pytest.mark.postgres


def test_qt_live_configuration_and_restart_have_actual_database_and_process_receipts(
    app_env, managed, tmp_path
):
    supervisor, children, nodes = managed
    http = client_for(app_env)
    exchanges = []

    def transport(method, url, headers, body):
        response = http.request(method, url, headers=headers, content=body)
        exchanges.append(
            {"method": method, "path": response.request.url.path, "status": response.status_code}
        )
        return response.status_code, response.content

    api = NativeSettingsClient(
        NativeConnection(
            str(app_env.context.workspace_id),
            "Disposable PostgreSQL fixture",
            "http://127.0.0.1:8765",
            TOKEN,
            ORIGIN,
            str(app_env.context.principal_id),
            lambda: SETUP,
        ),
        transport,
    )
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    journal = RevocationJournal(tmp_path / "private" / "live")
    dialogs = []

    def open_dialog(mode):
        dialog = create_live_dialog(
            None, api, str(app_env.context.principal_id), mode, journal=journal
        )
        dialog.setStyleSheet(native_stylesheet("dark"))
        dialogs.append(dialog)
        dialog.show()
        wait_until(lambda: dialog.snapshot is not None and not dialog._busy)
        return dialog

    try:
        configuration = open_dialog("configuration")
        enabled, editor = configuration.fields["max_packet_tokens"]
        enabled.setChecked(True)
        editor.setValue(4096)
        QTest.mouseClick(configuration.save, Qt.MouseButton.LeftButton)
        wait_until(lambda: configuration.review_dialog is not None and not configuration._busy)
        QTest.mouseClick(configuration.review_submit, Qt.MouseButton.LeftButton)
        wait_until(lambda: configuration.snapshot["revision"] == 1 and not configuration._busy)
        with app_env.db.transaction(app_env.context) as connection:
            row = connection.execute(
                "SELECT overrides FROM workspace_configuration_revisions WHERE revision=1"
            ).fetchone()
        assert row["overrides"] == {"max_packet_tokens": 4096}
        configuration.reject()

        dialog = open_dialog("nodes")
        old_pid = children.children[0].process.pid
        QTest.mouseClick(dialog.restart, Qt.MouseButton.LeftButton)
        wait_until(lambda: dialog.review_dialog is not None and not dialog._busy)
        review = dict(dialog._retained)
        QTest.mouseClick(dialog.review_submit, Qt.MouseButton.LeftButton)
        wait_until(lambda: dialog.snapshot["requests"] and not dialog._busy)
        assert dialog.snapshot["requests"][0]["state"] == "requested"
        assert len(children.children) == 1 and children.children[0].running()
        supervisor.tick()
        dialog.load()
        wait_until(
            lambda: not dialog._busy and dialog.snapshot["requests"][0]["state"] == "applied"
        )
        assert dialog.snapshot["nodes"][0]["incarnation"] != review["expectedIncarnation"]
        assert not children.children[0].running() and children.children[1].running()
        assert children.children[1].process.pid != old_pid
        with app_env.db.transaction(app_env.context) as connection:
            stages = connection.execute(
                "SELECT stage FROM node_control_events WHERE request_id=%s",
                (UUID(review["requestId"]),),
            ).fetchall()
        assert {row["stage"] for row in stages} == {"accepted", "applied"}
        observed = nodes.read(app_env.context, setup_available=True)
        assert observed["requests"][0]["state"] == "applied"
        output = Path("artifacts/live-controls/postgres-native-http.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "fixture": "Actual Qt/HTTP/SQL and owned fixture processes; no production node qualification",
                    "exchanges": exchanges,
                    "configurationRevision": 1,
                    "requestState": "applied",
                    "oldWorkerExited": True,
                    "newWorkerReady": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if os.environ.get("POTBOT_QT_CAPTURE_DIR"):
            capture = Path(os.environ["POTBOT_QT_CAPTURE_DIR"])
            capture.mkdir(parents=True, exist_ok=True)
            assert dialog.grab().save(str(capture / "live-postgres-restart.png"))
    finally:
        for dialog in dialogs:
            dialog.invalidate_scope()
            assert dialog.pool.waitForDone(6000)
        QApplication.processEvents()
        http.close()
