# -*- coding: utf-8 -*-
"""全屏截图遮罩层：一个覆盖整个虚拟桌面（可跨多显示器）的置顶无边框窗口。

坐标约定：遮罩层窗口左上角 == 虚拟桌面左上角 == 抓屏图像 (0,0)。
由于进程为 Per-Monitor DPI Aware 且禁用 Qt 缩放，Qt 局部坐标即物理像素，
事件 pos() 可直接用于索引桌面图像，选区可跨任意显示器绘制。

交互：
- 移动鼠标：高亮光标下的窗口（含标题提示）
- 按下拖动：手动绘制当前形状（矩形/圆形/椭圆/套索），可跨屏
- 单击不拖：智能捕获高亮窗口（窗口截图）
- 选区完成：悬浮工具条（保存/复制/重选/取消）；Enter 保存、Ctrl+C 复制、Esc 取消
"""

import time

from PyQt5.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QImage, QPainter, QPainterPath,
                         QPen, QPolygonF)
from PyQt5.QtWidgets import QDialog, QPushButton, QWidget

from . import image_exporter, window_detector

# 压暗程度（alpha 0-255）
DIM_ALPHA = 110
BORDER_COLOR = QColor(0, 168, 255)
TOOLBAR_H = 36

MODES = ("rect", "circle", "ellipse", "lasso", "window")

STATE_IDLE = 0      # 待选择：跟随高亮窗口
STATE_DRAWING = 1   # 拖动绘制中
STATE_SELECTED = 2  # 选区完成：显示工具条

MIN_CLICK_MOVE = 3   # 位移超过该像素视为拖动
MIN_SELECTION = 4    # 选区小于该尺寸视为误触


def _pil_to_qimage(img):
    """PIL RGB -> QImage（复制数据）。"""
    w, h = img.size
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, w, h, w * 3, QImage.Format_RGB888)
    return qimg.copy()


class _Toolbar(QWidget):
    """选区完成后的悬浮工具条。"""
    save_clicked = pyqtSignal()
    copy_clicked = pyqtSignal()
    redo_clicked = pyqtSignal()
    cancel_clicked = pyqtSignal()

    def __init__(self, parent, font_px):
        super().__init__(parent)
        self.setObjectName("capToolbar")
        self.setStyleSheet(
            "#capToolbar { background-color: rgba(40,40,40,242);"
            " border: 1px solid rgba(255,255,255,30); border-radius: 4px; }"
            " QPushButton { color: white; background: transparent;"
            " border: none; padding: 2px 12px; }"
            " QPushButton:hover { background-color: rgba(255,255,255,36);"
            " border-radius: 3px; }")
        for text, sig in (("保存", self.save_clicked), ("复制", self.copy_clicked),
                          ("重选", self.redo_clicked), ("取消", self.cancel_clicked)):
            btn = QPushButton(text, self)
            btn.setCursor(Qt.PointingHandCursor)
            f = btn.font()
            f.setPixelSize(font_px)
            btn.setFont(f)
            btn.clicked.connect(sig.emit)
        self._relayout()

    def _relayout(self):
        # sizeHint 在带 stylesheet padding 时会低估宽度，改按字体度量显式计算
        x = 0
        for btn in self.findChildren(QPushButton):
            w = btn.fontMetrics().horizontalAdvance(btn.text()) + 28
            btn.setFixedSize(w, TOOLBAR_H)
            btn.move(x, 0)
            x += w
        self.setFixedSize(max(1, x), TOOLBAR_H)


