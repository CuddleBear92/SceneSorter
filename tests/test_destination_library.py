import json
from datetime import date

from scenesorter.config import Settings
from scenesorter.importer import scan_library
from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.naming import parse_with_scheme
from scenesorter.parsing import enrich

WHISPARR = ("{Studio CleanTitleSlug}{.Release Date}.{Scene.CleanTitle}{.Scene.Performers}"
            "{.Quality.Title}{.MediaInfo VideoCodec}{-Release Group}")
NAME = ("LittleAsians.2026-06-30.Im.A.Woman.Now.But.Ill.Always.Be.Your.Little.Slut.Kaori.Ojima.Debut."
        "Kaori.Ojima.WEBDL-1080p.HEVC-PRT.mp4")


def test_old_library_setting_becomes_destination(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"library_dir": "D:/Sorted", "scan_library_dir": False}))
    s = Settings.load(f)
    assert s.destination_dir == "D:/Sorted" and s.library_dir == "" and s.scan_destination_dir is False
    f.write_text(json.dumps({"download_dir": "dl"}))  # old file still on the old default folder
    assert Settings.load(f).destination_dir == "library"
    s.library_dir = "E:/Library"
    s.save(f)
    s2 = Settings.load(f)
    assert (s2.destination_dir, s2.library_dir) == ("D:/Sorted", "E:/Library")


def test_whisparr_scheme_fields():
    p = parse_with_scheme(WHISPARR, NAME)
    assert p.studio == "LittleAsians" and p.scene_date == date(2026, 6, 30)
    assert (p.resolution, p.codec, p.group) == ("1080p", "HEVC", "PRT")
    assert "Kaori Ojima" in p.words and p.words.startswith("Im A Woman")
    p = parse_with_scheme("{Studio Title} - {Release-Date} - {Scene Title} [{Quality Full}]",
                          "Brazzers Exxtra - 2024-01-15 - Hot Day [WEBDL-1080p].mkv")
    assert (p.studio, p.scene_date, p.words, p.resolution) == ("Brazzers Exxtra", date(2024, 1, 15), "Hot Day", "1080p")


def _scene(title):
    return merge_releases([enrich(Release("wp", title[:20], "", title))])[0]


def test_library_scan_with_scheme_and_studio_folders(tmp_path):
    s = _scene("[LittleAsians] Kaori Ojima - Im A Woman Now But Ill Always Be Your Little Slut - Kaori Ojima Debut (2026-06-30) 1080p")
    other = _scene("LittleAsians.26.06.30.Someone.Else.Another.Scene.XXX.1080p.HEVC.x265.PRT")
    lib = tmp_path / "Library"
    (lib / "Little Asians").mkdir(parents=True)
    f = lib / "Little Asians" / NAME
    f.write_bytes(b"x")
    res = scan_library(lib, [s, other], scheme=WHISPARR)
    assert res.owned == {s.key: [f]} and res.unmatched == 0
    # cached: unchanged files aren't matched again
    known = {r[0]: (r[1], r[2], r[4], r[5]) for r in res.rows}
    again = scan_library(lib, [s, other], known, scheme=WHISPARR)
    assert again.owned == {s.key: [f]} and again.rows == []


def test_studio_folder_used_when_name_lacks_studio(tmp_path):
    s = _scene("Brazzers Exxtra - Jane Doe - Hot Summer Day In The City (2024-01-15) 1080p")
    lib = tmp_path / "Library"
    (lib / "Brazzers Exxtra").mkdir(parents=True)
    f = lib / "Brazzers Exxtra" / "2024-01-15 - Hot Summer Day In The City [WEBDL-1080p].mp4"
    f.write_bytes(b"x")
    assert scan_library(lib, [s]).owned == {s.key: [f]}
