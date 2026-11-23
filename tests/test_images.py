import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402

from scenesorter.gui.images import (  # noqa: E402
    PixmapLRU, decode_scaled, encode, load_file, prune_cache, save_and_load, shrink_for_disk,
)

app = QGuiApplication.instance() or QGuiApplication([])


def jpeg(w, h):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 80, 60))
    return encode(img)


def test_decode_scaled_never_full_size():
    img = decode_scaled(jpeg(4000, 6000), QSize(400, 600))
    assert (img.width(), img.height()) == (400, 600)
    small = decode_scaled(jpeg(300, 200), QSize(640, 360))
    assert (small.width(), small.height()) == (300, 200)   # never upscaled


def test_lru_evicts_oldest():
    lru = PixmapLRU(2)
    lru.put("a", None)
    lru.put("b", None)
    lru.get("a")          # a is now most recent
    lru.put("c", None)
    assert "a" in lru and "c" in lru and "b" not in lru


def test_old_full_size_cache_files_shrink_on_read(tmp_path):
    f = tmp_path / "x.img"
    f.write_bytes(jpeg(3840, 2160) + b"\0" * 300_000)  # big file from an older build
    pm = load_file(f, QSize(640, 360))
    assert pm.width() == 640 and f.stat().st_size < 100_000


def test_save_and_shrink_for_disk(tmp_path):
    f = tmp_path / "y.img"
    pm = save_and_load(f, jpeg(1920, 1080), QSize(640, 360))
    assert pm.width() == 640
    assert len(shrink_for_disk(jpeg(4000, 6000), QSize(400, 600))) < len(jpeg(4000, 6000))


def test_prune_oldest_first(tmp_path):
    for i in range(10):
        p = tmp_path / f"{i}.img"
        p.write_bytes(b"x" * 1000)
        os.utime(p, (time.time() - 1000 + i, time.time() - 1000 + i))
    before, after = prune_cache([tmp_path], 5000)
    assert before == 10_000 and after <= 5000
    left = sorted(int(p.stem) for p in tmp_path.iterdir())
    assert left == list(range(10 - len(left), 10))       # newest kept
    assert prune_cache([tmp_path], 0)[1] == 0             # "Clear image cache"
