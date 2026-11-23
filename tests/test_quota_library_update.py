from pathlib import Path

from scenesorter.library import render_name, scan_library, sort_into_library
from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich, match_key
from scenesorter.quota import WINDOW, QuotaTracker
from scenesorter.updater import is_newer


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_quota_rolling_window(tmp_path):
    clk = Clock()
    q = QuotaTracker(tmp_path / "q.json", clock=clk)
    for _ in range(3):
        assert q.try_consume("premium", 3)
    assert not q.try_consume("premium", 3)
    assert q.seconds_until_next("premium") == WINDOW
    clk.t += WINDOW + 1
    assert q.remaining("premium", 3) == 3
    # persisted
    q.try_consume("premium", 3)
    assert QuotaTracker(tmp_path / "q.json", clock=clk).used("premium") == 1


def test_quota_site_sync(tmp_path):
    clk = Clock()
    q = QuotaTracker(tmp_path / "q.json", clock=clk)
    q.sync_remaining("premium", 10)
    q.try_consume("premium", 500)
    assert q.remaining("premium", 500) == 9


def sample():
    r = enrich(Release("blog", "1", "", "[StudioName] Jane Doe - Some: Title? (2024-01-15) [1080p]"))
    return r


def test_render_name_sanitises():
    p = render_name("{studio}/{studio} - {date} - {performers} - {title} [{resolution}]", sample(), ".MP4")
    assert p == Path("StudioName", "StudioName - 2024-01-15 - Jane Doe - Some Title [1080p].mp4")


def test_render_name_empty_fields():
    r = enrich(Release("blog", "1", "", "StudioName - Just A Title"))
    p = render_name("{studio} - {date} - {title} [{resolution}]", r, ".mp4")
    assert p == Path("StudioName - Just A Title.mp4")


def test_sort_and_scan_roundtrip(tmp_path):
    dl = tmp_path / "dl" / "Some.Folder"
    dl.mkdir(parents=True)
    (dl / "sample.mp4").write_bytes(b"x")
    (dl / "main.mp4").write_bytes(b"x" * 100)
    lib = tmp_path / "lib"
    r = sample()
    out = sort_into_library(dl, r, lib, "{studio}/{studio} - {date} - {performers} - {title} [{resolution}]")
    assert out.exists() and not dl.exists()
    owned = scan_library(lib)
    # a 720p post of the same scene from another site should be recognised as owned
    other = enrich(Release("premium", "x", "", "StudioName.24.01.15.Jane.Doe.Some.Title.XXX.720p.MP4-GRP"))
    scene = merge_releases([other])[0]
    assert scene.key in owned


def test_version_compare():
    assert is_newer("v0.2.0", "0.1.9")
    assert not is_newer("v0.1.0", "0.1.0")
    assert is_newer("1.10.0", "1.9.3")
