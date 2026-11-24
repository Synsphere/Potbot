"""Native local Potbot workspace shell."""

from __future__ import annotations

import threading
from queue import Empty, SimpleQueue

from potbot.desktop_state import WEB_UI_URL, DesktopStatus, probe_status
from potbot.domain.errors import CapabilityUnavailableError, DomainError


def main() -> int:
    try:
        from PySide6.QtCore import Qt, QTimer, QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import (
            QApplication,
            QDockWidget,
            QFrame,
            QLabel,
            QListWidget,
            QMainWindow,
            QPushButton,
            QSizePolicy,
            QSplitter,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as error:
        raise CapabilityUnavailableError(
            "The native desktop requires the locked PySide6-Essentials runtime."
        ) from error

    from potbot.oneclick import start_services

    messages: SimpleQueue[tuple[str, str]] = SimpleQueue()

    class WorkspaceWindow(QMainWindow):  # type: ignore[misc]  # Optional Qt runtime.
        def __init__(self) -> None:
            super().__init__()
            self._starter: threading.Thread | None = None
            self._startup_error = ""
            self.setWindowTitle("Potbot")
            self.resize(1260, 780)
            self.setMinimumSize(860, 560)

            root = QWidget()
            layout = QVBoxLayout(root)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            self.banner = QLabel("Starting Potbot…")
            self.banner.setObjectName("startupBanner")
            self.banner.setMinimumHeight(44)
            self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.banner)

            scene = QSplitter(Qt.Orientation.Horizontal)
            navigation = QListWidget()
            navigation.setObjectName("workspaceNav")
            navigation.addItems(
                ["Chat", "Reasoning graph", "Applications", "Models", "Preferences"]
            )
            navigation.setCurrentRow(0)
            navigation.setMaximumWidth(210)
            scene.addWidget(navigation)

            content = QFrame()
            content.setObjectName("workspaceCanvas")
            canvas = QVBoxLayout(content)
            eyebrow = QLabel("LOCAL NATIVE WORKSPACE")
            eyebrow.setObjectName("eyebrow")
            title = QLabel("Potbot Workspace")
            title.setObjectName("workspaceTitle")
            explanation = QLabel(
                "The native workspace is available immediately. Backend services, managed WSL, "
                "Xpra and the remote web interface attach as they become ready."
            )
            explanation.setWordWrap(True)
            explanation.setObjectName("workspaceCopy")
            empty_chat = QLabel(
                "Chat and reasoning controls will move into this native surface in the next "
                "milestones. The complete remote interface remains available in Next.js."
            )
            empty_chat.setWordWrap(True)
            empty_chat.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_chat.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            empty_chat.setObjectName("emptyWorkspace")
            canvas.addWidget(eyebrow)
            canvas.addWidget(title)
            canvas.addWidget(explanation)
            canvas.addWidget(empty_chat, 1)
            scene.addWidget(content)
            scene.setStretchFactor(1, 1)
            layout.addWidget(scene, 1)
            self.setCentralWidget(root)

            status_dock = QDockWidget("Startup status", self)
            status_body = QWidget()
            status_layout = QVBoxLayout(status_body)
            self.status_labels: dict[str, QLabel] = {}
            for key, label in (
                ("config", "Workspace configuration"),
                ("backend", "Backend / managed WSL"),
                ("xpra", "Xpra applications"),
                ("web", "Remote web interface"),
            ):
                row = QLabel(label + " · waiting")
                row.setObjectName("statusRow")
                status_layout.addWidget(row)
                self.status_labels[key] = row
            status_layout.addStretch(1)
            self.retry_button = QPushButton("Retry startup")
            self.retry_button.clicked.connect(self.start_stack)
            status_layout.addWidget(self.retry_button)
            self.web_button = QPushButton("Open remote web interface")
            self.web_button.clicked.connect(self._open_web)
            self.web_button.setEnabled(False)
            status_layout.addWidget(self.web_button)
            status_dock.setWidget(status_body)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, status_dock)

            applications = QDockWidget("Workspace applications", self)
            app_body = QWidget()
            app_layout = QVBoxLayout(app_body)
            self.app_message = QLabel(
                "No X applications are running yet. The workspace remains available."
            )
            self.app_message.setWordWrap(True)
            app_layout.addWidget(self.app_message)
            applications.setWidget(app_body)
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, applications)

            self.setStyleSheet(
                """
                QMainWindow, QWidget { background: #08131d; color: #e9f3fa; }
                #startupBanner { background: #102638; color: #87ddf2; font-weight: 700;
                                 border-bottom: 1px solid #244154; }
                #workspaceNav { background: #0c1a26; border: 0; padding: 10px; }
                #workspaceNav::item { padding: 11px 12px; margin: 2px; border-radius: 6px; }
                #workspaceNav::item:selected { background: #163b4c; color: #9ce9f7; }
                #workspaceCanvas { padding: 26px; }
                #eyebrow { color: #68cfe9; font-size: 11px; font-weight: 700; }
                #workspaceTitle { font-size: 27px; font-weight: 700; padding: 4px 0 2px 0; }
                #workspaceCopy { color: #a8bbc7; font-size: 13px; }
                #emptyWorkspace { color: #738c9c; font-size: 14px; padding: 40px; }
                #statusRow { padding: 8px; background: #10202d; border-radius: 5px; }
                QDockWidget { color: #dbeaf2; }
                QDockWidget::title { background: #0e1e2a; padding: 8px; }
                QPushButton { background: #17445a; border: 1px solid #2d718d; border-radius: 5px;
                              padding: 8px 12px; color: white; }
                QPushButton:disabled { color: #657a86; background: #10202a; border-color: #203744; }
                QPushButton:hover:!disabled { background: #205b75; }
                """
            )

            self.timer = QTimer(self)
            self.timer.timeout.connect(self._refresh)
            self.timer.start(800)
            self._refresh()

        def start_stack(self, _checked: bool = False) -> None:
            if self._starter is not None and self._starter.is_alive():
                return
            self._startup_error = ""
            self.retry_button.setEnabled(False)

            def worker() -> None:
                try:
                    code = start_services(open_browser=False)
                    if code:
                        messages.put(("error", "Potbot startup returned a non-zero status."))
                    else:
                        messages.put(("complete", "Supervised services are running."))
                except DomainError as error:
                    messages.put(("error", str(error)))
                except Exception:
                    messages.put(
                        ("error", "Potbot startup failed; inspect the local service logs.")
                    )

            self._starter = threading.Thread(
                target=worker, name="potbot-native-startup", daemon=True
            )
            self._starter.start()

        def _open_web(self, _checked: bool = False) -> None:
            QDesktopServices.openUrl(QUrl(WEB_UI_URL))

        @staticmethod
        def _label(online: bool, waiting: str = "starting") -> str:
            return "online" if online else waiting

        def _apply(self, status: DesktopStatus) -> None:
            self.banner.setText(self._startup_error or status.banner)
            self.status_labels["config"].setText(
                "Workspace configuration · " + self._label(status.configured, "preparing")
            )
            self.status_labels["backend"].setText(
                "Backend / managed WSL · " + self._label(status.backend)
            )
            self.status_labels["xpra"].setText("Xpra applications · " + self._label(status.xpra))
            self.status_labels["web"].setText("Remote web interface · " + self._label(status.web))
            self.web_button.setEnabled(status.web)
            self.retry_button.setEnabled(self._starter is None or not self._starter.is_alive())
            if status.xpra:
                self.app_message.setText(
                    "Xpra is online. No X applications are running yet; an empty application "
                    "inventory never hides the workspace."
                )
            else:
                self.app_message.setText(
                    "Xpra is still starting. The workspace remains available while application "
                    "services come online."
                )

        def _refresh(self) -> None:
            while True:
                try:
                    kind, message = messages.get_nowait()
                except Empty:
                    break
                if kind == "error":
                    self._startup_error = message
                elif kind == "complete":
                    self._startup_error = ""
            self._apply(probe_status())

    application = QApplication.instance() or QApplication([])
    application.setApplicationName("Potbot")
    application.setOrganizationName("Synsphere")
    window = WorkspaceWindow()
    window.show()
    QTimer.singleShot(0, window.start_stack)
    return int(application.exec())


if __name__ == "__main__":
    raise SystemExit(main())
