# -*- coding: utf-8 -*-
"""跨屏截图工具入口。

用法：
    python main.py              # 启动图形界面
    python main.py --selftest   # 运行无界面自检（抓屏/窗口识别/遮罩/导出全链路）
"""

import sys


def main():
    # DPI 感知必须最先设置：在创建 QApplication 之前
    from core import screen_utils
    screen_utils.apply_dpi_awareness()
    screen_utils.apply_qt_attributes()

    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("CrossScreenCapture")
    app.setOrganizationName("ZcodeTool")
    app.setQuitOnLastWindowClosed(False)  # 关闭面板 = 最小化到托盘

    # 面板 UI 按系统缩放手动放大字号（Qt 缩放已禁用）
    scale = screen_utils.system_scale()
    if scale > 1.0:
        f = app.font()
        f.setPixelSize(max(12, round(12 * scale)))
        app.setFont(f)

    if "--selftest" in sys.argv:
        from core.selftest import run_selftest
        return run_selftest()

    from core.main_window import MainWindow
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
