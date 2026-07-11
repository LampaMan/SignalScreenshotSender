import os
import sys
import json
import time
import threading
import subprocess
import ctypes
from datetime import datetime
from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtGui import QPalette, QColor
from tray_icon import create_tray_icon
import mss
from PIL import Image
import logging
from logging.handlers import RotatingFileHandler
from signal_security import PendingDevice, SecureSessionManager, SignalDaemonMonitor, approve_pending_device
from updater import RepositoryUpdater, compare_versions


# Try to make the process DPI-aware on Windows so Tk/canvas coordinates
# and system screen coordinates use the same pixel scaling. This helps
# avoid selection/ cropping issues on high-DPI (4K) monitors.
def ensure_dpi_aware():
    try:
        # Windows 8.1+: shcore.SetProcessDpiAwareness
        shcore = ctypes.windll.shcore
        PROCESS_PER_MONITOR_DPI_AWARE = 2
        shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        logger.info("Set process DPI awareness via shcore.SetProcessDpiAwareness")
    except Exception:
        try:
            # Fallback: user32.SetProcessDPIAware (older Windows)
            ctypes.windll.user32.SetProcessDPIAware()
            logger.info("Set process DPI awareness via user32.SetProcessDPIAware")
        except Exception:
            logger.debug("Could not set process DPI awareness; continuing without it")

# Call early so GUI toolkits report real pixel sizes where possible
try:
    ensure_dpi_aware()
except Exception:
    pass

SETTINGS_FILE = "settings.json"
# Skip automatic update-group runs at this local time (hour, minute).
UPDATE_GROUP_DISABLED_AT = (12, 0)

# ---------- Logging ----------
LOG_FILE = "autoscreen.log"
logger = logging.getLogger("autoscreen")
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ---------- Settings Loader ----------
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.exception("Failed to parse settings file %s: %s", SETTINGS_FILE, e)
            try:
                backup_name = SETTINGS_FILE + ".corrupt." + time.strftime("%Y%m%d%H%M%S")
                os.rename(SETTINGS_FILE, backup_name)
                logger.info("Renamed corrupt settings file to %s", backup_name)
            except Exception:
                logger.exception("Unable to backup corrupt settings file %s", SETTINGS_FILE)
            return {}
        except Exception:
            logger.exception("Failed to read settings file %s", SETTINGS_FILE)
            return {}
    return {}

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
    except Exception:
        logger.exception("Failed to save settings to %s", SETTINGS_FILE)

# ---------- System Check ----------
def is_display_on():
    user32 = ctypes.windll.user32
    return user32.GetForegroundWindow() != 0

def is_workstation_locked():
    from ctypes import windll
    return windll.user32.GetForegroundWindow() == 0

def is_account_locked():
    from ctypes import windll
    return windll.user32.GetDesktopWindow() == 0

# ---------- Screenshot Sender ----------
def send_screenshot(image_path):
    settings = load_settings()
    cli_path = settings.get("cli_path")
    group_id = settings.get("group_id")
    signal_recipient = settings.get("signal_recipient")

    if not cli_path or not group_id:
        logger.error("Signal CLI or group_id not configured; cannot send screenshot")
        return

    send_cmd = [
        cli_path,
        "send",
        "-a", signal_recipient,
        "-g", group_id,
        "--attachment", image_path
    ]

    DETACHED = 0x08000000 # Detach process from parent console
    try:
        subprocess.run(send_cmd, creationflags=DETACHED, cwd=os.path.dirname(cli_path), capture_output=True, text=True, check=True)
        logger.info("Screenshot sent to Signal group: %s", image_path)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to send screenshot: returncode=%s; stdout=%s; stderr=%s", getattr(e, 'returncode', None), (getattr(e, 'stdout', '') or "").strip(), (getattr(e, 'stderr', '') or "").strip())
        logger.debug("CalledProcessError details", exc_info=True)
    except Exception:
        logger.exception("Unexpected error while sending screenshot")

