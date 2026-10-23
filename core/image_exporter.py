# -*- coding: utf-8 -*-
"""选区提取、异形遮罩与多格式导出。

遮罩用 4x 超采样 + LANCZOS 缩放生成，保证椭圆/套索边缘平滑抗锯齿。
所有坐标均为物理像素（桌面抓屏图像的像素坐标）。
"""

import datetime
import os

from PIL import Image, ImageDraw
from PyQt5.QtWidgets import QFileDialog

# 支持的导出格式：扩展名 -> (PIL 格式名, 是否支持透明)
FORMATS = [
    ("png", "PNG", True),
    ("jpg", "JPEG", False),
    ("bmp", "BMP", False),
    ("webp", "WEBP", True),
    ("tif", "TIFF", True),
]

_FILTER = "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg);;BMP 位图 (*.bmp);;WebP 图片 (*.webp);;TIFF 图片 (*.tif *.tiff)"

_SUPER_SAMPLE = 4  # 遮罩超采样倍数


def extract_selection(full_img, shape, bbox, polygon=None):
    """从桌面大图中按形状提取选区，返回带透明通道的 RGBA 图像。

    shape: "rect" / "window" / "ellipse" / "circle" / "lasso"
    bbox:  选区外接矩形 (left, top, w, h)，桌面物理坐标
    polygon: lasso 模式的顶点列表 [(x, y), ...]，桌面物理坐标
    """
    l, t, w, h = bbox
    w = max(1, int(w))
    h = max(1, int(h))
    crop = full_img.crop((l, t, l + w, t + h)).convert("RGBA")

    mask = Image.new("L", (w * _SUPER_SAMPLE, h * _SUPER_SAMPLE), 0)
    draw = ImageDraw.Draw(mask)
    s = _SUPER_SAMPLE

    if shape in ("rect", "window"):
        draw.rectangle([0, 0, w * s - 1, h * s - 1], fill=255)
    elif shape in ("ellipse", "circle"):
        draw.ellipse([0, 0, w * s - 1, h * s - 1], fill=255)
    elif shape == "lasso":
        pts = [((px - l) * s, (py - t) * s) for px, py in polygon]
        if len(pts) >= 3:
            draw.polygon(pts, fill=255)
        else:
            draw.rectangle([0, 0, w * s - 1, h * s - 1], fill=255)
    else:
        draw.rectangle([0, 0, w * s - 1, h * s - 1], fill=255)

    mask = mask.resize((w, h), Image.LANCZOS)
    crop.putalpha(mask)
    return crop


def flatten_for_format(img, pil_format):
    """不支持透明的格式（JPEG/BMP）铺白底后转 RGB。"""
    if pil_format in ("JPEG", "BMP"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    return img


def save_image(img, path, quality=92):
    """按扩展名保存图像。quality 对 JPEG/WEBP 生效。返回实际格式名。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    fmt_map = {e: (f, alpha) for e, f, alpha in FORMATS}
    ext_alias = {"jpeg": "jpg", "tiff": "tif"}
    ext = ext_alias.get(ext, ext)
    if ext not in fmt_map:
        ext = "png"
    pil_format, _supports_alpha = fmt_map[ext]
    out = flatten_for_format(img, pil_format)
    params = {}
    if pil_format in ("JPEG", "WEBP"):
        params["quality"] = int(quality)
    if pil_format == "JPEG":
        params["subsampling"] = 0  # 4:4:4，减少彩色文字的色度糊化
    out.save(path, pil_format, **params)
    return pil_format


def default_save_dir():
    """默认保存目录：用户的图片文件夹。"""
    pics = os.path.join(os.path.expanduser("~"), "Pictures")
    return pics if os.path.isdir(pics) else os.path.expanduser("~")


def default_filename(ext="png"):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return "截图_%s.%s" % (stamp, ext)


def pil_to_qimage(img):
    """PIL RGBA -> QImage（复制数据，避免悬空缓冲）。"""
    from PyQt5.QtGui import QImage
    w, h = img.size
    data = img.tobytes("raw", "BGRA")
    qimg = QImage(data, w, h, w * 4, QImage.Format_ARGB32)
    return qimg.copy()  # copy() 使 QImage 持有独立数据


def copy_image_to_clipboard(img):
    """把 RGBA 图像复制到系统剪贴板。"""
    from PyQt5.QtWidgets import QApplication
    QApplication.clipboard().setImage(pil_to_qimage(img))


def _format_for_path(path):
    """根据扩展名推断 (PIL 格式, 支持透明)。未知扩展名按 PNG 处理。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    ext_alias = {"jpeg": "jpg", "tiff": "tif"}
    ext = ext_alias.get(ext, ext)
    for e, f, alpha in FORMATS:
        if e == ext:
            return f, alpha
    return "PNG", True


class ExportDialog(QFileDialog):
    """保存对话框：QFileDialog + 底部质量滑条（对 JPEG/WEBP 生效）。

    用非原生对话框以便嵌入质量控件；accept() 后通过
    selectedFiles() / pilFormat / quality 属性读取结果。
    """

    def __init__(self, parent=None, quality=92, select_name=None):
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QSlider, QWidget

        super().__init__(parent)
        self.setWindowTitle("保存截图")
        self.setOption(QFileDialog.DontUseNativeDialog, True)
        self.setAcceptMode(QFileDialog.AcceptSave)
        self.setNameFilter(_FILTER)
        self.setDirectory(default_save_dir())
        self.selectFile(select_name or default_filename())

        self._quality = int(quality)

        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        cap = QLabel("质量(仅 JPEG/WebP):")
        slider = QSlider(Qt.Horizontal)
        slider.setRange(1, 100)
        slider.setValue(self._quality)
        val = QLabel(str(self._quality))
        slider.valueChanged.connect(self._on_slider)
        self._val_label = val
        for wdg in (cap, slider, val):
            lay.addWidget(wdg)

        grid = self.layout()
        if isinstance(grid, QGridLayout):
            grid.addWidget(row, grid.rowCount(), 0, 1, max(1, grid.columnCount()))

        ext_by_filter = {"PNG": "png", "JPEG": "jpg", "BMP": "bmp",
                         "WebP": "webp", "TIFF": "tif"}
        self.filterSelected.connect(
            lambda name: self.setDefaultSuffix(next(
                (ext for prefix, ext in ext_by_filter.items()
                 if name.startswith(prefix)), "png")))

    def _on_slider(self, v):
        self._quality = int(v)
        self._val_label.setText(str(v))

    @property
    def quality(self):
        return self._quality

    @property
    def pilFormat(self):
        path = self.selectedFiles()[0] if self.selectedFiles() else ""
        fmt, _alpha = _format_for_path(path)
        return fmt