class CaptureOverlay(QWidget):
    captured = pyqtSignal(dict)   # {"shape","bbox","polygon","image","origin","action"}
    cancelled = pyqtSignal()

    def __init__(self, desktop_image, origin, mode, scale=1.0, quality=92,
                 parent=None):
        super().__init__(parent)
        if mode not in MODES:
            mode = "rect"
        self._img = desktop_image                     # PIL RGB，虚拟桌面物理分辨率
        self._origin = origin                         # 虚拟桌面全局原点（可为负）
        self._mode = mode
        self._scale = scale                           # 系统缩放，用于字号
        self._quality = quality                       # 默认导出质量
        self._img_bright = _pil_to_qimage(desktop_image)
        self._img_dim = _pil_to_qimage(desktop_image)
        p = QPainter(self._img_dim)
        p.fillRect(self._img_dim.rect(), QColor(0, 0, 0, DIM_ALPHA))
        p.end()

        self._state = STATE_IDLE
        self._press_pos = None        # QPoint，图像坐标
        self._cur_pos = None
        self._shape_bbox = None       # (l, t, w, h) 图像坐标
        self._polygon = None          # lasso 顶点列表
        self._result_shape = None     # 实际生效的形状（窗口捕获时为 "window"）
        self._hover_win = None        # WindowInfo
        self._last_detect = 0.0
        font_px = max(11, round(12 * scale))
        self._toolbar = _Toolbar(self, font_px)
        self._toolbar.save_clicked.connect(self.save_dialog)
        self._toolbar.copy_clicked.connect(self.copy_clipboard)
        self._toolbar.redo_clicked.connect(self._reset_selection)
        self._toolbar.cancel_clicked.connect(self.cancel)
        self._toolbar.hide()

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    # ---------- 生命周期 ----------

    def show_across_desktop(self, desktop_rect):
        l, t, w, h = desktop_rect
        self.setGeometry(l, t, w, h)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def cancel(self):
        self.cancelled.emit()
        self.close()

    # ---------- 形状几何 ----------

    def _norm_bbox(self, p1, p2):
        """两点 -> 正规化 bbox（图像坐标）。circle 模式约束为正方形。"""
        dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
        if self._mode == "circle":
            side = max(abs(dx), abs(dy))
            dx = side if dx >= 0 else -side
            dy = side if dy >= 0 else -side
        x1, y1 = p1.x(), p1.y()
        x2, y2 = x1 + dx, y1 + dy
        l, t = min(x1, x2), min(y1, y2)
        return (l, t, abs(x2 - x1), abs(y2 - y1))

    def _shape_path(self, bbox, polygon, closed=True):
        """按形状构建 QPainterPath（图像坐标 == 本窗口局部坐标）。"""
        path = QPainterPath()
        if bbox is None:
            return path
        l, t, w, h = (float(v) for v in bbox)
        if self._mode in ("rect", "window"):
            path.addRect(l, t, w, h)
        elif self._mode in ("ellipse", "circle"):
            path.addEllipse(l, t, w, h)
        elif self._mode == "lasso" and polygon:
            path.addPolygon(QPolygonF([QPoint(*pt) for pt in polygon]))
            if closed:
                path.closeSubpath()
        return path

    # ---------- 事件处理 ----------

    def mouseMoveEvent(self, e):
        pos = e.pos()
        self._cur_pos = pos
        if self._state == STATE_IDLE:
            # 节流窗口检测，避免高频 EnumWindows
            now = time.perf_counter()
            if now - self._last_detect > 0.04:
                self._last_detect = now
                self._hover_win = window_detector.get_window_at(
                    pos.x() + self._origin[0], pos.y() + self._origin[1])
            self.update()
        elif self._state == STATE_DRAWING:
            if self._mode == "lasso":
                if (self._polygon
                        and (abs(pos.x() - self._polygon[-1][0])
                             + abs(pos.y() - self._polygon[-1][1])) >= 2):
                    self._polygon.append((pos.x(), pos.y()))
                    xs = [p[0] for p in self._polygon]
                    ys = [p[1] for p in self._polygon]
                    self._shape_bbox = (min(xs), min(ys),
                                        max(xs) - min(xs), max(ys) - min(ys))
            else:
                self._shape_bbox = self._norm_bbox(self._press_pos, pos)
            self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.pos()
        self._press_pos = pos
        self._cur_pos = pos
        self._polygon = [(pos.x(), pos.y())] if self._mode == "lasso" else None
        self._shape_bbox = None
        self._result_shape = None
        self._state = STATE_DRAWING  # 释放时若无位移则按单击处理
        self._toolbar.hide()
        self.setCursor(Qt.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._state != STATE_DRAWING:
            return
        pos = e.pos()
        moved = (abs(pos.x() - self._press_pos.x()) >= MIN_CLICK_MOVE
                 or abs(pos.y() - self._press_pos.y()) >= MIN_CLICK_MOVE)

        if not moved or self._mode == "window":
            # 单击：智能捕获窗口；点在空白桌面则取消（窗口模式不支持手动绘制）
            win = window_detector.get_window_at(
                pos.x() + self._origin[0], pos.y() + self._origin[1])
            if win is None:
                if not moved:
                    self.cancel()
                else:
                    self._reset_selection()
                return
            self._apply_window(win)
            self._finish_selection()
            return

        bbox = self._shape_bbox
        if (bbox is None or bbox[2] < MIN_SELECTION or bbox[3] < MIN_SELECTION
                or (self._mode == "lasso" and len(self._polygon) < 3)):
            self._reset_selection()
            return
        self._result_shape = self._mode
        self._finish_selection()

    def _apply_window(self, win):
        """记录窗口选区（全局物理坐标 -> 图像坐标）。"""
        gx, gy = self._origin
        self._shape_bbox = (win.left - gx, win.top - gy, win.width, win.height)
        self._polygon = None
        self._result_shape = "window"
        self._hover_win = win

    def _finish_selection(self):
        self._state = STATE_SELECTED
        self.setCursor(Qt.ArrowCursor)
        self._place_toolbar()
        self._toolbar.show()
        self._toolbar.raise_()
        self.update()

    def _reset_selection(self):
        self._state = STATE_IDLE
        self._shape_bbox = None
        self._polygon = None
        self._press_pos = None
        self._toolbar.hide()
        self.setCursor(Qt.CrossCursor)
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancel()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._state == STATE_SELECTED:
                self.save_dialog()
        elif e.key() == Qt.Key_C and e.modifiers() & Qt.ControlModifier:
            if self._state == STATE_SELECTED:
                self.copy_clipboard()
        else:
            super().keyPressEvent(e)

    # ---------- 工具条定位 ----------

    def _place_toolbar(self):
        l, t, w, h = self._shape_bbox
        vw, vh = self.width(), self.height()
        x = l
        y = t + h + 6
        if y + self._toolbar.height() > vh:
            y = t - 6 - self._toolbar.height()
            if y < 0:
                y = max(0, vh - self._toolbar.height() - 6)
        if x + self._toolbar.width() > vw:
            x = l + w - self._toolbar.width()
        x = max(0, min(x, vw - self._toolbar.width()))
        self._toolbar.move(x, y)

    # ---------- 绘制 ----------

    def paintEvent(self, _e):
        painter = QPainter(self)
        # 全屏压暗底图
        painter.drawImage(0, 0, self._img_dim)

        selected = self._state == STATE_SELECTED
        drawing = self._state == STATE_DRAWING

        if drawing or selected:
            bbox = self._shape_bbox
            poly = self._polygon
            closed = self._mode != "lasso"
        else:
            bbox = poly = None

        if bbox is not None:
            path = self._shape_path(bbox, poly, closed)
            # 选区内透出原亮度画面
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, self._mode != "rect")
            painter.setClipPath(path)
            painter.drawImage(0, 0, self._img_bright)
            painter.restore()

            # 边框
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, self._mode != "rect")
            painter.setPen(QPen(BORDER_COLOR, 2))
            if self._mode == "lasso" and poly:
                painter.drawPolygon(QPolygonF([QPoint(*pt) for pt in poly]))
            else:
                painter.drawPath(path)
            painter.restore()

            if drawing:
                self._draw_size_label(painter, bbox)

        # 待选状态：高亮光标下窗口
        if self._state == STATE_IDLE and self._hover_win is not None:
            gx, gy = self._origin
            r = QRect(self._hover_win.left - gx, self._hover_win.top - gy,
                      self._hover_win.width, self._hover_win.height)
            painter.save()
            painter.setPen(QPen(BORDER_COLOR, 2))
            painter.drawRect(r)
            painter.restore()
            if self._hover_win.title:
                self._draw_tooltip(painter, r.topLeft(), self._hover_win.title)

    def _draw_size_label(self, painter, bbox):
        l, t, w, h = bbox
        text = "%d × %d" % (int(w), int(h))
        painter.setFont(self._ui_font(12, True))
        fm = painter.fontMetrics()
        tw, th = fm.horizontalAdvance(text) + 12, fm.height() + 6
        x = l + w - tw
        y = t - th - 4
        if y < 0:
            y = t + h + 4
        x = max(0, min(x, self.width() - tw))
        painter.fillRect(x, y, tw, th, QColor(0, 168, 255, 230))
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(x + 6, y + fm.ascent() + 3, text)

    def _draw_tooltip(self, painter, topleft, text):
        painter.setFont(self._ui_font(12, False))
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text) + 12
        th = fm.height() + 6
        x = max(0, topleft.x())
        y = max(0, topleft.y() - th - 6)
        painter.fillRect(x, y, tw, th, QColor(0, 0, 0, 200))
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(x + 6, y + fm.ascent() + 3, text)

    def _ui_font(self, px, bold=False):
        f = QFont()
        f.setPixelSize(max(11, round(px * self._scale)))
        f.setBold(bold)
        return f

    # ---------- 结果 ----------

    def _selection_image(self):
        return image_exporter.extract_selection(
            self._img, self._result_shape, self._shape_bbox, self._polygon)

    def _payload(self, action):
        return {"shape": self._result_shape,
                "bbox": self._shape_bbox,
                "polygon": list(self._polygon) if self._polygon else None,
                "image": self._img,
                "origin": self._origin,
                "action": action}

    def save_dialog(self):
        if self._state != STATE_SELECTED:
            return
        self.hide()  # 对话框期间不再遮挡屏幕
        dlg = image_exporter.ExportDialog(
            None, quality=self._quality,
            select_name=image_exporter.default_filename())
        accepted = dlg.exec_() == QDialog.Accepted
        paths = dlg.selectedFiles() if accepted else []
        if paths and paths[0]:
            image_exporter.save_image(self._selection_image(), paths[0],
                                      dlg.quality)
            payload = self._payload("saved")
            payload["path"] = paths[0]
            self.captured.emit(payload)
            self.close()
            return
        # 对话框被取消：回到选区状态
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def copy_clipboard(self):
        if self._state != STATE_SELECTED:
            return
        image_exporter.copy_image_to_clipboard(self._selection_image())
        self.captured.emit(self._payload("copied"))
        self.close()
