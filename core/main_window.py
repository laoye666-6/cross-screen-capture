# -*- coding: utf-8 -*-
"""控制面板主窗口：模式选择、延时截图、导出设置、全局热键与系统托盘。"""

from PyQt5.QtCore import QSize, QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QComboBox, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QMainWindow,
                             QMenu, QPushButton, QRadioButton, QSlider,
                             QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import screen_utils
from .capture_overlay import CaptureOverlay
from .global_hotkey import MOD_CONTROL, MOD_SHIFT, GlobalHotkey, vk_for_char

MODE_LABELS = [
    ("rect", "矩形截图"),
    ("circle", "圆形截图"),
    ("ellipse", "椭圆截图"),
    ("lasso", "自由套索"),
    ("window", "窗口识别"),
]
FORMAT_LABELS = [
    ("png", "PNG（无损，支持透明）"),
    ("jpg", "JPEG（体积小）"),
    ("bmp", "BMP（位图）"),
    ("webp", "WebP（高压缩比）"),
    ("tif", "TIFF（无损存档）"),
]
HOTKEY_TEXT = "Ctrl + Shift + A"


def app_icon():
    """程序托盘图标（代码绘制，避免外部资源文件）。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(0, 168, 255), 5))
    p.drawRoundedRect(6, 18, 52, 38, 8, 8)
    p.drawEllipse(22, 28, 20, 18)
    p.drawRect(24, 10, 16, 9)
    p.end()
    return QIcon(pm)


class _Countdown(QWidget):
    """延时截图的屏幕中央倒计时。按 Esc 取消。"""
    finished = pyqtSignal()
    cancelled = pyqtSignal()

    def __init__(self, seconds, scale=1.0):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self._remaining = int(seconds)
        self._scale = scale
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.label = QLabel(self)
        self._apply_label()

    def _apply_label(self):
        f = self.label.font()
        f.setPixelSize(max(36, round(48 * self._scale)))
        f.setBold(True)
        self.label.setFont(f)
        self.label.setText("截图倒计时 %d 秒（Esc 取消）" % self._remaining)
        self.label.adjustSize()
        self.setFixedSize(self.label.size() + QSize(56, 32))
        self.label.move((self.width() - self.label.width()) // 2,
                        (self.height() - self.label.height()) // 2)

    def start(self):
        self.show_centered()
        self._timer.start(1000)

    def show_centered(self):
        geo = QApplication.primaryScreen().geometry()
        self.move(geo.center().x() - self.width() // 2,
                  geo.center().y() - self.height() // 2)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self.close()
            self.finished.emit()
        else:
            self._apply_label()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._timer.stop()
            self.close()
            self.cancelled.emit()
        else:
            super().keyPressEvent(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("跨屏截图工具")
        self.setFixedSize(380, 440)
        self._settings = QSettings("ZcodeTool", "CrossScreenCapture")
        self._overlay = None
        self._countdown = None
        self._panel_was_visible = True
        self._scale = screen_utils.system_scale()
        self._hotkey = None

        self._build_ui()
        self._load_settings()
        self._build_tray()
        self._setup_hotkey()

    # ---------- UI ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)

        # 截图模式
        grp_mode = QGroupBox("截图模式")
        mode_lay = QGridLayout(grp_mode)
        self._mode_group = QButtonGroup(self)
        for i, (value, text) in enumerate(MODE_LABELS):
            rb = QRadioButton(text)
            rb.setProperty("mode", value)
            self._mode_group.addButton(rb, i)
            mode_lay.addWidget(rb, i // 2, i % 2)
        root.addWidget(grp_mode)

        # 延时截图
        grp_delay = QGroupBox("延时截图")
        d_lay = QHBoxLayout(grp_delay)
        d_lay.addWidget(QLabel("延时"))
        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(0, 10)
        self._delay_spin.setSuffix(" 秒")
        self._delay_spin.setToolTip("0 表示立即截图；倒计时期间按 Esc 取消")
        d_lay.addWidget(self._delay_spin)
        d_lay.addWidget(QLabel("（适合截弹出菜单）"))
        d_lay.addStretch(1)
        root.addWidget(grp_delay)

        # 导出设置
        grp_fmt = QGroupBox("导出设置")
        f_lay = QGridLayout(grp_fmt)
        f_lay.addWidget(QLabel("图片格式"), 0, 0)
        self._fmt_combo = QComboBox()
        for _value, text in FORMAT_LABELS:
            self._fmt_combo.addItem(text)
        f_lay.addWidget(self._fmt_combo, 0, 1, 1, 2)
        f_lay.addWidget(QLabel("质量(JPEG/WebP)"), 1, 0)
        self._quality_slider = QSlider(Qt.Horizontal)
        self._quality_slider.setRange(1, 100)
        self._quality_val = QLabel("92")
        self._quality_slider.valueChanged.connect(
            lambda v: self._quality_val.setText(str(v)))
        f_lay.addWidget(self._quality_slider, 1, 1)
        f_lay.addWidget(self._quality_val, 1, 2)
        root.addWidget(grp_fmt)

        # 快捷键说明
        tip = QLabel("全局热键：%s\n遮罩中：拖动画形状；单击 = 智能识别窗口；"
                     "Enter 保存；Ctrl+C 复制" % HOTKEY_TEXT)
        tip.setStyleSheet("color: #666;")
        root.addWidget(tip)

        # 开始按钮
        btn = QPushButton("开始截图")
        btn.setMinimumHeight(34)
        btn.setStyleSheet(
            "QPushButton { background-color: #00A8FF; color: white;"
            " border: none; border-radius: 4px; font-weight: bold; }"
            " QPushButton:hover { background-color: #33b9ff; }"
            " QPushButton:pressed { background-color: #0090dd; }")
        btn.clicked.connect(self.start_capture)
        root.addWidget(btn)

        self._status = QLabel("就绪。关闭窗口会最小化到托盘。")
        self._status.setStyleSheet("color: #888;")
        root.addWidget(self._status)

    def _current_mode(self):
        btn = self._mode_group.button(self._mode_group.checkedId())
        return btn.property("mode") if btn else "rect"

    def _current_format(self):
        return FORMAT_LABELS[self._fmt_combo.currentIndex()][0]

    def _format_index(self, fmt):
        for i, (v, _t) in enumerate(FORMAT_LABELS):
            if v == fmt:
                return i
        return 0

    # ---------- 设置持久化 ----------

    def _load_settings(self):
        mode = self._settings.value("capture/mode", "rect")
        for btn in self._mode_group.buttons():
            if btn.property("mode") == mode:
                btn.setChecked(True)
                break
        else:
            self._mode_group.button(0).setChecked(True)
        self._delay_spin.setValue(int(self._settings.value("capture/delay", 0)))
        self._fmt_combo.setCurrentIndex(
            self._format_index(self._settings.value("export/format", "png")))
        q = int(self._settings.value("export/quality", 92))
        self._quality_slider.setValue(q)
        self._quality_val.setText(str(q))

    def _save_settings(self):
        self._settings.setValue("capture/mode", self._current_mode())
        self._settings.setValue("capture/delay", self._delay_spin.value())
        self._settings.setValue("export/format", self._current_format())
        self._settings.setValue("export/quality", self._quality_slider.value())

    def closeEvent(self, e):
        self._save_settings()
        if self.tray.isVisible():
            # 关闭 = 最小化到托盘
            e.ignore()
            self.hide()
            return
        super().closeEvent(e)

    # ---------- 托盘 ----------

    def _build_tray(self):
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("跨屏截图工具（%s）" % HOTKEY_TEXT)
        menu = QMenu()
        act_cap = menu.addAction("开始截图")
        act_cap.triggered.connect(self.start_capture)
        act_show = menu.addAction("显示面板")
        act_show.triggered.connect(self.show_panel)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # 单击托盘图标
            self.show_panel()

    def show_panel(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._save_settings()
        if self._hotkey:
            self._hotkey.stop()
        self.tray.hide()
        QApplication.quit()

    # ---------- 热键 ----------

    def _setup_hotkey(self):
        self._hotkey = GlobalHotkey(MOD_CONTROL | MOD_SHIFT | 0x4000,
                                    vk_for_char("A"))
        self._hotkey.triggered.connect(self.start_capture)
        self._hotkey.registration_failed.connect(self._on_hotkey_failed)
        self._hotkey.start()

    def _on_hotkey_failed(self, msg):
        self._status.setText(msg + "。仍可点击面板按钮截图。")

    # ---------- 截图流程 ----------

    def start_capture(self):
        if (self._overlay is not None and self._overlay.isVisible()) \
                or (self._countdown is not None and self._countdown.isVisible()):
            return  # 已在截图会话中
        self._save_settings()
        self._panel_was_visible = self.isVisible()

        delay = self._delay_spin.value()
        self.hide()
        if delay > 0:
            self._countdown = _Countdown(delay, self._scale)
            self._countdown.finished.connect(self._run_capture)
            self._countdown.cancelled.connect(self._on_countdown_cancelled)
            QTimer.singleShot(200, self._countdown.start)  # 等面板先从屏幕消失
        else:
            # 留 180ms 让面板从屏幕上消失，避免被截进图中
            QTimer.singleShot(180, self._run_capture)

    def _on_countdown_cancelled(self):
        self._countdown = None
        self._restore_panel()

    def _run_capture(self):
        self._countdown = None
        try:
            img, origin = screen_utils.grab_desktop()
        except Exception as exc:  # 抓屏失败
            self._status.setText("抓屏失败：%s" % exc)
            self._restore_panel()
            return
        quality = self._quality_slider.value()
        self._overlay = CaptureOverlay(img, origin, self._current_mode(),
                                       scale=self._scale, quality=quality)
        self._overlay.captured.connect(self._on_captured)
        self._overlay.cancelled.connect(self._on_cancelled)
        self._overlay.show_across_desktop(
            (origin[0], origin[1], img.size[0], img.size[1]))

    def _restore_panel(self):
        if self._panel_was_visible:
            self.show_panel()

    def _on_captured(self, payload):
        self._overlay = None
        action = payload.get("action")
        if action == "saved":
            self.tray.showMessage("截图已保存", payload.get("path", ""),
                                  QSystemTrayIcon.Information, 3000)
        elif action == "copied":
            self.tray.showMessage("截图已复制", "图片已复制到剪贴板，可直接粘贴",
                                  QSystemTrayIcon.Information, 3000)
        self._restore_panel()

    def _on_cancelled(self):
        self._overlay = None
        self._restore_panel()
