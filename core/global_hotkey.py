# -*- coding: utf-8 -*-
"""Windows 全局热键：后台线程 RegisterHotKey + 消息循环，触发时发 Qt 信号。

Qt 信号从工作线程发出时会自动以队列连接投递到主线程，线程安全。
"""

import ctypes
import ctypes.wintypes as wt
import threading

from PyQt5.QtCore import QObject, pyqtSignal

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32")


def vk_for_char(ch):
    """字符 -> 虚拟键码，如 'A' -> 0x41。"""
    return ord(ch.upper())


class GlobalHotkey(QObject):
    """注册一个全局热键。用法：

    hk = GlobalHotkey(MOD_CONTROL | MOD_SHIFT, vk_for_char("A"))
    hk.triggered.connect(on_hotkey)
    hk.start()
    ...
    hk.stop()
    """
    triggered = pyqtSignal()
    registration_failed = pyqtSignal(str)

    def __init__(self, modifiers, virtual_key, hotkey_id=1, parent=None):
        super().__init__(parent)
        self._modifiers = modifiers
        self._vk = virtual_key
        self._id = hotkey_id
        self._thread = None
        self._thread_id = None
        self._registered = threading.Event()

    @property
    def is_registered(self):
        return self._registered.is_set()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is not None and self._thread.is_alive():
            if self._thread_id:
                _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
        ok = _user32.RegisterHotKey(None, self._id, self._modifiers, self._vk)
        if not ok:
            err = ctypes.get_last_error()
            self._registered.clear()
            self.registration_failed.emit(
                "热键注册失败（可能被其他程序占用），错误码 %d" % err)
            return
        self._registered.set()

        msg = wt.MSG()
        while True:
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:  # 0 = WM_QUIT, -1 = 错误
                break
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                self.triggered.emit()
        _user32.UnregisterHotKey(None, self._id)
        self._registered.clear()