# ---------- Screenshot Capture ----------
def capture_area(area):
    try:
        with mss.MSS() as sct:
            # Grab the full virtual screen (monitor 0) and then crop.
            # This is more reliable across complex multi-monitor arrangements
            # (e.g., monitors positioned above/below) where grabbing a large
            # bbox directly can sometimes be clipped by backend limitations.
            vmon = sct.monitors[0]
            sct_img = sct.grab(vmon)
            img = Image.frombytes("RGB", sct_img.size, sct_img.rgb)

            # area is absolute virtual-screen coordinates; convert to image-relative
            left, top, right, bottom = map(int, area)
            vleft, vtop = int(vmon.get('left', 0)), int(vmon.get('top', 0))
            crop_box = (left - vleft, top - vtop, right - vleft, bottom - vtop)
            # Ensure crop box is within image bounds
            crop_box = (
                max(0, crop_box[0]),
                max(0, crop_box[1]),
                min(img.width, crop_box[2]),
                min(img.height, crop_box[3])
            )
            return img.crop(crop_box)
    except Exception:
        logger.exception("Failed to capture screen area: %s", area)
        raise
    
def update_group():
    settings = load_settings()
    cli_path = settings.get("cli_path")
    signal_recipient = settings.get("signal_recipient")
    group_id = settings.get("group_id")

    now = datetime.now()
    if (now.hour, now.minute) == UPDATE_GROUP_DISABLED_AT:
        logger.info("Skipping update_group at configured disabled time %s", UPDATE_GROUP_DISABLED_AT)
        return

    if not cli_path:
        logger.error("Signal CLI path not configured for update_group.")
        return

    # signal-cli expects a subcommand; include 'updateGroup' and use '-a' to specify account
    # place account (-a) before the subcommand so it's treated as the account selector
    update_group_cmd = [
        cli_path,
        "-a", signal_recipient,
        "updateGroup",
        "--group-id", group_id
    ]

    DETACHED = 0x08000000 # Detach process from parent console
    logger.info("Updating Signal group... cmd=%s", update_group_cmd)
    try:
        if group_id and signal_recipient:
            # capture output to include stdout/stderr in logs for diagnosis
            # Add a timeout so the app doesn't hang if signal-cli blocks.
            result = subprocess.run(
                update_group_cmd,
                creationflags=DETACHED,
                cwd=os.path.dirname(cli_path),
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            logger.info("Signal group updated: %s; stdout=%s", group_id, (result.stdout or "").strip())
    except subprocess.CalledProcessError as e:
        logger.error("Failed to update Signal group: %s; returncode=%s; stdout=%s; stderr=%s", group_id, getattr(e, 'returncode', None), (getattr(e, 'stdout', '') or "").strip(), (getattr(e, 'stderr', '') or "").strip())
        logger.debug("CalledProcessError details", exc_info=True)
    except Exception:
        logger.exception("Unexpected error in update_group")

# ---------- Cleanup Temp for Signal JNI ----------
def clean_temp_for_signal():
    """Clean temporary files related to Signal JNI AMD64"""
    try:
        user = os.environ.get('USERNAME')
        if not user:
            logger.warning("Could not determine current username for cleanup")
            return
        
        paths = [
            "C:\\Windows\\Temp",
            f"C:\\Users\\{user}\\AppData\\Local\\Temp",
            f"C:\\Users\\{user}\\AppData\\LocalLow\\Temp"
        ]
        
        logger.info("Starting Signal temp cleanup at %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        for path in paths:
            if os.path.exists(path):
                try:
                    logger.info("Cleaning Signal JNI files in: %s", path)
                    
                    # Use PowerShell to remove Signal JNI files
                    ps_script = f"""
$path = '{path}'
if (Test-Path $path) {{
    Get-ChildItem -Path $path -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Name -like "*signal_jni_amd64*" }} |
        Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
}}
"""
                    
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", ps_script],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    
                    if result.returncode == 0:
                        logger.info("Successfully cleaned %s", path)
                    else:
                        logger.warning("PowerShell cleanup command returned code %d for %s: %s", 
                                     result.returncode, path, result.stderr)
                except Exception as e:
                    logger.warning("Error cleaning path %s: %s", path, str(e))
            else:
                logger.debug("Path does not exist: %s", path)
        
        logger.info("Signal temp cleanup completed")
    except Exception:
        logger.exception("Unexpected error during Signal temp cleanup")

# ---------- Main App ----------
class ScreenshotApp(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screenshot Sender")
        self.setWindowIcon(QtGui.QIcon("icon.png"))
        self.setFixedSize(400, 390)
        self.setMinimumSize(390, 390)
        self.setMaximumSize(430, 430)

        self.settings = load_settings()

        self.security_manager = SecureSessionManager(base_dir=os.getcwd())
        self.security_manager.initialize()
        self.pending_devices = []
        self.updater = RepositoryUpdater(repo_url="https://github.com/LampaMan/SignalScreenshotSender.git", repo_dir=os.getcwd())

        self.init_ui()
        self.tray_icon = create_tray_icon(self)
        self.tray_icon.show()

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.queue_screenshot_send)

        self.last_image_path = None
        self.screenshot_thread = None
        self._load_pending_devices_from_disk()
        self.daemon_monitor = SignalDaemonMonitor(self.settings, self.security_manager, callback=self.handle_pending_device)
        self.daemon_monitor.start()

        # Run cleanup at startup (non-blocking)
        threading.Thread(target=clean_temp_for_signal, daemon=True).start()

        # Cleanup timer for Signal temp files at 12:00 daily
        self.cleanup_timer = QtCore.QTimer()
        self.cleanup_timer.timeout.connect(self.check_and_run_cleanup)
        self.cleanup_timer.start(60 * 1000)  # Check every minute
        self.last_cleanup_date = None

        self.update_check_timer = QtCore.QTimer()
        self.update_check_timer.timeout.connect(self.check_for_updates_periodically)
        self.update_check_timer.start(6 * 60 * 60 * 1000)  # Check every 6 hours
        self.last_update_notification = None
        self.pending_update_version = None
        

    def init_ui(self):
        self.enable_dark_theme()

        main_layout = QtWidgets.QVBoxLayout()

        self.main_tab = QtWidgets.QWidget()
        self.security_tab = QtWidgets.QWidget()
        self.donate_tab = QtWidgets.QWidget()

        self.page_stack = QtWidgets.QStackedWidget()
        self.page_stack.addWidget(self.main_tab)
        self.page_stack.addWidget(self.security_tab)
        self.page_stack.addWidget(self.donate_tab)
        self.page_stack.currentChanged.connect(lambda _: QtCore.QTimer.singleShot(0, self._update_donate_image))

        self.section_tab_bar = QtWidgets.QTabBar()
        self.section_tab_bar.addTab("Основне")
        self.section_tab_bar.addTab("Дозволи пристроїв")
        self.section_tab_bar.setShape(QtWidgets.QTabBar.RoundedNorth)
        self.section_tab_bar.setUsesScrollButtons(False)
        self.section_tab_bar.setExpanding(False)
        self.section_tab_bar.currentChanged.connect(self.page_stack.setCurrentIndex)

        self.donate_switch_btn = QtWidgets.QPushButton("Дяка")
        self.donate_switch_btn.setFixedHeight(28)
        self.donate_switch_btn.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.donate_switch_btn.setStyleSheet(
            "QPushButton { padding: 4px 10px; border: 1px solid #666; border-radius: 6px; background: #404040; color: white; }"
            "QPushButton:hover { background: #555; }"
        )
        self.donate_switch_btn.clicked.connect(lambda: self.page_stack.setCurrentIndex(2))

        tab_row = QtWidgets.QWidget()
        tab_row_layout = QtWidgets.QHBoxLayout(tab_row)
        tab_row_layout.setContentsMargins(0, 0, 0, 0)
        tab_row_layout.addWidget(self.section_tab_bar)
        tab_row_layout.addStretch()
        tab_row_layout.addWidget(self.donate_switch_btn)
        main_layout.addWidget(tab_row)
        main_layout.addWidget(self.page_stack)

        main_layout_main = QtWidgets.QVBoxLayout()
        self.main_tab.setLayout(main_layout_main)

        self.select_area_btn = QtWidgets.QPushButton("Обрати область екрана")
        self.select_area_btn.clicked.connect(self.select_area)
        main_layout_main.addWidget(self.select_area_btn)
        
        self.update_group_btn = QtWidgets.QPushButton("Оновити групу Signal")
        self.update_group_btn.clicked.connect(self.run_update_group)
        main_layout_main.addWidget(self.update_group_btn)

        self.check_update_btn = QtWidgets.QPushButton("Перевірити оновлення")
        self.check_update_btn.clicked.connect(self.run_update_check)
        main_layout_main.addWidget(self.check_update_btn)

        interval_widget = QtWidgets.QWidget()
        interval_layout = QtWidgets.QVBoxLayout()
        interval_widget.setLayout(interval_layout)

        self.interval_label = QtWidgets.QLabel("Інтервал скріншотів:")
        self.interval_label.setAlignment(QtCore.Qt.AlignCenter)
        interval_layout.addWidget(self.interval_label)

        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setMinimum(1)
        self.interval_spin.setValue(self.settings.get("interval", 4))
        self.interval_spin.setSuffix(" хв")
        self.interval_spin.setFixedHeight(80)
        self.interval_spin.setFixedWidth(150)
        font = self.interval_spin.font()
        font.setPointSize(18)
        self.interval_spin.setFont(font)
        self.interval_spin.setAlignment(QtCore.Qt.AlignCenter)
        self.interval_spin.valueChanged.connect(self.save_interval)
        interval_layout.addWidget(self.interval_spin, alignment=QtCore.Qt.AlignCenter)

        step_layout = QtWidgets.QHBoxLayout()
        minus_btn = QtWidgets.QPushButton("-1")
        plus_btn = QtWidgets.QPushButton("+1")
        step_layout.addWidget(minus_btn)
        step_layout.addWidget(plus_btn)
        interval_layout.addLayout(step_layout)

        minus_btn.clicked.connect(lambda: self.interval_spin.setValue(self.interval_spin.value() - 1))
        plus_btn.clicked.connect(lambda: self.interval_spin.setValue(self.interval_spin.value() + 1))

        quick_layout = QtWidgets.QHBoxLayout()
        quick_1 = QtWidgets.QPushButton("1 хв")
        quick_4 = QtWidgets.QPushButton("4 хв")
        quick_layout.addWidget(quick_1)
        quick_layout.addWidget(quick_4)
        interval_layout.addLayout(quick_layout)

        quick_1.clicked.connect(lambda: self.interval_spin.setValue(1))
        quick_4.clicked.connect(lambda: self.interval_spin.setValue(4))
        main_layout_main.addWidget(interval_widget)

        self.start_btn = QtWidgets.QPushButton("Почати")
        self.start_btn.clicked.connect(self.toggle_timer)
        main_layout_main.addWidget(self.start_btn)

        # Footer with version and author
        footer_widget = QtWidgets.QWidget()
        footer_layout = QtWidgets.QHBoxLayout()
        footer_layout.setContentsMargins(5, 5, 5, 5)
        
        # ---------- Version ----------
        version = "1.0.0.13"
        version_label = QtWidgets.QLabel(f"v{version}")
        version_label.setStyleSheet("color: gray; font-size: 9px;")
        footer_layout.addWidget(version_label, alignment=QtCore.Qt.AlignLeft)
        
        author_label = QtWidgets.QLabel("by LampaMan")
        author_label.setStyleSheet("color: gray; font-size: 9px;")
        footer_layout.addWidget(author_label, alignment=QtCore.Qt.AlignRight)
        
        footer_widget.setLayout(footer_layout)
        main_layout_main.addWidget(footer_widget)

        security_layout = QtWidgets.QVBoxLayout()
        self.security_tab.setLayout(security_layout)
        self.security_info = QtWidgets.QLabel("Список очікуваних пристроїв буде тут.\nПрограма працює у фоновому режимі, без мережевих виходів.")
        self.security_info.setWordWrap(True)
        security_layout.addWidget(self.security_info)
        self.security_status = QtWidgets.QLabel(f"Локальний сеанс: {self.security_manager.get_fingerprint()[:16]}")
        security_layout.addWidget(self.security_status)
        self.security_list = QtWidgets.QListWidget()
        security_layout.addWidget(self.security_list)

        donate_layout = QtWidgets.QVBoxLayout()
        self.donate_tab.setLayout(donate_layout)
        donate_layout.setContentsMargins(10, 6, 10, 10)
        donate_title = QtWidgets.QLabel("Підтримай автоматизатора")
        donate_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        donate_layout.addWidget(donate_title, alignment=QtCore.Qt.AlignCenter)
        donate_text = QtWidgets.QLabel(
            "Якщо ця програма рятує тобі час, можна закинути гріш на шаурму автоматизатору рутинної роботи."
        )
        donate_text.setWordWrap(True)
        donate_text.setAlignment(QtCore.Qt.AlignCenter)
        donate_layout.addWidget(donate_text)
        donate_layout.addSpacing(6)

        self.donate_image = QtWidgets.QLabel()
        self.donate_image.setAlignment(QtCore.Qt.AlignCenter)
        self.donate_image.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        self.donate_image.setMinimumSize(0, 0)

        image_container = QtWidgets.QWidget()
        image_container.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        image_container_layout = QtWidgets.QVBoxLayout(image_container)
        image_container_layout.setContentsMargins(0, 0, 0, 0)
        image_container_layout.addStretch(1)
        image_container_layout.addWidget(self.donate_image, alignment=QtCore.Qt.AlignCenter)
        image_container_layout.addStretch(1)

        donate_layout.addWidget(image_container, stretch=1)
        QtCore.QTimer.singleShot(0, self._update_donate_image)

        central = QtWidgets.QWidget()
        central.setLayout(main_layout)
        self.setCentralWidget(central)

    def _update_donate_image(self):
        donate_path = os.path.join(os.getcwd(), "Donate.png")
        if not os.path.exists(donate_path) or not hasattr(self, "donate_image"):
            return
        pixmap = QtGui.QPixmap(donate_path)
        if pixmap.isNull():
            return

        available_size = self.donate_tab.size()
        if available_size.width() <= 1 or available_size.height() <= 1:
            available_size = self.size() - QtCore.QSize(80, 160)

        usable_width = max(1, available_size.width() - 70)
        usable_height = max(1, available_size.height() - 140)
        target_size = QtCore.QSize(int(usable_width * 0.95), int(usable_height * 0.95))

        scaled_pixmap = pixmap.scaled(
            target_size,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        self.donate_image.setPixmap(scaled_pixmap)
        self.donate_image.setFixedSize(scaled_pixmap.size())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "donate_image"):
            QtCore.QTimer.singleShot(0, self._update_donate_image)

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "donate_image"):
            QtCore.QTimer.singleShot(0, self._update_donate_image)

    def enable_dark_theme(self):
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.Window, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.WindowText, QtCore.Qt.white)
        dark_palette.setColor(QPalette.Base, QColor(25, 25, 25))
        dark_palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ToolTipBase, QtCore.Qt.white)
        dark_palette.setColor(QPalette.ToolTipText, QtCore.Qt.white)
        dark_palette.setColor(QPalette.Text, QtCore.Qt.white)
        dark_palette.setColor(QPalette.Button, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ButtonText, QtCore.Qt.white)
        dark_palette.setColor(QPalette.BrightText, QtCore.Qt.red)
        dark_palette.setColor(QPalette.Highlight, QColor(142, 45, 197).lighter())
        dark_palette.setColor(QPalette.HighlightedText, QtCore.Qt.black)
        QtWidgets.QApplication.setPalette(dark_palette)

    def save_interval(self, value):
        self.settings["interval"] = value
        save_settings(self.settings)
        if self.timer.isActive():
            self.restart_timer()

    def restart_timer(self):
        self.timer.stop()
        interval_ms = self.settings.get("interval", 5) * 60 * 1000
        self.timer.start(interval_ms)

    def toggle_timer(self):
        if self.timer.isActive():
            self.timer.stop()
            self.start_btn.setText("Почати")
        else:
            self.start_btn.setText("Зупинити")
            QtCore.QTimer.singleShot(10000, self.queue_screenshot_send)
            self.restart_timer()

    def run_update_group(self):
        self.update_group_btn.setEnabled(False)
        threading.Thread(target=self._run_update_group_in_background, daemon=True).start()

    def run_update_check(self):
        self.check_update_btn.setEnabled(False)
        threading.Thread(target=self._run_update_check_in_background, daemon=True).start()

    def check_for_updates_periodically(self):
        if self.last_update_notification is None:
            threading.Thread(target=self._run_update_check_in_background, daemon=True).start()

    def _run_update_group_in_background(self):
        try:
            update_group()
        except Exception:
            logger.exception("Error while updating Signal group")
        finally:
            QtCore.QMetaObject.invokeMethod(self, "enable_update_group_button", QtCore.Qt.QueuedConnection)

    def _run_update_check_in_background(self):
        try:
            remote_version = self.updater.fetch_remote_version()
            local_version = self.updater.current_version()
            if remote_version and compare_versions(local_version, remote_version) < 0:
                self.pending_update_version = remote_version
                self.last_update_notification = remote_version
                QtCore.QMetaObject.invokeMethod(self, "show_update_available_message", QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, remote_version))
                return
            if remote_version is None:
                logger.info("Update check could not resolve remote version")
        except Exception:
            logger.exception("Error while checking for updates")
        finally:
            QtCore.QMetaObject.invokeMethod(self, "enable_update_buttons", QtCore.Qt.QueuedConnection)

    def install_update(self, remote_version=None):
        target_version = remote_version or self.pending_update_version
        if not target_version:
            return
        self.check_update_btn.setEnabled(False)
        self.update_group_btn.setEnabled(False)
        threading.Thread(target=self._install_update_in_background, args=(target_version,), daemon=True).start()

    def _install_update_in_background(self, target_version):
        try:
            result = self.updater.run_self_update(target_version=target_version)
            if result.get("ok"):
                QtCore.QMetaObject.invokeMethod(self, "show_update_installed_message", QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, result.get("message", "")))
            else:
                QtCore.QMetaObject.invokeMethod(self, "show_update_failed_message", QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, result.get("error", "")))
        except Exception:
            logger.exception("Error while installing update")
        finally:
            QtCore.QMetaObject.invokeMethod(self, "enable_update_buttons", QtCore.Qt.QueuedConnection)

    @QtCore.pyqtSlot(str)
    def show_update_available_message(self, remote_version):
        reply = QMessageBox.question(
            self,
            "Оновлення доступне",
            f"Доступна нова версія: {remote_version}\nБажаєте встановити її зараз?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.install_update(remote_version)

    @QtCore.pyqtSlot(str)
    def show_update_installed_message(self, message):
        QMessageBox.information(self, "Оновлення встановлено", f"Оновлення застосовано: {message}\nПрограма буде перезапущена.")
        self.restart_application()

    @QtCore.pyqtSlot(str)
    def show_update_failed_message(self, message):
        QMessageBox.critical(self, "Помилка оновлення", f"Не вдалося встановити оновлення: {message}")

    def restart_application(self):
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__)])
        except Exception:
            logger.exception("Failed to restart application")
        finally:
            QtWidgets.QApplication.quit()

    @QtCore.pyqtSlot()
    def enable_update_group_button(self):
        self.update_group_btn.setEnabled(True)

    @QtCore.pyqtSlot()
    def enable_update_buttons(self):
        self.update_group_btn.setEnabled(True)
        self.check_update_btn.setEnabled(True)

    def _load_pending_devices_from_disk(self):
        for event in self.security_manager.load_local_events():
            if event.event_type != "pending_device":
                continue
            payload = event.payload
            device = PendingDevice(
                uuid=payload.get("uuid", ""),
                name=payload.get("name", "unknown"),
                message=payload.get("message", ""),
                source=payload.get("source", "signal-cli"),
            )
            if device.uuid and not any(item.uuid == device.uuid for item in self.pending_devices):
                self.pending_devices.append(device)

    def handle_pending_device(self, device: PendingDevice):
        if not any(item.uuid == device.uuid for item in self.pending_devices):
            self.pending_devices.append(device)
        QtCore.QMetaObject.invokeMethod(self, "refresh_security_ui", QtCore.Qt.QueuedConnection, QtCore.Q_ARG(object, device))
        logger.info("Pending device detected: %s (%s)", device.uuid, device.name)

    @QtCore.pyqtSlot(object)
    def refresh_security_ui(self, device):
        self._refresh_security_list()

    def _refresh_security_list(self):
        self.security_list.clear()
        if not self.pending_devices:
            self.security_info.setText("Наразі немає нових пристроїв для підтвердження.")
            return
        self.security_info.setText(f"Знайдено {len(self.pending_devices)} очікуваних пристроїв.")
        for device in self.pending_devices:
            item = QtWidgets.QListWidgetItem(f"{device.name} | {device.uuid}")
            self.security_list.addItem(item)
            widget = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(widget)
            label = QtWidgets.QLabel(f"{device.name}\n{device.uuid}")
            button = QtWidgets.QPushButton("Дозволити")
            button.clicked.connect(lambda checked=False, dev=device: self.approve_device(dev))
            layout.addWidget(label)
            layout.addWidget(button)
            self.security_list.setItemWidget(item, widget)

    def approve_device(self, device: PendingDevice):
        try:
            result = approve_pending_device(device, self.settings, self.security_manager)
            logger.info("Approved device %s with result: %s", device.uuid, result)
            self.pending_devices = [item for item in self.pending_devices if item.uuid != device.uuid]
            self._refresh_security_list()
        except Exception:
            logger.exception("Failed to approve device %s", device.uuid)

    def check_and_run_cleanup(self):
        """Check if it's 12:00 and run cleanup if needed"""
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        current_date = now.date()
        
        # Run cleanup if it's 12:00 (between 12:00 and 12:01) and we haven't run it today
        if current_hour == 12 and current_minute == 0 and self.last_cleanup_date != current_date:
            self.last_cleanup_date = current_date
            # Run cleanup in a separate thread to avoid blocking UI
            cleanup_thread = threading.Thread(target=clean_temp_for_signal)
            cleanup_thread.daemon = True
            cleanup_thread.start()

    def queue_screenshot_send(self):
        if self.screenshot_thread and self.screenshot_thread.is_alive():
            return
        self.screenshot_thread = threading.Thread(target=self.take_screenshot_and_wait)
        self.screenshot_thread.start()

    @QtCore.pyqtSlot()
    def pause_due_to_screen_off(self):
        QMessageBox.information(self, "Пауза", "Екран вимкнено, заблоковано або обліковий запис неактивний. Програму зупинено.")
        self.toggle_timer()

    def take_screenshot_and_wait(self):
        try:
            area = self.settings.get("area")
            if not area:
                logger.info("Screenshot area not selected; skipping capture")
                return

            if not is_display_on() or is_workstation_locked() or is_account_locked():
                logger.info("Display off or locked; cancelling screenshot")
                QtCore.QMetaObject.invokeMethod(self, "pause_due_to_screen_off", QtCore.Qt.QueuedConnection)
                return

            time.sleep(5)
            # Hide window before screenshot so app isn't captured
            self.hide()
            time.sleep(0.5)

            img = capture_area(area)
            path = os.path.join(os.getcwd(), "screenshot.webp")
            img = img.convert("RGB")
            img.save(path, "WEBP", quality=85, method=6)

            self.show()

            send_screenshot(path)
        except Exception:
            logger.exception("Error during take_screenshot_and_wait")
        
        

    def select_area(self):
        try:
            import tkinter as tk
            from screeninfo import get_monitors

            def on_mouse_down(event):
                nonlocal start_x, start_y, rect
                start_x, start_y = event.x, event.y
                rect = canvas.create_rectangle(start_x, start_y, start_x, start_y, outline='red', width=2, tags="rect")

            def on_mouse_move(event):
                if rect:
                    canvas.coords(rect, start_x, start_y, event.x, event.y)
                    canvas.delete("mask")
                    x1, y1 = min(start_x, event.x), min(start_y, event.y)
                    x2, y2 = max(start_x, event.x), max(start_y, event.y)
                    w = canvas.winfo_width()
                    h = canvas.winfo_height()

                    # Темна маска навколо вибраної області
                    canvas.create_rectangle(0, 0, w, y1, fill="black", stipple="gray50", tags="mask")
                    canvas.create_rectangle(0, y1, x1, y2, fill="black", stipple="gray50", tags="mask")
                    canvas.create_rectangle(x2, y1, w, y2, fill="black", stipple="gray50", tags="mask")
                    canvas.create_rectangle(0, y2, w, h, fill="black", stipple="gray50", tags="mask")

            def on_mouse_up(event):
                # Use canvas-local coordinates and map them to virtual-screen pixels.
                end_x, end_y = event.x, event.y

                # Ensure widget sizes are up-to-date
                overlay.update_idletasks()
                c_w = canvas.winfo_width()
                c_h = canvas.winfo_height()

                scale_x = width / c_w if c_w else 1.0
                scale_y = height / c_h if c_h else 1.0

                abs_x1 = int(x_min + min(start_x, end_x) * scale_x)
                abs_y1 = int(y_min + min(start_y, end_y) * scale_y)
                abs_x2 = int(x_min + max(start_x, end_x) * scale_x)
                abs_y2 = int(y_min + max(start_y, end_y) * scale_y)

                coords = [abs_x1, abs_y1, abs_x2, abs_y2]
                logger.info("Selected virtual coords: %s (canvas %dx%d -> virt %dx%d)", coords, c_w, c_h, width, height)
                self.settings["area"] = coords
                save_settings(self.settings)
                overlay.destroy()
                QtWidgets.QMessageBox.information(self, "Готово", f"Область збережено:\n{coords}")

            # Отримати всі монітори
            monitors = get_monitors()
            logger.info("screeninfo monitors: %s", monitors)
            x_min = min(m.x for m in monitors)
            y_min = min(m.y for m in monitors)
            x_max = max(m.x + m.width for m in monitors)
            y_max = max(m.y + m.height for m in monitors)

            width = x_max - x_min
            height = y_max - y_min

            overlay = tk.Tk()
            overlay.attributes('-alpha', 0.4)
            overlay.attributes('-topmost', True)
            overlay.config(cursor="cross")
            overlay.overrideredirect(True)
            overlay.geometry(f"{width}x{height}+{x_min}+{y_min}")

            start_x = start_y = 0
            rect = None

            canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
            canvas.pack(fill=tk.BOTH, expand=True)

            def on_mouse_down_global(event):
                nonlocal start_x, start_y
                start_x, start_y = event.x, event.y
                on_mouse_down(event)

            canvas.bind("<ButtonPress-1>", on_mouse_down_global)
            canvas.bind("<B1-Motion>", on_mouse_move)
            canvas.bind("<ButtonRelease-1>", on_mouse_up)
            overlay.bind("<KeyPress>", lambda e: overlay.destroy() if e.keysym == 'Escape' else None)
            overlay.focus_force()
            overlay.mainloop()
        except Exception:
            logger.exception("Error in select_area")
            QtWidgets.QMessageBox.critical(self, "Помилка", "Сталася помилка під час вибору області. Перегляньте лог.")


    



# ---------- Main ----------
if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    QtWidgets.QApplication.setStyle("Fusion")

    settings = load_settings()
    if not settings.get("group_id") or not settings.get("cli_path"):
        logger.error("Missing Signal configuration: group_id or cli_path")
        QtWidgets.QMessageBox.critical(None, "Налаштування не завершено", "Будь ласка, спочатку запустіть setup_gui.py для налаштування Signal.")
        sys.exit(1)

    window = ScreenshotApp()
    window.show()
    sys.exit(app.exec_())
