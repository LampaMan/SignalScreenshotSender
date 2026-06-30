from PyQt5 import QtWidgets, QtGui, QtCore

def create_tray_icon(parent):
    tray = QtWidgets.QSystemTrayIcon(parent)
    tray.setIcon(QtGui.QIcon("icon.png"))

    menu = QtWidgets.QMenu(parent)

    start_action = menu.addAction("Почати")
    stop_action = menu.addAction("Зупинити")
    area_action = menu.addAction("Обрати область екрана")
    exit_action = menu.addAction("Вийти")

    def on_start():
        if not parent.timer.isActive():
            parent.start_btn.setText("Зупинити")
            QtCore.QTimer.singleShot(10000, parent.queue_screenshot_send)
            parent.restart_timer()


    def on_stop():
        if parent.timer.isActive():
            parent.toggle_timer()

    def on_area():
        parent.select_area()

    def on_exit():
        tray.hide()
        QtWidgets.qApp.quit()

    start_action.triggered.connect(on_start)
    stop_action.triggered.connect(on_stop)
    area_action.triggered.connect(on_area)
    exit_action.triggered.connect(on_exit)

    tray.setContextMenu(menu)

    def on_click(reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger:  # ЛКМ
            parent.showNormal()
            parent.activateWindow()

    tray.activated.connect(on_click)

    return tray
