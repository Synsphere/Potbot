"""Native Qt -> existing HTTP APIs -> genuine disposable PostgreSQL acceptance."""

# ruff: noqa: E402
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("POTBOT_REQUIRE_DESKTOP_TESTS") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6", reason="Native settings have a required Qt/PostgreSQL CI lane")

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

from potbot.api.ui_resources import register_ui_routes
from potbot.api.ui_settings import register_settings_routes
from potbot.controller.remote_workspaces import current_control_grant
from potbot.controller.swarm_requests import apply_revocation
from potbot.desktop_settings import create_settings_widget
from potbot.desktop_settings_client import NativeConnection, NativeSettingsClient
from potbot.desktop_settings_state import RevocationJournal
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.persistence.database import admin_connection
from potbot.tools.local_inventory import InventoryProfile
from tests.integration.test_ui_settings import seed

pytestmark = pytest.mark.postgres
TOKEN = "native-api-fixture-" * 3
SETUP = "native-setup-fixture-" * 3
ORIGIN = "http://127.0.0.1:3100"


def wait_until(predicate):
    deadline = time.monotonic() + 8
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert predicate(), "Qt/API/database postcondition was not reached"


def test_native_qt_preferences_inventory_and_revocation_use_actual_api_and_ledger(
    app_env, tmp_path
):
    _node, grant = seed(app_env)
    project = tmp_path / "native-inventory-fixture"
    (project / "models").mkdir(parents=True)
    package = project / "packages/native-1.dist-info"
    package.mkdir(parents=True)
    (project / "models/native-fixture.gguf").write_bytes(b"Synthetic fixture; not model weights")
    (package / "METADATA").write_text("Name: native-fixture\nVersion: 1.0\n\nprivate description")
    profile = InventoryProfile(
        app_env.context.workspace_id, project, "PostgreSQL fixture host", ("models",), ("packages",)
    )
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def errors(_request, error):
        status = (
            409
            if isinstance(error, RevisionConflictError)
            else 400
            if isinstance(error, InvalidInputError)
            else 403
        )
        return JSONResponse({"error": error.code.value}, status_code=status)

    def authenticate(value):
        if value != "Bearer " + TOKEN:
            raise ScopeDeniedError("Unavailable")
        return app_env.context

    def setup(value):
        if value != SETUP:
            raise ScopeDeniedError("Unavailable")

    register_ui_routes(app, app_env.db, authenticate, (ORIGIN,))
    register_settings_routes(
        app, app_env.db, authenticate, (ORIGIN,), app_env.context.workspace_id, setup, profile
    )
    http = TestClient(app)
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
            "Disposable database fixture",
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
    widget = create_settings_widget(
        client_loader=lambda: [api], journal=RevocationJournal(tmp_path / "private/revocations")
    )
    widget.resize(1280, 860)
    widget.show()
    try:
        wait_until(lambda: widget._catalogue and not widget._busy)
        widget.open_section("appearance")
        wait_until(lambda: widget._draft() and not widget._busy)
        widget.preference_controls["theme"].setCurrentIndex(1)
        QTest.mouseClick(widget.save, Qt.MouseButton.LeftButton)
        wait_until(lambda: widget._draft().base["revision"] == 1 and not widget._busy)

        widget.preference_controls["theme"].setCurrentIndex(2)
        concurrent = http.put(
            "/v1/ui/preferences",
            headers={"Authorization": "Bearer " + TOKEN, "Origin": ORIGIN},
            json={"revision": 1, "settings": {"theme": "dark", "graphDisplay": "list"}},
        )
        assert concurrent.status_code == 200
        QTest.mouseClick(widget.save, Qt.MouseButton.LeftButton)
        wait_until(lambda: widget._draft().latest and not widget._busy)
        QTest.mouseClick(widget.rebase, Qt.MouseButton.LeftButton)
        QTest.mouseClick(widget.save, Qt.MouseButton.LeftButton)
        wait_until(lambda: widget._draft().base["revision"] == 3 and not widget._busy)
        assert api.read("preferences")["settings"] == {"theme": "light", "graphDisplay": "list"}

        for section, expected in (("models", "native-fixture.gguf"), ("tools", "native-fixture")):
            widget.open_section(section)
            wait_until(
                lambda expected_section=section: (
                    widget._document
                    and widget._document.get("kind") == expected_section
                    and not widget._busy
                )
            )
            assert widget.inventory.topLevelItem(0).text(0) == expected
            assert widget.inventory.topLevelItem(0).text(3) == "observed"
            assert widget.inventory.topLevelItem(0).text(4) == "unknown"
            widget.inventory.setCurrentItem(widget.inventory.topLevelItem(0))
            visible = " ".join(label.text() for label in widget.findChildren(QLabel)) + " ".join(
                view.toPlainText() for view in widget.findChildren(QPlainTextEdit)
            )
            assert str(project) not in visible and TOKEN not in visible and SETUP not in visible

        widget.open_section("swarm")
        wait_until(lambda: widget._document and "grants" in widget._document and not widget._busy)
        assert widget.nodes.topLevelItemCount() == 1
        widget.swarm_tabs.setCurrentIndex(2)
        widget.grants.setCurrentItem(widget.grants.topLevelItem(0))
        QTest.mouseClick(widget.revoke, Qt.MouseButton.LeftButton)
        wait_until(lambda: widget.review_dialog and widget.review_dialog.isVisible())
        request_id = UUID(widget._review["requestId"])
        QTest.mouseClick(widget.review_submit, Qt.MouseButton.LeftButton)
        wait_until(lambda: widget.requests.topLevelItemCount() == 1 and not widget._busy)
        assert widget.requests.topLevelItem(0).text(3) == "requested"
        with app_env.db.transaction(app_env.context) as connection:
            assert not current_control_grant(connection, grant.workspace_id, grant.id).revoked

        # An independent protected operator applies; Qt never obtains its DSN.
        with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
            apply_revocation(
                connection,
                workspace_id=app_env.context.workspace_id,
                owner_principal_id=app_env.context.principal_id,
                request_id=request_id,
            )
        QTest.mouseClick(widget.refresh, Qt.MouseButton.LeftButton)
        wait_until(
            lambda: (
                widget.requests.topLevelItemCount() == 1
                and widget.requests.topLevelItem(0).text(3) == "applied"
                and not widget._busy
            )
        )
        assert widget.grants.topLevelItem(0).text(1) == "revoked"
        widget.swarm_tabs.setCurrentIndex(3)
        widget.requests.setCurrentItem(widget.requests.topLevelItem(0))
        assert not widget.copy_handoff.isEnabled()

        output = Path("artifacts/native-settings/postgres-native-http.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "fixture": "Disposable SQL-backed Qt/HTTP acceptance; no real node qualification",
                    "workspaceId": str(app_env.context.workspace_id),
                    "exchanges": exchanges,
                    "preferencesRevision": 3,
                    "grantState": "revoked",
                    "requestState": "applied",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if os.environ.get("POTBOT_NATIVE_UI_CAPTURE_DIR"):
            capture = Path(os.environ["POTBOT_NATIVE_UI_CAPTURE_DIR"])
            capture.mkdir(parents=True, exist_ok=True)
            assert widget.grab().save(str(capture / "native-postgres-applied.png"))
    finally:
        widget.close()
        assert widget.pool.waitForDone(6000)
        widget.deleteLater()
        QApplication.processEvents()
        http.close()
