# -*- coding: utf-8 -*-
"""无界面自检：验证抓屏、跨屏窗口识别、四种形状遮罩与五种格式导出全链路。

用法：python main.py --selftest
"""

import os
import tempfile
import traceback


def _check(name, cond, detail=""):
    status = "通过" if cond else "失败"
    print("  [%s] %s%s" % (status, name, (" — " + detail) if detail else ""))
    return cond


def run_selftest():
    print("== 跨屏截图工具自检 ==\n")
    ok = True
    tmpdir = tempfile.mkdtemp(prefix="screenshot_selftest_")

    # 1. 显示器枚举与虚拟桌面几何
    from core import screen_utils
    print("[1] 显示器与虚拟桌面")
    monitors = screen_utils.enum_monitor_rects()
    ok &= _check("EnumDisplayMonitors 枚举", len(monitors) >= 1,
                 "%d 台显示器 %s" % (len(monitors), monitors))
    vl, vt, vw, vh = screen_utils.virtual_desktop_rect()
    ok &= _check("虚拟桌面几何", vw > 0 and vh > 0,
                 "origin=(%d,%d) size=%dx%d" % (vl, vt, vw, vh))
    # 每台显示器都应落在虚拟桌面内
    inside = all(vl <= l and vt <= t and l + w <= vl + vw and t + h <= vt + vh
                 for l, t, w, h in monitors)
    ok &= _check("各显示器均在虚拟桌面内", inside)

    # 2. 抓屏
    print("\n[2] 桌面抓屏（物理分辨率）")
    try:
        img, origin = screen_utils.grab_desktop()
        ok &= _check("抓屏尺寸与虚拟桌面一致",
                     img.size == (vw, vh),
                     "image=%s" % (img.size,))
        extrema = img.getextrema()
        not_pure = any(lo != hi for lo, hi in extrema)
        print("    内容极值 %s（%s）" % (extrema, "正常" if not_pure else "纯色画面"))
    except Exception as exc:
        ok &= _check("抓屏", False, repr(exc))
        img, origin = None, (0, 0)

    # 3. 窗口识别
    print("\n[3] 跨屏窗口识别")
    from core import window_detector
    found_any = False
    for l, t, w, h in monitors:
        win = window_detector.get_window_at(l + w // 2, t + h // 2)
        if win is not None:
            found_any = True
            print("    屏幕(%d,%d): %r %s" % (l, t, win.title[:40], win.rect))
    ok &= _check("至少识别到一个窗口", found_any)

    # 4. 四种形状遮罩
    print("\n[4] 形状遮罩提取")
    from PIL import Image
    from core import image_exporter
    if img is None:
        img = Image.new("RGB", (800, 600), (200, 100, 50))
        origin = (0, 0)
    if img.mode != "RGB":
        img = img.convert("RGB")

    bbox = (10, 10, 200, 120)
    # 菱形套索：顶点在四边中点，保证 bbox 四角在多边形外（可检验异形遮罩）
    lasso_poly = [(110, 10), (210, 70), (110, 130), (10, 70)]
    cases = [
        ("rect", bbox, None),
        ("window", bbox, None),
        ("ellipse", bbox, None),
        ("circle", (10, 10, 120, 120), None),
        ("lasso", (10, 10, 200, 120), lasso_poly),
    ]
    for shape, bb, poly in cases:
        out = image_exporter.extract_selection(img, shape, bb, poly)
        alpha = out.split()[3]
        cx, cy = out.width // 2, out.height // 2
        center_opaque = alpha.getpixel((cx, cy)) > 250
        if shape in ("rect", "window"):
            corner_clear = alpha.getpixel((0, 0)) > 250
        else:
            corner_clear = alpha.getpixel((0, 0)) < 5  # 圆/套索四角应透明
        ok &= _check("遮罩 %s" % shape,
                     out.size == (bb[2], bb[3]) and center_opaque and corner_clear,
                     "size=%s" % (out.size,))

    # 5. 五种格式导出
    print("\n[5] 五种格式导出")
    out = image_exporter.extract_selection(img, "ellipse", bbox)
    for ext, fmt, alpha_ok in image_exporter.FORMATS:
        path = os.path.join(tmpdir, "test.%s" % ext)
        try:
            image_exporter.save_image(out, path, quality=85)
            rim = Image.open(path)
            rim.load()
            ok &= _check("导出 %s" % fmt.upper(),
                         os.path.getsize(path) > 0
                         and rim.size == out.size,
                         "%d bytes" % os.path.getsize(path))
        except Exception as exc:
            ok &= _check("导出 %s" % fmt.upper(), False, repr(exc))

    # 6. GUI 模块可导入（不显示窗口）
    print("\n[6] GUI 模块导入")
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from core.capture_overlay import CaptureOverlay  # noqa: F401
        from core.main_window import MainWindow  # noqa: F401
        from core.image_exporter import ExportDialog  # noqa: F401
        ok &= _check("capture_overlay / main_window / ExportDialog 导入", True)
        # PIL <-> QImage 转换
        small = Image.new("RGBA", (8, 6), (255, 0, 0, 128))
        qi = image_exporter.pil_to_qimage(small)
        ok &= _check("PIL->QImage 转换",
                     qi.width() == 8 and qi.height() == 6
                     and qi.pixelColor(0, 0).alpha() == 128)
    except Exception as exc:
        traceback.print_exc()
        ok &= _check("GUI 模块导入", False, repr(exc))

    print("\n== 结果：%s ==" % ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1
