"""Actual Qt interactions with explicitly synthetic live-control API records."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

if os.environ.get("POTBOT_REQUIRE_DESKTOP_TESTS") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from potbot.desktop_live_settings import create_live_dialog  # noqa: E402
from potbot.desktop_settings import native_stylesheet  # noqa: E402
from potbot.desktop_settings_state import RevocationJournal  # noqa: E402
from potbot.domain.configuration import resolve_configuration  # noqa: E402
from potbot.domain.live_controls import EDITABLE, SCHEMA  # noqa: E402
from potbot.domain.serialization import content_hash  # noqa: E402
from tests.unit.test_desktop_settings_qt import FixtureAPI, until  # noqa: E402
from tests.unit.test_desktop_settings_qt import application as application  # noqa: E402
from tests.unit.test_desktop_settings_qt import native as native  # noqa: E402


class LiveAPI(FixtureAPI):
    def __init__(self, workspace=None):
        super().__init__(workspace)
        self.revision, self.values = 0, {}
        self.binding = {
            "id": self.wire["swarm"]["nodes"][0]["id"],
            "label": "node.fixture.invalid",
            "generation": 1,
            "managed": True,
            "revision": 2,
            "incarnation": str(uuid4()),
            "phase": "ready",
            "desired": "active",
            "operatorFresh": True,
            "canManage": True,
            "unsettled": 0,
        }
        self.node_requests = []
        self.lose_node_reply = False
        self.headers = []

    def envelope(self):
        return {key: self.wire["catalogue"][key] for key in ("workspaceId", "principalId")} | {
            "schema": SCHEMA
        }

    def configuration(self):
        settings = resolve_configuration({"workspace": self.values}).settings
        return {
            **self.envelope(),
            "scope": "workspace",
            "revision": self.revision,
            "values": copy.deepcopy(self.values),
            "sha256": content_hash(self.values),
            "requestId": getattr(self, "last_config", None),
            "apply": "next_admission",
            "canEdit": True,
            "editableFields": list(EDITABLE),
            "effective": {key: settings[key] for key in EDITABLE},
        }

    def transport(self, method, url, headers, body):
        kind = url.rsplit("/", 1)[-1]
        if kind not in {"live", "control"}:
            return super().transport(method, url, headers, body)
        value = json.loads(body) if body else None
        self.calls.append((method, kind, value))
        self.headers.append((method, dict(headers)))
        if kind == "live":
            if method == "PUT":
                if value["expectedRevision"] != self.revision:
                    return 409, b"PRIVATE conflict body"
                self.revision += 1
                self.values = copy.deepcopy(value["values"])
                self.last_config = value["requestId"]
                return 200, json.dumps(
                    {
                        **self.envelope(),
                        "revision": self.revision,
                        "values": self.values,
                        "sha256": content_hash(self.values),
                        "requestId": self.last_config,
                        "apply": "next_admission",
                    }
                ).encode()
            return 200, json.dumps(self.configuration()).encode()
        if method == "POST":
            if not any(row["requestId"] == value["requestId"] for row in self.node_requests):
                self.node_requests.append(
                    {
                        "review": value,
                        **{
                            key: value[key]
                            for key in (
                                "requestId",
                                "nodeId",
                                "action",
                                "expectedRevision",
                                "expectedIncarnation",
                            )
                        },
                        "state": "requested",
                        "reason": "awaiting_native_operator",
                        "observedIncarnation": None,
                        "observedRevision": None,
                    }
                )
            if self.lose_node_reply:
                self.lose_node_reply = False
                raise OSError("PRIVATE reply lost after commit")
            return 202, json.dumps(
                {**self.envelope(), "review": value, "state": "requested"}
            ).encode()
        node = {
            **self.binding,
            "canManage": not any(
                row["state"] in {"requested", "accepted"} for row in self.node_requests
            ),
        }
        return 200, json.dumps(
            {
                **self.envelope(),
                "scope": "potbot_worker_service",
                "nodes": [node],
                "requests": self.node_requests,
                "recordsTruncated": False,
            }
        ).encode()


@pytest.fixture
def dialogs(application, tmp_path):
    opened = []

    def make(mode, api=None):
        api = api or LiveAPI()
        dialog = create_live_dialog(
            None,
            api.client(),
            api.wire["catalogue"]["principalId"],
            mode,
            journal=RevocationJournal(tmp_path / "private" / "live"),
        )
        dialog.setStyleSheet(native_stylesheet("dark"))
        dialog.show()
        opened.append(dialog)
        until(lambda: dialog.snapshot is not None and not dialog._busy)
        return dialog, api

    yield make
    for dialog in opened:
        dialog.invalidate_scope()
        assert dialog.pool.waitForDone(4000)


def capture(widget, name):
    root = os.environ.get("POTBOT_QT_CAPTURE_DIR")
    if root:
        target = Path(root)
        target.mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(target / (name + ".png")))


def confirm(dialog):
    until(lambda: dialog.review_dialog is not None and not dialog._busy)
    QTest.mouseClick(dialog.review_submit, Qt.MouseButton.LeftButton)


def test_native_live_configuration_reviews_cancel_saves_and_rebases(dialogs):
    dialog, api = dialogs("configuration")
    enabled, editor = dialog.fields["max_packet_tokens"]
    enabled.setChecked(True)
    editor.setValue(4096)
    QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
    until(lambda: dialog.review_dialog is not None)
    dialog.review_dialog.reject()
    assert api.revision == 0
    QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
    confirm(dialog)
    until(lambda: dialog.snapshot["revision"] == 1 and not dialog._busy)
    assert api.values == {"max_packet_tokens": 4096}

    enabled, editor = dialog.fields["physical_inference_concurrency"]
    enabled.setChecked(True)
    editor.setValue(2)
    api.values["coding_models"] = ["fixture/concurrent-model"]
    api.revision = 2
    QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
    confirm(dialog)
    until(lambda: dialog.state.latest is not None and not dialog._busy)
    assert dialog.state.draft["physical_inference_concurrency"] == 2
    QTest.mouseClick(dialog.rebase, Qt.MouseButton.LeftButton)
    assert dialog.state.draft["coding_models"] == ["fixture/concurrent-model"]
    QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
    confirm(dialog)
    until(lambda: dialog.snapshot["revision"] == 3 and not dialog._busy)
    assert api.values["physical_inference_concurrency"] == 2
    assert api.values["coding_models"] == ["fixture/concurrent-model"]
    assert all(
        "X-Potbot-Setup-Token" not in headers for method, headers in api.headers if method == "GET"
    )
    assert all(
        "X-Potbot-Setup-Token" in headers for method, headers in api.headers if method != "GET"
    )
    capture(dialog, "live-configuration")


def test_native_node_restart_keeps_original_review_after_lost_reply(dialogs):
    dialog, api = dialogs("nodes")
    api.lose_node_reply = True
    QTest.mouseClick(dialog.restart, Qt.MouseButton.LeftButton)
    until(lambda: dialog.review_dialog is not None and not dialog._busy)
    original = dict(dialog._retained)
    capture(dialog.review_dialog, "live-node-review")
    confirm(dialog)
    until(lambda: dialog._unknown and not dialog._busy)
    assert not dialog.review_submit.isEnabled()
    dialog.review_dialog.reject()
    QTest.mouseClick(dialog.refresh, Qt.MouseButton.LeftButton)
    until(lambda: len(dialog.snapshot["requests"]) == 1 and not dialog._busy)
    dialog.requests.setCurrentItem(dialog.requests.topLevelItem(0))
    QTest.mouseClick(dialog.retry, Qt.MouseButton.LeftButton)
    until(lambda: dialog.review_dialog is not None and not dialog._busy)
    assert dialog._retained == original
    confirm(dialog)
    until(lambda: not dialog._busy and dialog.review_dialog is None)
    posts = [
        body for method, resource, body in api.calls if method == "POST" and resource == "control"
    ]
    assert len(posts) == 2 and posts[0] == posts[1] == original
    assert len(api.node_requests) == 1
    assert dialog.snapshot["requests"][0]["state"] == "requested"
    api.binding.update(revision=6, incarnation=str(uuid4()))
    api.node_requests[0].update(
        state="applied",
        reason="replacement_worker_ready",
        observedRevision=6,
        observedIncarnation=api.binding["incarnation"],
    )
    dialog.load()
    until(lambda: not dialog._busy and dialog.snapshot["requests"][0]["state"] == "applied")
    capture(dialog, "live-node-applied")


def test_python_settings_navigation_opens_native_live_controls(native):
    widget, api = native(api=LiveAPI())
    QTest.mouseClick(widget.live_configuration, Qt.MouseButton.LeftButton)
    until(lambda: widget.live_dialog.snapshot is not None and not widget.live_dialog._busy)
    assert widget._mutating and not widget.target.isEnabled()
    widget.live_dialog.reject()
    until(lambda: not widget._mutating and not widget._busy)
    widget.open_section("swarm")
    until(lambda: not widget._busy)
    QTest.mouseClick(widget.live_nodes, Qt.MouseButton.LeftButton)
    until(lambda: widget.live_dialog.snapshot is not None and not widget.live_dialog._busy)
    assert widget.live_dialog.mode == "nodes"
    widget.live_dialog.reject()
    until(lambda: not widget._mutating and not widget._busy)
