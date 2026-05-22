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
        with mss.mss() as sct:
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
        self.setFixedSize(400, 300)

        self.settings = load_settings()

        self.init_ui()
        self.tray_icon = create_tray_icon(self)
        self.tray_icon.show()

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.queue_screenshot_send)

        self.last_image_path = None
        self.screenshot_thread = None
        
        self.update_timer = QtCore.QTimer()
        # Run update_group in a background thread to avoid blocking the GUI if signal-cli hangs.
        self.update_timer.timeout.connect(lambda: threading.Thread(target=update_group, daemon=True).start())
        self.update_timer.start(30 * 60 * 1000)  # 30 min in milliseconds
        threading.Thread(target=update_group, daemon=True).start()  # first run immediately

        # Run cleanup at startup (non-blocking)
        threading.Thread(target=clean_temp_for_signal, daemon=True).start()

        # Cleanup timer for Signal temp files at 12:00 daily
        self.cleanup_timer = QtCore.QTimer()
        self.cleanup_timer.timeout.connect(self.check_and_run_cleanup)
        self.cleanup_timer.start(60 * 1000)  # Check every minute
        self.last_cleanup_date = None
        

    def init_ui(self):
        self.enable_dark_theme()

        main_layout = QtWidgets.QVBoxLayout()

        self.select_area_btn = QtWidgets.QPushButton("Обрати область екрана")
        self.select_area_btn.clicked.connect(self.select_area)
        main_layout.addWidget(self.select_area_btn)

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
        main_layout.addWidget(interval_widget)

        self.start_btn = QtWidgets.QPushButton("Почати")
        self.start_btn.clicked.connect(self.toggle_timer)
        main_layout.addWidget(self.start_btn)

        # Footer with version and author
        footer_widget = QtWidgets.QWidget()
        footer_layout = QtWidgets.QHBoxLayout()
        footer_layout.setContentsMargins(5, 5, 5, 5)
        
        # ---------- Version ----------
        version = "1.0.0.12"
        version_label = QtWidgets.QLabel(f"v{version}")
        version_label.setStyleSheet("color: gray; font-size: 9px;")
        footer_layout.addWidget(version_label, alignment=QtCore.Qt.AlignLeft)
        
        author_label = QtWidgets.QLabel("by LampaMan")
        author_label.setStyleSheet("color: gray; font-size: 9px;")
        footer_layout.addWidget(author_label, alignment=QtCore.Qt.AlignRight)
        
        footer_widget.setLayout(footer_layout)
        main_layout.addWidget(footer_widget)

        central = QtWidgets.QWidget()
        central.setLayout(main_layout)
        self.setCentralWidget(central)

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
