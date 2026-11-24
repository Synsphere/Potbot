"""Native live editors; Qt stays optional and all effects use scoped APIs."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from potbot.desktop_settings_client import (
    NativeSettingsClient,
    NativeSettingsError,
    _boolean,
    _choice,
    _hash,
    _integer,
    _object,
    _rows,
    _text,
    _unique,
    _uuid,
)
from potbot.desktop_settings_state import RevocationJournal
from potbot.domain.live_controls import (
    EDITABLE,
    SCHEMA,
    configuration_review,
    node_review,
    overrides,
)
from potbot.domain.serialization import content_hash


def project_live(resource: str, raw: Any, workspace: str, principal: str) -> dict[str, Any]:
    try:
        value = _object(raw)
        if value["schema"] != SCHEMA:
            raise ValueError
        if _uuid(value["workspaceId"]) != workspace or _uuid(value["principalId"]) != principal:
            raise NativeSettingsError("scope")
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "workspaceId": workspace,
            "principalId": principal,
        }
        if resource in {"configuration", "configuration_saved"}:
            accepted = overrides(value["values"])
            sha256 = _hash(value["sha256"])
            if sha256 != content_hash(accepted) or value["apply"] != "next_admission":
                raise ValueError
            result.update(
                revision=_integer(value["revision"]),
                values=accepted,
                sha256=sha256,
                requestId=_uuid(value["requestId"]) if value["requestId"] else None,
                apply="next_admission",
            )
            if resource == "configuration":
                if value["scope"] != "workspace" or value["editableFields"] != list(EDITABLE):
                    raise ValueError
                effective = overrides(value["effective"])
                if set(effective) != set(EDITABLE):
                    raise ValueError
                result.update(
                    scope="workspace", effective=effective, canEdit=_boolean(value["canEdit"])
                )
            elif result["revision"] < 1 or result["requestId"] is None:
                raise ValueError
        elif resource == "nodes":
            if value["scope"] != "potbot_worker_service":
                raise ValueError
            nodes = []
            for row in _rows(value["nodes"]):
                managed, fresh, can_manage = (
                    _boolean(row[key]) for key in ("managed", "operatorFresh", "canManage")
                )
                phase = _choice(
                    row["phase"],
                    {
                        "unmanaged",
                        "starting",
                        "ready",
                        "draining",
                        "drained",
                        "restarting",
                        "indeterminate",
                        "stopped",
                    },
                )
                revision = _integer(row["revision"], 1) if managed else None
                incarnation = _uuid(row["incarnation"]) if managed else None
                if can_manage and not (managed and fresh and phase in {"ready", "drained"}):
                    raise ValueError
                if not managed and (
                    fresh
                    or phase != "unmanaged"
                    or row["revision"] is not None
                    or row["incarnation"] is not None
                    or row["unsettled"] is not None
                ):
                    raise ValueError
                nodes.append(
                    {
                        "id": _uuid(row["id"]),
                        "label": _text(row["label"], 253),
                        "generation": _integer(row["generation"], 1),
                        "managed": managed,
                        "revision": revision,
                        "incarnation": incarnation,
                        "phase": phase,
                        "desired": _choice(row["desired"], {"active", "drain", "restart"})
                        if managed
                        else None,
                        "operatorFresh": fresh,
                        "canManage": can_manage,
                        "unsettled": _integer(row["unsettled"]) if managed else None,
                    }
                )
            requests = []
            for row in _rows(value["requests"]):
                review = node_review(row["review"])
                if any(
                    row[name] != review[name]
                    for name in (
                        "requestId",
                        "nodeId",
                        "action",
                        "expectedRevision",
                        "expectedIncarnation",
                    )
                ):
                    raise ValueError
                state = _choice(
                    row["state"], {"requested", "accepted", "applied", "rejected", "indeterminate"}
                )
                incarnation = (
                    _uuid(row["observedIncarnation"]) if row["observedIncarnation"] else None
                )
                revision = (
                    _integer(row["observedRevision"], 1)
                    if row["observedRevision"] is not None
                    else None
                )
                if state == "applied" and (
                    revision is None
                    or revision <= review["expectedRevision"]
                    or incarnation is None
                    or review["action"] == "restart"
                    and incarnation == review["expectedIncarnation"]
                ):
                    raise ValueError
                requests.append(
                    {
                        **{key: review[key] for key in ("requestId", "nodeId", "action")},
                        "review": review,
                        "state": state,
                        "reason": _text(row["reason"], 100),
                        "observedIncarnation": incarnation,
                        "observedRevision": revision,
                    }
                )
            result.update(
                nodes=_unique(nodes),
                requests=_unique(requests, "requestId"),
                recordsTruncated=_boolean(value["recordsTruncated"]),
            )
        elif resource == "node_requested":
            if value["state"] != "requested":
                raise ValueError
            result.update(review=node_review(value["review"]), state="requested")
        else:
            raise ValueError
        return result
    except NativeSettingsError:
        raise
    except Exception:
        raise NativeSettingsError("invalid_response") from None


class ConfigurationDraft:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.accept(snapshot)

    def accept(self, snapshot: dict[str, Any]) -> None:
        self.base = copy.deepcopy(snapshot)
        self.draft = copy.deepcopy(snapshot["values"])
        self.latest: dict[str, Any] | None = None

    @property
    def changed(self) -> list[str]:
        absent = object()
        return sorted(
            name
            for name in self.base["values"].keys() | self.draft.keys()
            if self.base["values"].get(name, absent) != self.draft.get(name, absent)
        )

    def observe(self, snapshot: dict[str, Any]) -> None:
        if self.changed and (snapshot["revision"], snapshot["sha256"]) != (
            self.base["revision"],
            self.base["sha256"],
        ):
            self.latest = copy.deepcopy(snapshot)
        elif not self.changed:
            self.accept(snapshot)

    def rebase(self) -> list[str]:
        if self.latest is None:
            return []
        edited, wanted, absent = self.changed, self.draft, object()
        conflicts = [
            name
            for name in edited
            if self.base["values"].get(name, absent) != self.latest["values"].get(name, absent)
            and wanted.get(name, absent) != self.latest["values"].get(name, absent)
        ]
        latest = self.latest
        self.accept(latest)
        for name in edited:
            if name in wanted:
                self.draft[name] = wanted[name]
            else:
                self.draft.pop(name, None)
        return conflicts


def create_live_dialog(
    parent: Any,
    client: NativeSettingsClient,
    principal: str,
    mode: str,
    *,
    journal: RevocationJournal | None = None,
) -> Any:
    from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QLabel,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
        QSpinBox,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    if mode not in {"configuration", "nodes"}:
        raise NativeSettingsError("configuration")
    if journal is None:
        from potbot.dependencies import project_root

        journal = RevocationJournal(project_root() / ".potbot" / "desktop" / "live")

    class SignalBox(QObject):  # type: ignore[misc, unused-ignore]
        done = Signal(object)

    class Work(QRunnable):  # type: ignore[misc, unused-ignore]
        def __init__(self, identity: int, kind: str, callback: Callable[[], Any]) -> None:
            super().__init__()
            self.identity, self.kind, self.callback = identity, kind, callback
            self.signal = SignalBox()

        def run(self) -> None:
            result, error = None, None
            try:
                result = self.callback()
            except NativeSettingsError as failure:
                error = failure.code
            except Exception:
                error = "unavailable"
            self.signal.done.emit((self.identity, self.kind, result, error))

    class Review(QDialog):  # type: ignore[misc, unused-ignore]
        posting = False

        def reject(self) -> None:
            if not self.posting:
                super().reject()

    class LiveDialog(QDialog):  # type: ignore[misc, unused-ignore]
        def __init__(self) -> None:
            super().__init__(parent)
            self.setObjectName("liveControlsDialog")
            self.setWindowTitle(
                "Live workspace configuration"
                if mode == "configuration"
                else "Node worker controls"
            )
            self.setWindowModality(Qt.WindowModality.WindowModal)
            self.resize(1040, 780)
            self.client, self.principal, self.mode, self.journal = client, principal, mode, journal
            self.snapshot: dict[str, Any] | None = None
            self.state: ConfigurationDraft | None = None
            self._busy = self._closed = self._rendering = self._unknown = False
            self._recovered = False
            self._serial = 0
            self._jobs: dict[int, Any] = {}
            self._retained: dict[str, Any] | None = None
            self.review_dialog: Any = None
            self.pool = QThreadPool(self)
            self.pool.setMaxThreadCount(1)
            layout = QVBoxLayout(self)
            heading = self.label(self.windowTitle())
            heading.setObjectName("settingsHeading")
            layout.addWidget(heading)
            layout.addWidget(
                self.label(
                    f"Workspace: {client.connection.name} · {client.connection.workspace_id}\nPrincipal: {principal}"
                )
            )
            self.notice = self.label("Loading scoped live controls…")
            self.notice.setObjectName("liveNotice")
            self.operation = self.label("")
            layout.addWidget(self.notice)
            layout.addWidget(self.operation)
            self.fields: dict[str, tuple[Any, Any]] = {}
            if mode == "configuration":
                layout.addWidget(
                    self.label(
                        "Workspace overrides apply to new task admissions. Existing tasks retain their recorded configuration. Uncheck an override to inherit its value."
                    )
                )
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                content = QWidget()
                form = QFormLayout(content)
                for name in EDITABLE:
                    title = name.replace("_", " ").capitalize()
                    enabled = QCheckBox("Override " + title)
                    enabled.setObjectName("override_" + name)
                    editor: Any
                    if name in {"physical_inference_concurrency", "max_packet_tokens"}:
                        editor = QSpinBox()
                        editor.setRange(1, 1_000_000)
                        editor.valueChanged.connect(self.changed)
                    else:
                        editor = QPlainTextEdit()
                        editor.setMaximumHeight(85)
                        editor.textChanged.connect(self.changed)
                    editor.setObjectName("live_" + name)
                    editor.setAccessibleName(title)
                    enabled.toggled.connect(self.changed)
                    self.fields[name] = enabled, editor
                    form.addRow(enabled, editor)
                scroll.setWidget(content)
                layout.addWidget(scroll, 1)
                layout.addWidget(
                    self.label(
                        "Model/tool selections retain their existing qualification and task grants. Source access and credential references remain with their native security owners."
                    )
                )
            else:
                layout.addWidget(
                    self.label(
                        "These actions control the node's Potbot worker service. Drain fences new jobs and waits for jobs and resource holds. Restart replaces that worker after drain."
                    )
                )
                self.node = QComboBox()
                self.node.setObjectName("liveNode")
                self.node.setAccessibleName("Node worker service to manage")
                self.node.currentIndexChanged.connect(self.render_node)
                layout.addWidget(self.node)
                self.details = QPlainTextEdit()
                self.details.setReadOnly(True)
                self.details.setAccessibleName("Protected native node state")
                layout.addWidget(self.details, 1)
                self.requests = QTreeWidget()
                self.requests.setObjectName("liveRequests")
                self.requests.setHeaderLabels(
                    ["Request", "Node", "Action", "Native state", "Reason"]
                )
                self.requests.setRootIsDecorated(False)
                self.requests.itemSelectionChanged.connect(self.controls)
                layout.addWidget(self.requests, 1)
            buttons = QHBoxLayout()
            self.refresh = QPushButton("Refresh")
            self.refresh.setObjectName("liveRefresh")
            self.refresh.clicked.connect(self.load)
            buttons.addWidget(self.refresh)
            self.save = QPushButton("Review configuration change")
            self.save.setObjectName("liveSave")
            self.save.clicked.connect(lambda: self.prepare("configuration"))
            self.rebase = QPushButton("Rebase my draft for review")
            self.rebase.setObjectName("liveRebase")
            self.rebase.clicked.connect(self.rebase_draft)
            self.use_latest = QPushButton("Use latest saved values")
            self.use_latest.clicked.connect(self.use_saved)
            self.drain = QPushButton("Review drain")
            self.drain.setObjectName("liveDrain")
            self.drain.clicked.connect(lambda: self.prepare("drain"))
            self.restart = QPushButton("Review restart and resume")
            self.restart.setObjectName("liveRestart")
            self.restart.clicked.connect(lambda: self.prepare("restart"))
            self.retry = QPushButton("Review original request")
            self.retry.setObjectName("liveRetry")
            self.retry.clicked.connect(self.retry_review)
            for button in (
                self.save,
                self.rebase,
                self.use_latest,
                self.drain,
                self.restart,
                self.retry,
            ):
                buttons.addWidget(button)
            close = QPushButton("Close")
            close.clicked.connect(self.reject)
            buttons.addWidget(close)
            layout.addLayout(buttons)
            self.finished.connect(self.closed)
            self.timer = QTimer(self)
            self.timer.setInterval(1500)
            self.timer.timeout.connect(self.poll)
            self.timer.start()
            self.controls()
            QTimer.singleShot(0, self.load)

        @staticmethod
        def label(text: str) -> Any:
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            return label

        def start(self, kind: str, callback: Callable[[], Any]) -> None:
            self._serial += 1
            self._busy = True
            work = Work(self._serial, kind, callback)
            self._jobs[self._serial] = work
            work.signal.done.connect(self.finished_work)
            self.controls()
            self.pool.start(work)

        def load(self) -> None:
            if self._busy or self._closed:
                return
            self.start("load", lambda: self.client.read_live(self.mode, self.principal))

        def poll(self) -> None:
            if (
                self.mode == "nodes"
                and self.snapshot
                and not self.review_dialog
                and any(
                    row["state"] in {"requested", "accepted"} for row in self.snapshot["requests"]
                )
            ):
                self.load()

        @Slot(object)  # type: ignore[untyped-decorator, unused-ignore]
        def finished_work(self, payload: Any) -> None:
            identity, kind, result, error = payload
            self._jobs.pop(identity, None)
            if self._closed:
                return
            self._busy = False
            if self.review_dialog:
                self.review_dialog.posting = False
            if error:
                self.notice.setText(str(NativeSettingsError(error)))
                if kind == "write":
                    self._unknown = True
                    self._recovered = False
                    self.operation.setText(
                        "Outcome unconfirmed. Refresh before reviewing the original retained request."
                    )
                    if self.review_dialog:
                        self.review_notice.setText(
                            "Outcome unconfirmed. Cancel to refresh; the original review is retained."
                        )
                if error == "scope":
                    self.snapshot = None
                    self.state = None
                    self._retained = None
                    for _enabled, editor in self.fields.values():
                        editor.clear()
                    if self.mode == "nodes":
                        self.node.clear()
                        self.details.clear()
                        self.requests.clear()
                self.controls()
                return
            if kind == "load":
                self.snapshot = result
                self._recovered = True
                if self.mode == "configuration":
                    if self.state:
                        self.state.observe(result)
                    else:
                        self.state = ConfigurationDraft(result)
                    if self._retained and result["requestId"] == self._retained["requestId"]:
                        self.state.accept(result)
                        self._retained = None
                        self._unknown = False
                        self.operation.setText(
                            "The original configuration change is confirmed by live readback."
                        )
                    self.render_configuration()
                else:
                    selected = self.node.currentData()
                    self._rendering = True
                    self.node.clear()
                    for row in result["nodes"]:
                        self.node.addItem(row["label"] + " · " + row["id"], row["id"])
                    if self.node.findData(selected) >= 0:
                        self.node.setCurrentIndex(self.node.findData(selected))
                    self._rendering = False
                    self.requests.clear()
                    for row in result["requests"]:
                        item = QTreeWidgetItem(
                            [
                                row[key]
                                for key in ("requestId", "nodeId", "action", "state", "reason")
                            ]
                        )
                        item.setData(0, Qt.ItemDataRole.UserRole, row)
                        self.requests.addTopLevelItem(item)
                    self.render_node()
                self.notice.setText(
                    "Live workspace revision " + str(result["revision"])
                    if self.mode == "configuration"
                    else "Native worker state · "
                    + ("bounded subset" if result["recordsTruncated"] else "current scoped records")
                )
            elif kind == "prepare":
                self._retained = result
                self.show_review()
            elif kind == "write":
                if "conflict" in result:
                    self.snapshot = result["conflict"]
                    assert self.state is not None
                    self.state.observe(self.snapshot)
                    self.operation.setText(
                        "Configuration changed in another session. Your draft is retained; rebase for review."
                    )
                    if self.review_dialog:
                        self.review_dialog.reject()
                    self.render_configuration()
                else:
                    if self.review_dialog:
                        self.review_dialog.accept()
                    if self.mode == "configuration":
                        assert self.state is not None and self.snapshot is not None
                        self.state.accept({**self.snapshot, **result})
                        self._retained = None
                        self._unknown = False
                        self.operation.setText(
                            "Configuration saved at revision "
                            + str(result["revision"])
                            + "; applies to new admissions."
                        )
                    else:
                        self._unknown = False
                        self.operation.setText(
                            "Request retained by the backend. Native completion is shown independently below."
                        )
                    self.load()
            self.controls()

        def render_configuration(self) -> None:
            if not self.state or not self.snapshot:
                return
            self._rendering = True
            for name, (enabled, editor) in self.fields.items():
                enabled.setChecked(name in self.state.draft)
                value = self.state.draft.get(name, self.snapshot["effective"][name])
                if isinstance(editor, QSpinBox):
                    editor.setValue(value)
                else:
                    editor.setPlainText(json.dumps(value, indent=2))
            self._rendering = False

        def collect(self) -> dict[str, Any]:
            values = {}
            for name, (enabled, editor) in self.fields.items():
                if enabled.isChecked():
                    if isinstance(editor, QSpinBox):
                        values[name] = int(editor.value())
                    else:
                        raw = editor.toPlainText()
                        if len(raw) > 16_384:
                            raise ValueError
                        values[name] = json.loads(raw)
            return overrides(values)

        def changed(self, *_args: Any) -> None:
            if not self._rendering and self.state:
                try:
                    self.state.draft = self.collect()
                except Exception:
                    self.notice.setText("Check the configuration JSON and declared value types.")
            self.controls()

        def rebase_draft(self) -> None:
            if self.state:
                try:
                    self.state.draft = self.collect()
                except Exception:
                    self.notice.setText("Correct the draft values before rebasing.")
                    return
                conflicts = self.state.rebase()
                self._retained = None
                self._unknown = False
                self.operation.setText(
                    "Rebased for review. "
                    + (
                        "Your choices replace changes to: " + ", ".join(conflicts)
                        if conflicts
                        else "Other saved choices were preserved."
                    )
                )
                self.render_configuration()
                self.controls()

        def use_saved(self) -> None:
            if self.state and self.state.latest:
                self.state.accept(self.state.latest)
                self._unknown = False
                self._retained = None
                self.render_configuration()
                self.controls()

        def selected_node(self) -> dict[str, Any] | None:
            if not self.snapshot or self.mode != "nodes":
                return None
            return next(
                (row for row in self.snapshot["nodes"] if row["id"] == self.node.currentData()),
                None,
            )

        def render_node(self, *_args: Any) -> None:
            if self._rendering:
                return
            row = self.selected_node()
            self.details.setPlainText(
                json.dumps(row, indent=2) if row else "No registered node is visible."
            )
            self.controls()

        def prepare(self, action: str) -> None:
            if self._busy or not self.snapshot:
                return
            if action == "configuration":
                if (
                    not self.state
                    or not self.snapshot["canEdit"]
                    or self.state.latest
                    or self._unknown
                ):
                    return
                try:
                    self.state.draft = self.collect()
                except Exception:
                    self.notice.setText("Correct the draft values before review.")
                    return
                review = {
                    "requestId": str(uuid4()),
                    "expectedRevision": self.state.base["revision"],
                    "expectedSha256": self.state.base["sha256"],
                    "values": self.state.draft,
                    "confirm": True,
                }
            else:
                node = self.selected_node()
                if not node or not node["canManage"]:
                    return
                review = {
                    "requestId": str(uuid4()),
                    "nodeId": node["id"],
                    "expectedGeneration": node["generation"],
                    "expectedRevision": node["revision"],
                    "expectedIncarnation": node["incarnation"],
                    "action": action,
                    "confirm": True,
                }
            self.retain(review)

        def retain(self, review: dict[str, Any], specific: bool = False) -> None:
            assert self.journal is not None
            retained_journal = self.journal
            validator = configuration_review if self.mode == "configuration" else node_review
            self.start(
                "prepare",
                lambda: retained_journal.store(
                    client.connection.workspace_id,
                    self.principal,
                    review,
                    validator=validator,
                    maximum=32_768,
                    specific=specific,
                    maximum_records=1000,
                ),
            )

        def retry_review(self) -> None:
            selected = self.requests.currentItem() if self.mode == "nodes" else None
            if selected is not None:
                row = selected.data(0, Qt.ItemDataRole.UserRole)
                if row["state"] == "requested":
                    self.retain(row["review"], specific=True)
                    return
            if self._retained and self.snapshot:
                self.show_review()

        def show_review(self) -> None:
            dialog = Review(self)
            self.review_dialog = dialog
            dialog.setObjectName("liveReviewDialog")
            dialog.setWindowTitle(
                "Review exact configuration change"
                if self.mode == "configuration"
                else "Review exact node-service action"
            )
            dialog.resize(700, 500)
            layout = QVBoxLayout(dialog)
            layout.addWidget(
                self.label(
                    "Workspace: " + client.connection.workspace_id + "\nOwner: " + self.principal
                )
            )
            text = QPlainTextEdit(json.dumps(self._retained, indent=2))
            text.setReadOnly(True)
            text.setAccessibleName("Exact retained live-control review")
            layout.addWidget(text, 1)
            self.review_notice = self.label(
                "Submission uses the existing owner/setup authority. This retained review is not execution evidence."
            )
            layout.addWidget(self.review_notice)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            self.review_submit = buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.review_submit.setText("Submit this exact change")
            self.review_submit.setObjectName("liveReviewSubmit")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
            buttons.rejected.connect(dialog.reject)
            self.review_submit.clicked.connect(self.send)
            dialog.finished.connect(lambda _result: setattr(self, "review_dialog", None))
            layout.addWidget(buttons)
            dialog.open()

        def send(self) -> None:
            if self._busy or not self._retained or not self.review_dialog:
                return
            review = copy.deepcopy(self._retained)
            self.review_dialog.posting = True

            def work() -> Any:
                try:
                    return client.write_live(self.mode, self.principal, review)
                except NativeSettingsError as error:
                    if error.code != "conflict" or self.mode != "configuration":
                        raise
                    return {"conflict": client.read_live(self.mode, self.principal)}

            self.start("write", work)

        def controls(self) -> None:
            self.refresh.setEnabled(not self._busy)
            configuration = self.mode == "configuration"
            self.save.setVisible(configuration)
            self.rebase.setVisible(bool(configuration and self.state and self.state.latest))
            self.use_latest.setVisible(bool(configuration and self.state and self.state.latest))
            valid = False
            if configuration:
                try:
                    self.collect()
                    valid = True
                except Exception:
                    pass
            editable = bool(configuration and self.snapshot and self.snapshot["canEdit"])
            for enabled, editor in self.fields.values():
                enabled.setEnabled(editable and not self._busy)
                editor.setEnabled(editable and not self._busy and enabled.isChecked())
            self.save.setEnabled(
                bool(
                    editable
                    and valid
                    and self.state
                    and self.state.changed
                    and not self.state.latest
                    and not self._busy
                    and not self._unknown
                )
            )
            self.rebase.setEnabled(not self._busy)
            self.use_latest.setEnabled(not self._busy)
            self.drain.setVisible(not configuration)
            self.restart.setVisible(not configuration)
            node = self.selected_node() if not configuration else None
            can_manage = bool(node and node["canManage"] and not self._busy)
            self.drain.setEnabled(can_manage)
            self.restart.setEnabled(can_manage)
            retry = self._unknown and self._retained is not None and self.snapshot is not None
            if not configuration:
                self.node.setEnabled(not self._busy)
                selected = self.requests.currentItem()
                retry = retry or bool(
                    selected and selected.data(0, Qt.ItemDataRole.UserRole)["state"] == "requested"
                )
            self.retry.setVisible(bool(retry))
            self.retry.setEnabled(not self._busy)
            if self.review_dialog:
                self.review_submit.setEnabled(
                    not self._busy and (not self._unknown or self._recovered)
                )

        def reject(self) -> None:
            if self.review_dialog and self.review_dialog.posting:
                return
            super().reject()

        def closed(self, *_args: Any) -> None:
            self._closed = True
            self.timer.stop()

        def invalidate_scope(self, *_args: Any) -> None:
            self._closed = True
            self.timer.stop()
            if self.review_dialog:
                self.review_dialog.posting = False
                self.review_dialog.reject()
            super().reject()

    return LiveDialog()
