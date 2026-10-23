# -*- coding: utf-8 -*-
"""Win32 窗口识别：按 Z 序枚举顶级窗口，找到光标下最上层的可视窗口。

进程为 Per-Monitor DPI Aware，GetWindowRect / DWM 扩展边框返回的均为物理像素，
与虚拟桌面抓屏图像 1:1 对应，天然支持多显示器。
"""

import ctypes
import ctypes.wintypes as wt
import os

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_dwmapi = None
try:
    _dwmapi = ctypes.WinDLL("dwmapi")
except OSError:
    pass

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

# 桌面壁纸窗口：点击空白桌面时不做窗口捕获（任务栏 Shell_TrayWnd 保留可捕获）
_SKIP_CLASSES = {"Progman", "WorkerW"}

# self process
_OWN_PID = os.getpid()


class WindowInfo:
    __slots__ = ("hwnd", "left", "top", "width", "height", "title")

    def __init__(self, hwnd, left, top, width, height, title):
        self.hwnd = hwnd
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        self.title = title

    @property
    def rect(self):
        return self.left, self.top, self.width, self.height

    def contains(self, x, y):
        return (self.left <= x < self.left + self.width
                and self.top <= y < self.top + self.height)

    def __repr__(self):
        return "WindowInfo(%r, %s)" % (self.title, self.rect)


def _get_ex_style(hwnd):
    return _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) or 0


def _get_rect(hwnd):
    """窗口可视边界（物理像素）。优先 DWM 扩展边框（去掉不可见阴影边距）。"""
    rect = wt.RECT()
    if _dwmapi is not None:
        hr = _dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect))
        if hr == 0:
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    if _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return None


def _is_cloaked(hwnd):
    """UWP 挂起等被遮蔽(cloaked)的窗口实际不可见，需跳过。"""
    if _dwmapi is None:
        return False
    val = ctypes.c_uint(0)
    _dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), DWMWA_CLOAKED,
                                  ctypes.byref(val), ctypes.sizeof(val))
    return val.value != 0


def _get_title(hwnd):
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _get_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _enum_top_level_windows():
    """按 Z 序（最上层在前）枚举顶级窗口 hwnd 列表。"""
    hwnds = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        hwnds.append(hwnd)
        return 1

    _user32.EnumWindows(proto(_cb), 0)
    return hwnds


def _is_valid_target(hwnd):
    """是否可能是可捕获窗口（位置无关的快速过滤）。"""
    if not _user32.IsWindowVisible(hwnd):
        return False
    if _user32.IsIconic(hwnd):  # 最小化
        return False
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == _OWN_PID:  # 自己的遮罩层/面板
        return False
    ex = _get_ex_style(hwnd)
    if ex & WS_EX_TOOLWINDOW or ex & WS_EX_TRANSPARENT:
        return False
    if _is_cloaked(hwnd):
        return False
    if _get_class_name(hwnd) in _SKIP_CLASSES:
        return False
    return True


def get_window_at(x, y):
    """返回包含物理坐标 (x, y) 的最上层窗口；无则 None。"""
    for hwnd in _enum_top_level_windows():
        if not _is_valid_target(hwnd):
            continue
        r = _get_rect(hwnd)
        if r is None:
            continue
        l, t, w, h = r
        if w <= 0 or h <= 0:
            continue
        if l <= x < l + w and t <= y < t + h:
            return WindowInfo(hwnd, l, t, w, h, _get_title(hwnd))
    return None


def get_window_rect(hwnd):
    """指定 hwnd 的可视边界（物理像素），失败返回 None。"""
    return _get_rect(hwnd)
