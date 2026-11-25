from pathlib import Path

from scenesorter.library import render_name, sort_into_library
from scenesorter.importer import scan_library
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


def test_render_name_group_and_default():
    from scenesorter.config import DEFAULT_TEMPLATE
    r = enrich(Release("blog", "1", "", "StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265.PRT"))
    assert render_name(DEFAULT_TEMPLATE, r, ".mkv").name.endswith("[1080p] [PRT].mkv")
    r.release_group = ""
    assert render_name(DEFAULT_TEMPLATE, r, ".mkv").name.endswith("[1080p].mkv")  # empty [] removed


def test_old_default_template_upgraded(tmp_path):
    import json
    from scenesorter.config import DEFAULT_TEMPLATE, Settings
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"rename_template": "{studio}/{studio} - {date} - {performers} - {title} [{resolution}]"}))
    assert Settings.load(f).rename_template == DEFAULT_TEMPLATE
    f.write_text(json.dumps({"rename_template": "{studio}/{title}"}))
    assert Settings.load(f).rename_template == "{studio}/{title}"   # custom templates are left alone


def test_sort_without_rename(tmp_path):
    f = tmp_path / "Orig.Name.1080p.mp4"
    f.write_bytes(b"x")
    out = sort_into_library(f, sample(), tmp_path / "lib", "{studio}/{title}", rename=False)
    assert out == tmp_path / "lib" / "Orig.Name.1080p.mp4"


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
    # a 720p post of the same scene from another site should be recognised as owned
    other = enrich(Release("premium", "x", "", "StudioName.24.01.15.Jane.Doe.Some.Title.XXX.720p.MP4-GRP"))
    scene = merge_releases([other])[0]
    assert scene.key in scan_library(lib, [scene]).owned


def test_version_compare():
    assert is_newer("v0.2.0", "0.1.9")
    assert not is_newer("v0.1.0", "0.1.0")
    assert is_newer("1.10.0", "1.9.3")


def test_flat_build_numbers():
    assert is_newer("v2", "1")
    assert is_newer("v2", "0.1.0")  # first release was tagged v0.1.0 (= build 1)
    assert is_newer("v10", "9")
    assert not is_newer("v1", "1")
    assert is_newer("v1", "0-dev+abc1234")  # dev builds always see v1+ as newer


def test_update_script_retries_and_logs():
    from pathlib import PureWindowsPath as W

    from scenesorter.updater import build_update_script
    s = build_update_script(4242, W(r"C:\Apps\Scene Sorter\SceneSorter-6.exe.new"),
                            W(r"C:\Apps\Scene Sorter\SceneSorter.exe"), W(r"C:\Apps\Scene Sorter\data\update.log"))
    assert "\r\n" in s and "timeout" not in s           # timeout fails without a console
    assert 'set "PID=4242"' in s
    assert 'find /I "SceneSorter.exe"' in s              # waits for *our* exe, not any PID substring
    assert sum(l.endswith("goto move") for l in s.split("\r\n")) == 3 and "%TRIES% GEQ 60" in s
    assert 'move /Y "%NEW%" "%CUR%"' in s and 'start "" "%CUR%"' in s
    assert s.index(":wait") < s.index(":move") < s.index(":moved") < s.index(":failed")


def test_network_error_classification():
    import pytest
    pytest.importorskip("PySide6")
    from scenesorter.gui.main_window import is_network_error
    assert is_network_error("HTTPSConnectionPool(host='x', port=443): Max retries exceeded ... NameResolutionError")
    assert is_network_error("Traceback ...\nrequests.exceptions.ConnectionError: ('Connection aborted.')")
    assert is_network_error("HTTPSConnectionPool(host='x'): Read timed out. (read timeout=30)")
    assert not is_network_error("500 Server Error: Internal Server Error for url: https://x/wp-json")


def test_exit_flush_without_console(monkeypatch):
    # The windowed exe has no console: sys.stdout / sys.stderr are None.
    import sys
    from scenesorter.app import _flush_streams
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    _flush_streams()  # must not raise
