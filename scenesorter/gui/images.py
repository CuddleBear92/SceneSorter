"""Memory-safe image handling.

Decoded images are huge compared to the files: a 1280x720 JPEG of ~15 KB is
3.5 MB as a pixmap, a 4000x6000 portrait ~96 MB. So:

* decode straight to the size we display (QImageReader.setScaledSize — the
  full-size image is never materialised),
* keep only the most recently used pixmaps in memory (LRU),
* store the downscaled version on disk, and cap the disk cache.
"""
from __future__ import annotations

import os
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

SCENE_SIZE = QSize(640, 360)      # cards are 260x146 (520x292 on 2x screens), detail panel 360x210
PORTRAIT_SIZE = QSize(400, 600)
LOGO_SIZE = QSize(440, 220)


class PixmapLRU:
    """Tiny LRU for decoded pixmaps. None values (= "no image") are cached too."""

    def __init__(self, max_items: int):
        self.max_items = max_items
        self._d: OrderedDict[str, QPixmap | None] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def get(self, key: str):
        pm = self._d.get(key)
        if key in self._d:
            self._d.move_to_end(key)
        return pm

    def put(self, key: str, pm: QPixmap | None) -> None:
        self._d[key] = pm
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)

    def pop(self, key: str, default=None):
        return self._d.pop(key, default)

    def __len__(self) -> int:
        return len(self._d)


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file + rename, so a crash/close never leaves half an image."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def is_svg(data: bytes) -> bool:
    head = data[:300].lstrip().lower()
    return head.startswith(b"<svg") or head.startswith(b"<?xml")


def decode_scaled(data: bytes, box: QSize) -> QImage | None:
    """Decode bytes into an image that fits inside ``box`` (aspect kept, never upscaled)."""
    if is_svg(data):
        r = QSvgRenderer(QByteArray(data))
        if not r.isValid():
            return None
        size = r.defaultSize()
        if size.isEmpty():
            size = QSize(box)
        size.scale(box, Qt.AspectRatioMode.KeepAspectRatio)
        img = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        r.render(p)
        p.end()
        return img
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and (size.width() > box.width() or size.height() > box.height()):
        size.scale(box, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(size)
    img = reader.read()
    return None if img.isNull() else img


def encode(img: QImage) -> bytes:
    """Small file for the disk cache: JPEG, or PNG when the image has transparency (logos)."""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG" if img.hasAlphaChannel() else "JPG", -1 if img.hasAlphaChannel() else 85)
    return bytes(ba)


def load_file(path: Path, box: QSize, shrink_over: int = 200_000) -> QPixmap | None:
    """Load a cached file at display size. Files cached by older builds at full size
    are rewritten downscaled the first time they're read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    img = decode_scaled(data, box)
    if img is None:
        return None
    if len(data) > shrink_over and not is_svg(data):
        try:
            atomic_write(path, encode(img))
        except OSError:
            pass
    return QPixmap.fromImage(img)


def save_and_load(path: Path, data: bytes, box: QSize) -> QPixmap | None:
    """Downscale freshly downloaded bytes, store the small version, return the pixmap."""
    img = decode_scaled(data, box)
    if img is None:
        return None
    try:
        atomic_write(path, data if is_svg(data) else encode(img))  # SVGs are tiny and stay sharp
    except OSError:
        pass
    return QPixmap.fromImage(img)


def prune_cache(folders: list[Path], max_bytes: int) -> tuple[int, int]:
    """Delete least-recently-modified files until the folders fit in max_bytes.
    Returns (bytes before, bytes after)."""
    files = []
    for folder in folders:
        if folder.is_dir():
            for f in folder.iterdir():
                try:
                    st = f.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, f))
    total = before = sum(s for _m, s, _f in files)
    if total > max_bytes:
        for _m, size, f in sorted(files):  # oldest first
            try:
                f.unlink()
                total -= size
            except OSError:
                continue
            if total <= max_bytes * 0.9:  # leave some headroom so we don't prune every start
                break
    return before, total


def touch(path: Path) -> None:
    """Mark a cache file as recently used (for pruning)."""
    try:
        now = time.time()
        os.utime(path, (now, now))
    except OSError:
        pass


def shrink_for_disk(data: bytes, box: QSize) -> bytes:
    """Thread-safe (QImage only): downscale downloaded bytes before caching them."""
    if is_svg(data):
        return data
    img = decode_scaled(data, box)
    return encode(img) if img is not None else data
