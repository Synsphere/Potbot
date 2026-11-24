"""Real Qt settings, inventory and swarm panels over the existing HTTP APIs.

Qt is imported only when the desktop asks for this widget. Network/vault work
runs in a bounded thread pool; selection generations discard superseded views.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from potbot.desktop_settings_client import (
    NativeSettingsClient,
    NativeSettingsError,
    load_native_clients,
)
from potbot.desktop_settings_state import PreferenceDraft, RevocationJournal
from potbot.domain.settings_catalogue import CATEGORIES


def native_stylesheet(theme: str, system_dark: bool = True) -> str:
    dark = theme == "dark" or theme == "system" and system_dark
    background, surface, ink, muted, line, accent = (
        ("#08131d", "#102638", "#e9f3fa", "#b2c8d6", "#456276", "#87ddf2")
        if dark
        else ("#f3f7fa", "#ffffff", "#173044", "#456174", "#9aafbd", "#075d79")
    )
    alternate = "#163044" if dark else "#e8f0f6"
    return f"""
      QMainWindow, QWidget {{ background: {background}; color: {ink}; }}
      QLabel {{ background: transparent; }}
      QLineEdit, QComboBox, QTreeWidget, QPlainTextEdit {{ background: {surface}; color: {ink};
          border: 1px solid {line}; border-radius: 4px; padding: 5px; }}
      QHeaderView::section, QTabBar::tab {{ background: {surface}; color: {ink}; padding: 7px; }}
      QTreeWidget {{ alternate-background-color: {alternate}; }}
      QTreeWidget::item:selected, QListWidget::item:selected {{ background: #235a70; color: #ffffff; }}
      QPushButton {{ background: {surface}; color: {ink}; border: 1px solid {line};
          border-radius: 4px; padding: 8px 12px; min-height: 20px; }}
      QPushButton:focus, QComboBox:focus, QLineEdit:focus, QTreeWidget:focus {{ border: 2px solid {accent}; }}
      QPushButton:disabled {{ color: {muted}; border-color: {line}; }}
      #startupBanner, QDockWidget::title {{ background: {surface}; color: {accent}; padding: 8px; }}
      #workspaceNav {{ border: 0; padding: 10px; }}
      #workspaceNav::item {{ padding: 11px 12px; margin: 2px; }}
      #workspaceCanvas {{ padding: 26px; }}
      #eyebrow {{ color: {accent}; font-size: 11px; font-weight: 700; }}
      #workspaceTitle, #settingsHeading {{ font-size: 24px; font-weight: 700; }}
      #workspaceCopy, #emptyWorkspace, #settingsHelp {{ color: {muted}; }}
      #settingsNotice, #settingsOperation {{ color: {accent}; padding: 5px; }}
      QCheckBox {{ spacing: 10px; padding: 6px; }}
    """


def create_settings_widget(
    parent: Any = None,
    *,
    client_loader: Callable[[], list[NativeSettingsClient]] = load_native_clients,
    journal: RevocationJournal | None = None,
) -> Any:
    from PySide6.QtCore import (
        QObject,
        QRunnable,
        QSignalBlocker,
        Qt,
        QThreadPool,
        QTimer,
        Signal,
        Slot,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QPushButton,
        QStackedWidget,
        QTabWidget,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    class Completion(QObject):  # type: ignore[misc, unused-ignore]
        done = Signal(object)

    class Task(QRunnable):  # type: ignore[misc, unused-ignore]
        def __init__(
            self,
            epoch: int,
            identity: int,
            kind: str,
            work: Callable[[], Any],
            cancel: threading.Event,
        ) -> None:
            super().__init__()
            self.completion = Completion()
            self.epoch, self.identity, self.kind, self.work, self.cancel = (
                epoch,
                identity,
                kind,
                work,
                cancel,
            )

        def run(self) -> None:
            result, error = None, None
            try:
                if self.cancel.is_set():
                    raise NativeSettingsError("cancelled")
                result = self.work()
            except NativeSettingsError as failure:
                error = failure.code
            except Exception:
                error = "unavailable"
            self.completion.done.emit((self.epoch, self.identity, self.kind, result, error))

    class ReviewDialog(QDialog):  # type: ignore[misc, unused-ignore]
        posting = False

        def reject(self) -> None:
            if not self.posting:
                super().reject()

        def closeEvent(self, event: Any) -> None:
            if self.posting:
                event.ignore()
            else:
                super().closeEvent(event)

    class SettingsWidget(QWidget):  # type: ignore[misc, unused-ignore]
        preferencesChanged = Signal(object)
        mutationBusy = Signal(bool)

        def __init__(self) -> None:
            super().__init__(parent)
            self.setObjectName("nativeSettings")
            self._journal = journal or RevocationJournal()
            self._clients: list[NativeSettingsClient] = []
            self._home = ""
            self._epoch = 0
            self._serial = 0
            self._jobs: dict[int, Task] = {}
            self._cancel = threading.Event()
            self._busy = False
            self._mutating = False
            self._closed = False
            self._principal = ""
            self._catalogue: dict[str, Any] | None = None
            self._document: dict[str, Any] | None = None
            self._drafts: dict[tuple[str, str], PreferenceDraft] = {}
            self._review: dict[str, Any] | None = None
            self.review_dialog: Any = None
            self._unknown_save = False
            self._section = "overview"
            self.pool = QThreadPool(self)
            self.pool.setMaxThreadCount(2)

            layout = QVBoxLayout(self)
            self.heading = self.label("Settings", "settingsHeading")
            layout.addWidget(self.heading)
            self.home_label = self.label("Connecting to the native workspace…")
            layout.addWidget(self.home_label)
            toolbar = QHBoxLayout()
            self.target = QComboBox()
            self.target.setObjectName("workspaceTarget")
            self.target.setAccessibleName("Workspace to inspect")
            self.category = QComboBox()
            self.category.setObjectName("settingsCategory")
            self.category.setAccessibleName("Settings category")
            self.category.addItem("All settings", "overview")
            for identity, label, _path in CATEGORIES:
                self.category.addItem(label, identity)
            self.search = QLineEdit()
            self.search.setObjectName("settingsSearch")
            self.search.setAccessibleName("Search settings and inventory")
            self.search.setPlaceholderText("Search this view")
            self.refresh = QPushButton("Refresh")
            self.refresh.setObjectName("settingsRefresh")
            toolbar.addWidget(self.target, 2)
            toolbar.addWidget(self.category, 2)
            toolbar.addWidget(self.search, 2)
            toolbar.addWidget(self.refresh)
            layout.addLayout(toolbar)
            self.notice = self.label("Loading native workspace connections…", "settingsNotice")
            self.operation = self.label("", "settingsOperation")
            layout.addWidget(self.notice)
            layout.addWidget(self.operation)
            self.pages = QStackedWidget()
            layout.addWidget(self.pages, 1)

            overview = QWidget()
            overview_layout = QVBoxLayout(overview)
            overview_layout.addWidget(
                self.label(
                    "Installation defaults and the latest recorded run snapshot. Live layer editing is not available.",
                    "settingsHelp",
                )
            )
            self.values = self.tree(
                ["Setting", "Installation default", "Recorded snapshot", "Apply", "Owner"],
                "configurationTree",
            )
            self.features = self.tree(
                ["Feature", "Configuration area", "Description"], "featureTree"
            )
            overview_layout.addWidget(self.values, 2)
            overview_layout.addWidget(self.features, 1)
            self.pages.addWidget(overview)

            appearance = QWidget()
            form = QFormLayout(appearance)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            self.preference_status = self.label("No saved preferences loaded.")
            form.addRow(self.preference_status)
            self.preference_controls: dict[str, Any] = {}
            control: Any
            for key, title, choices in (
                ("theme", "Theme", [("System", "system"), ("Dark", "dark"), ("Light", "light")]),
                (
                    "graphDisplay",
                    "Graph display",
                    [("3D graph", "3d"), ("Accessible list", "list")],
                ),
                (
                    "xpraMaxFps",
                    "Xpra client frame limit",
                    [(str(fps) + " FPS", fps) for fps in (10, 15, 24, 30, 60)],
                ),
            ):
                control = QComboBox()
                control.setObjectName("preference_" + key)
                control.setAccessibleName(title)
                for title_value, value in choices:
                    control.addItem(title_value, value)
                control.currentIndexChanged.connect(
                    lambda _index, name=key, box=control: self.edit_preference(
                        name, box.currentData()
                    )
                )
                self.preference_controls[key] = control
                form.addRow(title, control)
            for key, title in (
                ("reducedMotion", "Reduce motion"),
                ("showGraphDiagnostics", "Graph diagnostics"),
                ("orbAutoHide", "Automatically hide the orb"),
                ("downloadNotifications", "Download notifications"),
            ):
                control = QCheckBox(title)
                control.setObjectName("preference_" + key)
                control.toggled.connect(
                    lambda selected, name=key: self.edit_preference(name, selected)
                )
                self.preference_controls[key] = control
                form.addRow(control)
            self.save = QPushButton("Save preferences")
            self.save.setObjectName("preferencesSave")
            self.rebase = QPushButton("Rebase my draft for review")
            self.rebase.setObjectName("preferencesRebase")
            self.use_saved = QPushButton("Use latest saved values")
            self.use_saved.setObjectName("preferencesUseSaved")
            self.conflict_details = QPlainTextEdit()
            self.conflict_details.setReadOnly(True)
            self.conflict_details.setAccessibleName("Concurrent saved preferences")
            form.addRow(self.save)
            form.addRow(self.rebase, self.use_saved)
            form.addRow(self.conflict_details)
            form.addRow(
                self.label(
                    "Theme applies to this native Home workspace after saving. Graph, orb and Xpra choices are shared with views that implement them. Dock layout remains client-local.",
                    "settingsHelp",
                )
            )
            self.pages.addWidget(appearance)

            inventory = QWidget()
            inventory_layout = QVBoxLayout(inventory)
            self.inventory_status = self.label("No inventory loaded.")
            inventory_layout.addWidget(self.inventory_status)
            self.inventory_filter = QComboBox()
            self.inventory_filter.setObjectName("inventoryFilter")
            self.inventory_filter.setAccessibleName("Inventory filter")
            for title, value in (
                ("All records", "all"),
                ("Observed locally", "observed"),
                ("Registry records", "records"),
                ("Needs checks", "attention"),
            ):
                self.inventory_filter.addItem(title, value)
            inventory_layout.addWidget(self.inventory_filter)
            self.inventory = self.tree(
                [
                    "Name",
                    "Version",
                    "Origin",
                    "Presence",
                    "Qualification",
                    "Recorded health",
                    "Task permission",
                    "Bytes",
                ],
                "inventoryTree",
            )
            inventory_layout.addWidget(self.inventory, 3)
            self.inventory_details = QPlainTextEdit()
            self.inventory_details.setReadOnly(True)
            self.inventory_details.setAccessibleName("Inventory observation details")
            inventory_layout.addWidget(self.inventory_details, 1)
            inventory_layout.addWidget(
                self.label(
                    "Refresh uses the existing backend inventory API. It loads no models and executes no discovered tools. Registration and metadata do not establish runtime readiness or task permission.",
                    "settingsHelp",
                )
            )
            self.pages.addWidget(inventory)

            swarm = QWidget()
            swarm_layout = QVBoxLayout(swarm)
            self.swarm_status = self.label("No swarm records loaded.")
            swarm_layout.addWidget(self.swarm_status)
            self.swarm_tabs = QTabWidget()
            self.swarm_tabs.setObjectName("swarmTabs")
            self.nodes = self.tree(
                ["Node", "State", "Generation", "SSH fingerprint", "Registered"], "nodeTree"
            )
            self.deployments = self.tree(
                ["Workspace", "Node", "Revision", "Execution mode", "Qualification", "Platform"],
                "deploymentTree",
            )
            self.grants = self.tree(
                ["Source workspace", "State", "Permissions", "Revision", "Expires"], "grantTree"
            )
            self.requests = self.tree(
                ["Request", "Grant", "Reviewed revision", "State", "Recorded"], "requestTree"
            )
            for title, tree in (
                ("Nodes", self.nodes),
                ("Deployments", self.deployments),
                ("Control grants", self.grants),
                ("Requests", self.requests),
            ):
                self.swarm_tabs.addTab(tree, title)
            swarm_layout.addWidget(self.swarm_tabs, 3)
            self.revoke = QPushButton("Review revocation request")
            self.revoke.setObjectName("requestRevocation")
            self.copy_handoff = QPushButton("Copy native operator command")
            self.copy_handoff.setObjectName("copyNativeHandoff")
            buttons = QHBoxLayout()
            buttons.addWidget(self.revoke)
            buttons.addWidget(self.copy_handoff)
            buttons.addStretch()
            swarm_layout.addLayout(buttons)
            self.swarm_details = QPlainTextEdit()
            self.swarm_details.setReadOnly(True)
            self.swarm_details.setAccessibleName("Swarm selection and native operator handoff")
            swarm_layout.addWidget(self.swarm_details, 1)
            swarm_layout.addWidget(
                self.label(
                    "Registered nodes are not proven online or qualified. Capacity, drain, restart and other native node lifecycle actions are unavailable. A saved request is not a revoked grant or a stopped process.",
                    "settingsHelp",
                )
            )
            self.pages.addWidget(swarm)

            self.target.currentIndexChanged.connect(self._target_changed)
            self.category.currentIndexChanged.connect(
                lambda _index: self.open_section(self.category.currentData())
            )
            self.refresh.clicked.connect(self.reload)
            self.search.textChanged.connect(self._filter)
            self.inventory_filter.currentIndexChanged.connect(self._filter)
            self.inventory.itemSelectionChanged.connect(self._inventory_selected)
            for tree in (self.nodes, self.deployments, self.grants, self.requests):
                tree.itemSelectionChanged.connect(self._swarm_selected)
            self.swarm_tabs.currentChanged.connect(self._swarm_selected)
            self.save.clicked.connect(self._save_preferences)
            self.rebase.clicked.connect(self._rebase)
            self.use_saved.clicked.connect(self._use_saved)
            self.revoke.clicked.connect(self._prepare_revocation)
            self.copy_handoff.clicked.connect(self._copy_handoff)
            self._controls()
            QTimer.singleShot(0, self.reconnect)

        @staticmethod
        def label(text: str, name: str = "") -> Any:
            result = QLabel()
            result.setTextFormat(Qt.TextFormat.PlainText)
            result.setText(text)
            result.setWordWrap(True)
            result.setObjectName(name)
            return result

        @staticmethod
        def tree(headers: list[str], name: str) -> Any:
            result = QTreeWidget()
            result.setObjectName(name)
            result.setAccessibleName(name)
            result.setHeaderLabels(headers)
            result.setRootIsDecorated(False)
            result.setAlternatingRowColors(True)
            result.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
            result.setMinimumHeight(120)
            result.header().setMaximumSectionSize(460)
            result.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            result.header().setStretchLastSection(True)
            return result

        @staticmethod
        def add(tree: Any, cells: list[Any], data: dict[str, Any]) -> None:
            item = QTreeWidgetItem(
                [str(value) if value is not None else "Not observed" for value in cells]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, data)
            tree.addTopLevelItem(item)

        def _client(self) -> NativeSettingsClient:
            index = self.target.currentIndex()
            if not 0 <= index < len(self._clients):
                raise NativeSettingsError("configuration")
            return self._clients[index]

        def _draft(self) -> PreferenceDraft | None:
            if not self._clients or not self._principal:
                return None
            return self._drafts.get((self._client().connection.workspace_id, self._principal))

        def _invalidate(self) -> None:
            self._epoch += 1
            self._cancel.set()
            self._cancel = threading.Event()
            self._principal = ""
            self._catalogue = None
            self._document = None
            self._unknown_save = False
            for tree in (
                self.values,
                self.features,
                self.inventory,
                self.nodes,
                self.deployments,
                self.grants,
                self.requests,
            ):
                tree.clear()
            self.inventory_details.clear()
            self.swarm_details.clear()
            self.inventory_status.setText("No inventory loaded for this selection.")
            self.swarm_status.setText("No swarm records loaded for this selection.")
            self.preference_status.setText("No saved preferences loaded for this selection.")
            for control in self.preference_controls.values():
                with QSignalBlocker(control):
                    control.setChecked(False) if isinstance(
                        control, QCheckBox
                    ) else control.setCurrentIndex(0)
            self._review = None
            if self.review_dialog and not self.review_dialog.posting:
                self.review_dialog.reject()
            self._controls()

        def _start(self, kind: str, work: Callable[[], Any]) -> None:
            self._serial += 1
            self._busy = True
            self._mutating = kind in {"save", "revoke"}
            self.mutationBusy.emit(self._mutating)
            task = Task(self._epoch, self._serial, kind, work, self._cancel)
            self._jobs[self._serial] = task
            task.completion.done.connect(self._finished)
            self._controls()
            self.pool.start(task)

        def reconnect(self) -> None:
            if self._mutating or self._closed:
                return
            self._invalidate()
            self._clients = []
            with QSignalBlocker(self.target):
                self.target.clear()
            self.notice.setText("Loading native workspace connections…")
            self._start("connect", client_loader)

        def backend_available(self) -> None:
            if not self._clients and not self._busy:
                self.reconnect()

        def _target_changed(self, _index: int) -> None:
            if self._clients:
                self.operation.clear()
                self._load()

        def open_section(self, section: str) -> None:
            if self._mutating or section not in {"overview", *(row[0] for row in CATEGORIES)}:
                return
            self._section = section
            with QSignalBlocker(self.category):
                self.category.setCurrentIndex(self.category.findData(section))
            self.heading.setText(
                "Settings"
                if section == "overview"
                else next(row[1] for row in CATEGORIES if row[0] == section)
            )
            self.pages.setCurrentIndex(
                1
                if section == "appearance"
                else 2
                if section in {"models", "tools"}
                else 3
                if section == "swarm"
                else 0
            )
            if self._clients:
                self._load()

        def reload(self) -> None:
            if not self._mutating:
                self._load() if self._clients else self.reconnect()

        def _load(self) -> None:
            if self._closed:
                return
            client, section = self._client(), self._section
            self._invalidate()
            cancel = self._cancel
            self.notice.setText("Loading workspace-scoped records…")

            def work() -> Any:
                catalogue = client.read("catalogue")
                if cancel.is_set():
                    raise NativeSettingsError("cancelled")
                resource = (
                    section
                    if section in {"models", "tools", "swarm"}
                    else "preferences"
                    if section == "appearance"
                    else "effective"
                )
                document = client.read(resource, catalogue["principalId"])
                if cancel.is_set():
                    raise NativeSettingsError("cancelled")
                home_preferences = (
                    document
                    if section == "appearance"
                    else client.read("preferences", catalogue["principalId"])
                    if client.connection.workspace_id == self._home
                    else None
                )
                return catalogue, document, home_preferences

            self._start("load", work)

        @Slot(object)
        def _finished(self, payload: Any) -> None:
            epoch, identity, kind, data, error = payload
            self._jobs.pop(identity, None)
            if self._closed or epoch != self._epoch:
                return
            self._busy = self._mutating = False
            self.mutationBusy.emit(False)
            if self.review_dialog:
                self.review_dialog.posting = False
            if error:
                self.notice.setText(str(NativeSettingsError(error)))
                if kind == "save":
                    self._unknown_save = True
                    self.operation.setText(
                        "Save outcome not confirmed. Your draft is retained. Refresh before another save."
                    )
                if kind == "revoke" and self._review:
                    self.operation.setText(
                        "Request outcome not confirmed. The original review is retained: "
                        + self._review["requestId"]
                        + ". Refresh and review this same request before retrying."
                    )
                if self.review_dialog and self._review:
                    self.review_notice.setText(
                        self.operation.text() or str(NativeSettingsError(error))
                    )
                if error == "scope":
                    self._invalidate()
                    self._clients = []
                    with QSignalBlocker(self.target):
                        self.target.clear()
                self._controls()
                return
            if kind == "connect":
                self._clients = data
                self._home = data[0].connection.workspace_id
                with QSignalBlocker(self.target):
                    for client in data:
                        self.target.addItem(client.connection.name, client.connection.workspace_id)
                    self.target.setCurrentIndex(0)
                self.home_label.setText(
                    "Home: "
                    + data[0].connection.name
                    + " · inspection never changes the active Home workspace."
                )
                self._load()
                return
            if kind == "load":
                self._catalogue, self._document, home_preferences = data
                self._principal = self._catalogue["principalId"]
                if home_preferences is not None:
                    self._apply_home_theme(home_preferences["settings"])
                self.notice.setText(
                    "Viewing "
                    + self._client().connection.name
                    + " · "
                    + self._client().connection.workspace_id
                )
                self._render()
            elif kind == "save":
                state = self._draft()
                if state:
                    if "conflict" in data:
                        state.observe(data["conflict"])
                        self.operation.setText(
                            "Preferences changed in another session. Your draft is retained; rebase for review."
                        )
                    else:
                        state.accept(data)
                        self.operation.setText(
                            "Preferences saved and read back at revision "
                            + str(data["revision"])
                            + "."
                        )
                    self._render_preferences()
            elif kind == "prepare":
                self._review = data
                self._show_review()
            elif kind == "revoke":
                if self.review_dialog:
                    self.review_dialog.accept()
                self.operation.setText(
                    "Request saved. Native operator application is still required; acceptance is not revocation."
                )
                self._load()
                return
            self._controls()

        def _render(self) -> None:
            if not self._catalogue or self._document is None:
                return
            data = self._document
            if self._section == "appearance":
                key = (self._client().connection.workspace_id, self._principal)
                if key in self._drafts:
                    self._drafts[key].observe(data)
                else:
                    if len(self._drafts) >= 24:
                        self._drafts.pop(next(iter(self._drafts)))
                    self._drafts[key] = PreferenceDraft(data)
                self._render_preferences()
            elif self._section in {"models", "tools"}:
                local = data["local"]
                text = "Backend: " + local["nodeLabel"] + " · read " + data["observedAt"]
                if local["status"] == "not_configured":
                    text += "\nNative inventory roots are not configured. The operator must supply --inventory-profile to potbot serve. Registry records are not proof of installation."
                if local["status"] == "partial" or data["recordsTruncated"]:
                    text += "\nPartial/bounded inventory; unavailable locations: " + str(
                        local["unavailableRoots"]
                    )
                self.inventory_status.setText(text)
                for row in data["items"]:
                    self.add(
                        self.inventory,
                        [
                            row[key]
                            for key in (
                                "name",
                                "version",
                                "origin",
                                "presence",
                                "qualification",
                                "runtime",
                                "permission",
                                "bytes",
                            )
                        ],
                        row,
                    )
                if not data["items"]:
                    self.notice.setText(
                        self.notice.text()
                        + " · No matching authorised observations; unavailable roots do not establish an empty machine."
                    )
            elif self._section == "swarm":
                self.swarm_status.setText(
                    "Protected authority read "
                    + data["observedAt"]
                    + (" · bounded subset" if data["recordsTruncated"] else "")
                )
                for row in data["nodes"]:
                    self.add(
                        self.nodes,
                        [
                            row[key]
                            for key in (
                                "label",
                                "state",
                                "generation",
                                "sshFingerprint",
                                "registeredAt",
                            )
                        ],
                        row,
                    )
                for row in data["deployments"]:
                    self.add(
                        self.deployments,
                        [
                            row[key]
                            for key in (
                                "workspaceId",
                                "nodeId",
                                "revision",
                                "executionMode",
                                "qualification",
                                "platform",
                            )
                        ],
                        row,
                    )
                for row in data["grants"]:
                    self.add(
                        self.grants,
                        [
                            row["sourceWorkspaceId"],
                            row["state"],
                            ", ".join(row["permissions"]),
                            row["revision"],
                            row["expiresAt"],
                        ],
                        row,
                    )
                for row in data["requests"]:
                    self.add(
                        self.requests,
                        [
                            row[key]
                            for key in (
                                "requestId",
                                "grantId",
                                "expectedRevision",
                                "state",
                                "recordedAt",
                            )
                        ],
                        row,
                    )
            else:
                defaults = {row["id"]: row["value"] for row in data["defaults"]}
                snapshot = data["latestRecordedSnapshot"]
                recorded = (
                    {row["id"]: row["value"] for row in snapshot["values"]} if snapshot else {}
                )
                for row in self._catalogue["descriptors"]:
                    if not row["editable"] and (
                        self._section == "overview" or row["category"] == self._section
                    ):
                        self.add(
                            self.values,
                            [
                                row["label"],
                                defaults.get(row["id"], row["defaultValue"]),
                                recorded.get(row["id"], "Not recorded"),
                                row["apply"],
                                row["owner"],
                            ],
                            row,
                        )
                for row in self._catalogue["features"]:
                    if self._section == "overview" or row["category"] == self._section:
                        self.add(
                            self.features, [row["label"], row["category"], row["description"]], row
                        )
            self._filter()

        def _render_preferences(self) -> None:
            state = self._draft()
            if not state:
                return
            defaults = {"theme": "system", "graphDisplay": "3d", "xpraMaxFps": 30}
            for key, control in self.preference_controls.items():
                with QSignalBlocker(control):
                    value = state.draft.get(key, defaults.get(key, False))
                    control.setChecked(value) if isinstance(
                        control, QCheckBox
                    ) else control.setCurrentIndex(control.findData(value))
            self.preference_status.setText(
                "Saved revision "
                + str(state.base["revision"])
                + " · "
                + (
                    "Pending: " + ", ".join(state.changed)
                    if state.changed
                    else "No unsaved changes"
                )
            )
            self.conflict_details.setPlainText(
                json.dumps(state.latest["settings"], indent=2) if state.latest else ""
            )
            if self._client().connection.workspace_id == self._home:
                self._apply_home_theme(state.base["settings"])
            self._controls()

        def _apply_home_theme(self, settings: dict[str, Any]) -> None:
            app = QApplication.instance()
            assert isinstance(app, QApplication)
            dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
            self.setStyleSheet(native_stylesheet(settings.get("theme", "system"), dark))
            self.preferencesChanged.emit(settings)

        def edit_preference(self, key: str, value: Any) -> None:
            state = self._draft()
            if state and not self._busy:
                state.edit(key, value)
                self.preference_status.setText(
                    "Saved revision "
                    + str(state.base["revision"])
                    + " · Pending: "
                    + ", ".join(state.changed)
                )
                self._controls()

        def _save_preferences(self) -> None:
            state = self._draft()
            if not state or not state.changed or state.latest or self._busy or self._unknown_save:
                return
            client, principal, revision, draft = (
                self._client(),
                self._principal,
                state.base["revision"],
                dict(state.draft),
            )
            self.operation.clear()

            def work() -> Any:
                try:
                    return client.save_preferences(principal, revision, draft)
                except NativeSettingsError as error:
                    if error.code != "conflict":
                        raise
                    return {"conflict": client.read("preferences", principal)}

            self._start("save", work)

        def _rebase(self) -> None:
            state = self._draft()
            if state:
                conflicts = state.rebase()
                self.operation.setText(
                    "Rebased for review. "
                    + (
                        "Your choices replace concurrent changes to: " + ", ".join(conflicts)
                        if conflicts
                        else "Unrelated concurrent choices were preserved."
                    )
                    + " Save only after checking."
                )
                self._render_preferences()

        def _use_saved(self) -> None:
            state = self._draft()
            if state and state.latest:
                state.accept(state.latest)
                self._render_preferences()

        def _filter(self, *_args: Any) -> None:
            needle = self.search.text().casefold().strip()
            for tree in (
                self.values,
                self.features,
                self.inventory,
                self.nodes,
                self.deployments,
                self.grants,
                self.requests,
            ):
                for index in range(tree.topLevelItemCount()):
                    item = tree.topLevelItem(index)
                    visible = not needle or any(
                        needle in item.text(col).casefold() for col in range(tree.columnCount())
                    )
                    if tree is self.inventory:
                        row, selected = (
                            item.data(0, Qt.ItemDataRole.UserRole),
                            self.inventory_filter.currentData(),
                        )
                        visible = visible and (
                            selected == "all"
                            or selected == "observed"
                            and row["presence"] == "observed"
                            or selected == "records"
                            and row["presence"] == "not_observed"
                            or selected == "attention"
                            and (
                                row["presence"] != "observed" or row["qualification"] != "qualified"
                            )
                        )
                    item.setHidden(not visible)

        def _inventory_selected(self) -> None:
            item = self.inventory.currentItem()
            self.inventory_details.setPlainText(
                json.dumps(item.data(0, Qt.ItemDataRole.UserRole), indent=2) if item else ""
            )

        def _swarm_selected(self, *_args: Any) -> None:
            tree: Any = self.swarm_tabs.currentWidget()
            item = tree.currentItem()
            data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            text = (
                json.dumps(data, indent=2)
                if data
                else "Select a record to inspect its public identity."
            )
            if tree is self.requests and data and data["state"] == "requested":
                text += (
                    "\n\nProtected native operator handoff:\n"
                    + self._handoff(data)
                    + "\n\nRefresh after native application. A saved request does not establish revocation."
                )
            self.swarm_details.setPlainText(text)
            self._controls()

        def _handoff(self, request: dict[str, Any]) -> str:
            return (
                "potbot remote-workspace apply-revocation --workspace-id "
                + self._client().connection.workspace_id
                + " --owner-principal-id "
                + self._principal
                + " --request-id "
                + str(request["requestId"])
                + " --confirm"
            )

        def _copy_handoff(self) -> None:
            item = self.requests.currentItem()
            if item:
                row = item.data(0, Qt.ItemDataRole.UserRole)
                if row["state"] == "requested":
                    QApplication.clipboard().setText(self._handoff(row))
                    self.operation.setText(
                        "Copied the native operator command. It has not been executed."
                    )

        def _prepare_revocation(self) -> None:
            item = self.grants.currentItem()
            if not item or not self._document or self._busy:
                return
            grant = item.data(0, Qt.ItemDataRole.UserRole)
            if not grant["canRequestRevocation"]:
                return
            workspace, principal, requests = (
                self._client().connection.workspace_id,
                self._principal,
                self._document["requests"],
            )
            self._start(
                "prepare", lambda: self._journal.prepare(workspace, principal, grant, requests)
            )

        def _show_review(self) -> None:
            dialog = ReviewDialog(self)
            self.review_dialog = dialog
            dialog.setObjectName("revocationReviewDialog")
            dialog.setWindowTitle("Review grant revocation request")
            dialog.resize(620, 430)
            layout = QVBoxLayout(dialog)
            layout.addWidget(
                self.label(
                    "This records an exact owner request. A protected native operator must apply it before the grant is revoked."
                )
            )
            layout.addWidget(
                self.label(
                    "Target workspace: "
                    + self._client().connection.workspace_id
                    + "\nOwner principal: "
                    + self._principal
                )
            )
            details = QPlainTextEdit(json.dumps(self._review, indent=2))
            details.setReadOnly(True)
            details.setAccessibleName("Exact retained revocation review")
            layout.addWidget(details)
            self.review_notice = self.label("", "reviewNotice")
            layout.addWidget(self.review_notice)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
            )
            self.review_submit = buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.review_submit.setText("Submit this request")
            self.review_submit.setObjectName("reviewSubmit")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
            buttons.rejected.connect(dialog.reject)
            self.review_submit.clicked.connect(self._submit_review)
            layout.addWidget(buttons)
            dialog.open()

        def _submit_review(self) -> None:
            if self._busy or not self._review or not self.review_dialog:
                return
            client, principal, review = self._client(), self._principal, dict(self._review)
            self.review_dialog.posting = True
            self.review_submit.setEnabled(False)
            self._start("revoke", lambda: client.request_revocation(principal, review))

        def _controls(self) -> None:
            available = bool(self._catalogue and self._principal)
            self.refresh.setEnabled(not self._busy)
            self.target.setEnabled(not self._mutating)
            self.category.setEnabled(not self._mutating)
            state = self._draft()
            for control in self.preference_controls.values():
                control.setEnabled(available and not self._busy)
            self.save.setEnabled(
                available
                and bool(state and state.changed and not state.latest)
                and not self._busy
                and not self._unknown_save
            )
            conflict = bool(state and state.latest) and available
            self.rebase.setVisible(conflict)
            self.use_saved.setVisible(conflict)
            self.conflict_details.setVisible(conflict)
            self.rebase.setEnabled(not self._busy)
            self.use_saved.setEnabled(not self._busy)
            grant = self.grants.currentItem()
            can_revoke = bool(
                grant and grant.data(0, Qt.ItemDataRole.UserRole)["canRequestRevocation"]
            )
            self.revoke.setEnabled(available and can_revoke and not self._busy)
            self.revoke.setVisible(self.swarm_tabs.currentIndex() == 2)
            request = self.requests.currentItem()
            self.copy_handoff.setEnabled(
                available
                and bool(
                    request and request.data(0, Qt.ItemDataRole.UserRole)["state"] == "requested"
                )
            )
            self.copy_handoff.setVisible(self.swarm_tabs.currentIndex() == 3)
            if self.review_dialog and not self.review_dialog.posting:
                self.review_submit.setEnabled(not self._busy)

        def closeEvent(self, event: Any) -> None:
            self._closed = True
            self._cancel.set()
            self._epoch += 1
            super().closeEvent(event)

    return SettingsWidget()
