from scenesorter.db import Database
from scenesorter.importer import scan
from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich


def scenes():
    titles = ["Tushy.24.02.10.Lily.Lou.Weekend.Away.XXX.2160p.MP4-GUSH",
              "Tushy.24.02.10.Lily.Lou.Weekend.Away.XXX.1080p.HEVC.x265.PRT"]
    return merge_releases([enrich(Release("blog", str(i), "", t)) for i, t in enumerate(titles)])


def test_scan_folder_and_skip_known(tmp_path):
    (tmp_path / "Tushy - 2024-02-10 - Lily Lou - Weekend Away [WEBDL-1080p].mp4").write_text("x")
    rel = tmp_path / "Tushy.24.02.10.Lily.Lou.Weekend.Away.XXX.2160p.MP4-GUSH"
    rel.mkdir()
    (rel / "video.mp4").write_text("x")                     # name only in folder
    (tmp_path / "random clip.mp4").write_text("x")
    r = scan([tmp_path], scenes())
    assert sorted(m.release.resolution for _p, m in r.matched) == ["1080p", "2160p"]
    assert [p.name for p in r.unmatched] == ["random clip.mp4"]
    db = Database(tmp_path / "s.db")
    db.save_scanned(r.rows())
    again = scan([tmp_path], scenes(), known=db.scanned())
    assert again.skipped == 3 and not again.matched
