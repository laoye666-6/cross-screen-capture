# -*- coding: utf-8 -*-
"""屏幕与 DPI 相关工具。

核心约定：进程以 Per-Monitor DPI Aware 运行，且禁用 Qt 高分屏缩放，
因此 Qt 逻辑坐标 == Windows 物理像素坐标，与 Win32 API 和抓屏图像完全 1:1。
虚拟桌面原点可能为负（主屏左侧/上方的显示器），所有计算需保留符号。
"""

import ctypes
import ctypes.wintypes as wt

from PyQt5.QtCore import Qt

# Windows 常量
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)
MDT_EFFECTIVE_DPI = 0

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shcore = None
try:
    _shcore = ctypes.WinDLL("shcore")
except OSError:
    pass

_dpi_applied = False


def apply_dpi_awareness():
    """在创建 QApplication 之前调用：让进程按物理像素感知 DPI。

    逐级回退：Per-Monitor V2 (Win10 1703+) -> Per-Monitor (Win8.1+) -> System (Vista+)。
    由我们抢先设置后，Qt 初始化时设置 DPI 感知会静默失败，不再二次改写。
    """
    global _dpi_applied
    if _dpi_applied:
        return
    if hasattr(_user32, "SetProcessDpiAwarenessContext"):
        # 成功返回非零；失败（已设置过）则继续回退
        if _user32.SetProcessDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            _dpi_applied = True
            return
    if _shcore is not None and hasattr(_shcore, "SetProcessDpiAwareness"):
        # 2 == PROCESS_PER_MONITOR_DPI_AWARE
        if _shcore.SetProcessDpiAwareness(2) == 0:
            _dpi_applied = True
            return
    _user32.SetProcessDPIAware()
    _dpi_applied = True


def apply_qt_attributes():
    """设置 Qt 属性：禁用高分屏自动缩放。须在 QApplication 创建前调用。"""
    from PyQt5.QtCore import QCoreApplication, Qt
    if hasattr(Qt, "AA_DisableHighDpiScaling"):
        QCoreApplication.setAttribute(Qt.AA_DisableHighDpiScaling)
    QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)


def virtual_desktop_rect():
    """整个虚拟桌面矩形（物理像素），原点可为负。返回 (left, top, width, height)。"""
    left = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, width, height


def enum_monitor_rects():
    """枚举所有显示器，返回物理像素矩形列表 [(left, top, width, height), ...]。"""
    rects = []

    _MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(wt.RECT), ctypes.c_double)

    def _callback(_hmonitor, _hdc, lprect, _data):
        r = lprect.contents
        rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1  # 继续枚举

    _user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(_callback), 0)
    return rects


def monitor_scale(hmonitor=None):
    """取显示器缩放率（96 DPI 为 100%）。hmonitor 为 None 时取主显示器。"""
    if _shcore is None or not hasattr(_shcore, "GetDpiForMonitor"):
        return 1.0
    try:
        if hmonitor is None:
            rects = enum_monitor_rects()
            if not rects:
                return 1.0
            l, t, w, h = rects[0]
            pt = wt.POINT(l + w // 2, t + h // 2)
            hmonitor = _user32.MonitorFromPoint(pt, 1)  # MONITOR_DEFAULTTOPRIMARY
        dpi_x = ctypes.c_uint(96)
        dpi_y = ctypes.c_uint(96)
        _shcore.GetDpiForMonitor(ctypes.c_void_p(hmonitor), MDT_EFFECTIVE_DPI,
                                 ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        return dpi_x.value / 96.0
    except Exception:
        return 1.0


def system_scale():
    """系统级缩放率，用于手动放大控制面板 UI。"""
    try:
        hdc = _user32.GetDC(0)
        gdi32 = ctypes.WinDLL("gdi32")
        dpi = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        _user32.ReleaseDC(0, hdc)
        if dpi and dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def grab_desktop():
    """抓取整个虚拟桌面，返回 (PIL.Image, (left, top))，图像像素与物理坐标 1:1。

    优先用 Pillow 的 ImageGrab；若结果尺寸与虚拟桌面不符（个别驱动/多屏环境），
    回退为 ctypes BitBlt 逐屏抓取后拼接。
    """
    left, top, width, height = virtual_desktop_rect()
    if width <= 0 or height <= 0:
        raise RuntimeError("无法获取虚拟桌面尺寸")

    img = None
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        if img.size != (width, height):
            img = None
    except Exception:
        img = None

    if img is None:
        img = _bitblt_grab(left, top, width, height)
    return img, (left, top)


def _bitblt_grab(left, top, width, height):
    """ctypes BitBlt 抓屏兜底：直接从屏幕 DC 复制物理像素。"""
    from PIL import Image
    gdi32 = ctypes.WinDLL("gdi32")

    SRCCOPY = 0x00CC0020
    BI_RGB = 0
    DIB_RGB_COLORS = 0

    hdc_screen = _user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32),
                    ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                    ("bmiColors", ctypes.c_uint32 * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # 负值 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), ctypes.c_uint]
    gdi32.SelectObject(hdc_mem, gdi32.CreateCompatibleBitmap(hdc_screen, width, height))
    gdi32.BitBlt(ctypes.c_void_p(hdc_mem), 0, 0, width, height,
                 ctypes.c_void_p(hdc_screen), left, top, SRCCOPY)
    gdi32.GetDIBits(hdc_mem, gdi32.GetCurrentObject(hdc_mem, 7), 0, height,
                    buf, ctypes.byref(bmi), DIB_RGB_COLORS)  # 7 = OBJ_BITMAP
    _user32.ReleaseDC(0, hdc_screen)
    gdi32.DeleteDC(hdc_mem)

    # GetDIBits 32bpp 的内存字节序为 B,G,R,X，必须按 BGRA 解析，否则红蓝互换
    img = Image.frombuffer("RGBA", (width, height), buf.raw, "raw", "BGRA", 0, 1)
    return img.convert("RGB")
