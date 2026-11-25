"""Native local Potbot workspace shell."""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from typing import Any

from potbot.desktop_state import WEB_UI_URL, DesktopStatus, probe_status
from potbot.domain.errors import CapabilityUnavailableError, DomainError
from potbot.instance import InstanceBusy, InstanceLease, StartupCancelled, request_stop


@dataclass
class _DesktopInterrupt:
    requested: bool = False
    dispatched: bool = False

    def handle(self, _signum: int, _frame: object) -> None:
        # Python can deliver this inside a Qt callback or a lock operation.
        # Defer event locks, file I/O and every Qt call to the normal timer.
        self.requested = True

    def dispatch(self, lease: InstanceLease) -> None:
        if not self.requested or self.dispatched:
            return
        self.dispatched = True
        lease.stopped.set()
        try:
            # Wake a foreground worker's cleanup and a manager-owned service.
            # The full stop after Qt exits also covers a queued task that does
            # not yet own a service lease at the time of this early request.
            request_stop("service", root=lease.root)
        except (DomainError, OSError):
            # Final stop reports and verifies failures outside the Qt callback.
            pass


@contextmanager
def _capture_interrupt() -> Iterator[_DesktopInterrupt]:
    if threading.current_thread() is not threading.main_thread():
        raise CapabilityUnavailableError("The native desktop must run on the main thread.")
    interrupt = _DesktopInterrupt()
    previous = signal.signal(signal.SIGINT, interrupt.handle)
    try:
        yield interrupt
    finally:
        signal.signal(signal.SIGINT, previous)


def main() -> int:
    try:
        with _capture_interrupt() as interrupt:
            with InstanceLease("desktop") as lease:
                result = _run_desktop(lease, interrupt)
            if interrupt.requested:
                # _stop waits for the desktop lease, so release it first. Keep
                # SIGINT trapped while the existing stop path verifies tasks,
                # WSL and PostgreSQL instead of interrupting that cleanup.
                from potbot.oneclick import main as oneclick_main

                stopped = oneclick_main(["stop"])
                return 130 if stopped == 0 else stopped
            return result
    except InstanceBusy as error:
        print("[Potbot] " + str(error), file=sys.stderr, flush=True)
        return 1
    except StartupCancelled as error:
        print("[Potbot] " + str(error), file=sys.stderr, flush=True)
        return 130


def _run_desktop(lease: InstanceLease, interrupt: _DesktopInterrupt) -> int:
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
            QStackedWidget,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as error:
        raise CapabilityUnavailableError(
            "The native desktop requires the locked PySide6-Essentials runtime."
        ) from error

    from potbot.desktop_settings import create_settings_widget, native_stylesheet
    from potbot.oneclick import start_services

    messages: SimpleQueue[tuple[str, str]] = SimpleQueue()

    class WorkspaceWindow(QMainWindow):  # type: ignore[misc, unused-ignore]  # Optional Qt runtime.
        def __init__(self) -> None:
            super().__init__()
            self._starter: threading.Thread | None = None
            self._startup_error = ""
            self._settings_backend_ready = False
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
                [
                    "Chat",
                    "Reasoning graph",
                    "Applications",
                    "Models",
                    "Tools",
                    "Workspace swarm",
                    "Settings",
                    "Preferences",
                ]
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
            self.settings_panel = create_settings_widget(self)
            self.settings_panel.preferencesChanged.connect(self._apply_preferences)
            self.settings_panel.mutationBusy.connect(navigation.setDisabled)
            pages = QStackedWidget()
            pages.addWidget(content)
            pages.addWidget(self.settings_panel)

            def select_panel(index: int) -> None:
                section = {3: "models", 4: "tools", 5: "swarm", 6: "overview", 7: "appearance"}.get(
                    index
                )
                if section is None:
                    pages.setCurrentWidget(content)
                else:
                    pages.setCurrentWidget(self.settings_panel)
                    self.settings_panel.open_section(section)

            navigation.currentRowChanged.connect(select_panel)
            scene.addWidget(pages)
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

            self.setStyleSheet(native_stylesheet("dark"))

            self.timer = QTimer(self)
            self.timer.timeout.connect(self._refresh)
            self.timer.start(800)
            self._refresh()

        def _apply_preferences(self, settings: dict[str, object]) -> None:
            application = QApplication.instance()
            assert isinstance(application, QApplication)
            system_dark = application.styleHints().colorScheme() == Qt.ColorScheme.Dark
            self.setStyleSheet(native_stylesheet(str(settings.get("theme", "system")), system_dark))

        def closeEvent(self, event: Any) -> None:
            if self._starter is not None and self._starter.is_alive():
                # Preserve ownership until the setup worker acknowledges this.
                # The shared event also covers a worker not yet inside _start.
                lease.stopped.set()
                self.banner.setText("Cancelling Potbot startup…")
                event.ignore()
                return
            self.settings_panel.close()
            super().closeEvent(event)

        def start_stack(self, _checked: bool = False) -> None:
            if lease.stopped.is_set():
                return
            if self._starter is not None and self._starter.is_alive():
                return
            self._startup_error = ""
            self.retry_button.setEnabled(False)

            def worker() -> None:
                try:
                    code = start_services(open_browser=False, stop_event=lease.stopped)
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
            self.status_labels["xpra"].setText(
                "Xpra applications · " + self._label(
                    status.xpra, "failed/unreachable" if status.xpra_failed else "starting"
                )
            )
            self.status_labels["web"].setText("Remote web interface · " + self._label(status.web))
            self.web_button.setEnabled(status.web)
            if status.backend and not self._settings_backend_ready:
                self.settings_panel.backend_available()
            self._settings_backend_ready = status.backend
            self.retry_button.setEnabled(self._starter is None or not self._starter.is_alive())
            if status.xpra:
                self.app_message.setText(
                    "Xpra is online. No X applications are running yet; an empty application "
                    "inventory never hides the workspace."
                )
            else:
                self.app_message.setText(
                    status.xpra_detail or (
                        "Xpra is still starting. The workspace remains available while application "
                        "services come online."
                    )
                )

        def _refresh(self) -> None:
            interrupt.dispatch(lease)
            if lease.stopped.is_set():
                self.retry_button.setEnabled(False)
                self.banner.setText("Stopping Potbot…")
                # Keep the desktop lease until its startup thread finishes so
                # stop cannot report success while setup is still running.
                if self._starter is None or not self._starter.is_alive():
                    self.close()
                    application = QApplication.instance()
                    if application is not None:
                        application.quit()
                return
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
