from datetime import date

from scenesorter.db import Database
from scenesorter.models import DownloadLink, LinkKind, Release
from scenesorter.parsing import enrich


def mk(pid, title, **kw):
    return enrich(Release("blog", pid, f"http://x/{pid}", title, posted=date(2024, 1, 20), **kw))


def test_roundtrip_and_incremental(tmp_path):
    db = Database(tmp_path / "s.db")
    r = mk("1", "StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265.PRT",
           tags=["Blonde"], duration_s=1935, filename="a.mkv", details_loaded=True,
           links=[DownloadLink("magnet:?xt=1", LinkKind.MAGNET, "Magnet")])
    assert db.upsert_releases([r]) == 1
    assert db.upsert_releases([r, mk("2", "Other.24.01.16.X.XXX.720p.MP4-GUSH")]) == 1
    assert db.known_ids("blog", ["1", "2", "3"]) == {"1", "2"}
    back = {x.post_id: x for x in db.load_releases()}
    a = back["1"]
    assert (a.release_group, a.tags, a.duration_s, a.filename) == ("PRT", ["Blonde"], 1935, "a.mkv")
    assert a.scene_date == date(2024, 1, 15) and a.posted == date(2024, 1, 20)
    assert a.links[0].kind is LinkKind.MAGNET
    assert dict(db.release_groups()) == {"PRT": 1, "GUSH": 1}


def test_listing_does_not_clobber_details(tmp_path):
    db = Database(tmp_path / "s.db")
    db.upsert_releases([mk("1", "S.24.01.15.A.XXX.1080p.MP4-PRT", duration_s=100, details_loaded=True)])
    db.upsert_releases([mk("1", "S.24.01.15.A.XXX.1080p.MP4-PRT")])
    assert db.load_releases()[0].duration_s == 100


def test_reparse(tmp_path):
    db = Database(tmp_path / "s.db")
    db.upsert_releases([mk("1", "S.24.01.15.A.XXX.1080p.HEVC.x265.PRT")])
    db.conn.execute("UPDATE posts SET release_group='', studio='wrong'")
    db.set_meta("parser_version", "0")
    assert db.reparse_if_needed() == 1
    r = db.load_releases()[0]
    assert (r.release_group, r.studio) == ("PRT", "S")
    assert db.reparse_if_needed() == 0


def test_flags(tmp_path):
    db = Database(tmp_path / "s.db")
    db.set_flag("blog", "1", "started")
    db.set_flag("blog", "2", "manual")
    assert db.flags() == {("blog", "1"): "started", ("blog", "2"): "manual"}
    db.set_flag("blog", "1", "verified")
    db.set_flag("blog", "1", "manual")  # never downgrades verified
    assert db.flags()[("blog", "1")] == "verified"
    db.clear_flags([("blog", "1")])
    assert ("blog", "1") not in db.flags()


def test_migrates_old_downloads_to_flags(tmp_path):
    db = Database(tmp_path / "s.db")
    db.conn.execute("INSERT INTO downloads(id, source, post_id, state) VALUES ('a','blog','9','Sent to torrent client')")
    db.set_meta("schema_version", "1")
    db.close()
    assert Database(tmp_path / "s.db").flags() == {("blog", "9"): "started"}
