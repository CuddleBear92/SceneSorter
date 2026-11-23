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
    db.set_flag("blog", "1", "grabbed")
    db.set_flag("blog", "2", "done")
    assert db.flags() == {("blog", "1"): "grabbed", ("blog", "2"): "done"}
    db.set_flag("blog", "2", "failed")            # a failed retry keeps 'done'
    assert db.flags()[("blog", "2")] == "done"
    db.set_flag("blog", "1", "failed")            # grabbed -> failed is fine
    assert db.flags()[("blog", "1")] == "failed"
    db.set_flag("blog", "1", "verified")
    db.set_flag("blog", "1", "done")              # never downgrades verified
    assert db.flags()[("blog", "1")] == "verified"
    db.set_flag("blog", "2", "grabbed", force=True)  # explicit user action wins
    assert db.flags()[("blog", "2")] == "grabbed"
    db.clear_flags([("blog", "1")])
    assert ("blog", "1") not in db.flags()


def test_hidden(tmp_path):
    db = Database(tmp_path / "s.db")
    db.hide_scene("k1", "Scene", [("blog", "1"), ("blog", "2")])
    db.hide_studio("studio", "Studio")
    keys, posts, studios = db.hidden()
    assert keys == {"k1"} and posts == {("blog", "1"), ("blog", "2")} and studios == {"studio": "Studio"}
    db.hide_scene("k1", "Scene", [("blog", "1"), ("blog", "2")], hide=False)
    db.hide_studio("studio", "Studio", hide=False)
    assert db.hidden() == (set(), set(), {})


def test_migrates_old_downloads_to_flags(tmp_path):
    db = Database(tmp_path / "s.db")
    db.conn.execute("INSERT INTO downloads(id, source, post_id, state) VALUES ('a','blog','9','Sent to torrent client')")
    db.set_meta("schema_version", "1")
    db.close()
    assert Database(tmp_path / "s.db").flags() == {("blog", "9"): "grabbed"}


def test_migrates_build3_flag_names(tmp_path):
    db = Database(tmp_path / "s.db")
    db.conn.execute("INSERT INTO flags(source, post_id, state) VALUES ('blog','1','started'), ('blog','2','manual'),"
                    " ('blog','3','started')")
    db.conn.execute("INSERT INTO downloads(id, source, post_id, state) VALUES ('x','blog','3','Done')")
    db.set_meta("schema_version", "2")
    db.close()
    assert Database(tmp_path / "s.db").flags() == {("blog", "1"): "grabbed", ("blog", "2"): "done",
                                                   ("blog", "3"): "done"}


def test_favorites(tmp_path):
    db = Database(tmp_path / "s.db")
    db.set_favorite("studio", "tushy", "Tushy", True)
    db.set_favorite("performer", "lilylou", "Lily Lou", True)
    assert db.favorites("studio") == {"tushy": "Tushy"} and db.favorites("performer") == {"lilylou": "Lily Lou"}
    db.set_favorite("studio", "tushy", "Tushy", False)
    assert db.favorites("studio") == {}


def test_interrupted_http_downloads_marked_failed(tmp_path):
    db = Database(tmp_path / "s.db")
    db.conn.execute("INSERT INTO downloads(id, source, post_id, link_kind, state) VALUES "
                    "('a','blog','1','direct','Downloading'), ('b','blog','2','magnet','Sent to torrent client'),"
                    "('c','blog','3','direct','Done')")
    db.set_flag("blog", "1", "grabbed")
    db.set_flag("blog", "2", "grabbed")
    db.set_flag("blog", "3", "done")
    assert db.mark_interrupted_downloads() == 1
    assert db.flags() == {("blog", "1"): "failed", ("blog", "2"): "grabbed", ("blog", "3"): "done"}


def _premium(**kw):
    from datetime import date as _d
    base = dict(studio="Sis Loves Me", title="Luna Princess", performers=["Luna Lovehard", "Rion King"],
                scene_date=_d(2026, 11, 21), filename="SisLovesMe.26.11.21.Luna.Princess.XXX.1080p.HEVC.x265.PRT.mp4")
    base.update(kw)
    return enrich(Release("encodedfap", "e1", "", "Luna Princess", **base))


def test_enrich_records_only_parsed_fields():
    r = _premium()
    assert "performers" not in r.parsed and "studio" not in r.parsed     # source-supplied
    assert "resolution" in r.parsed and "release_group" in r.parsed      # filled by parsing


def test_parser_upgrade_never_touches_source_fields(tmp_path):
    db = Database(tmp_path / "s.db")
    db.upsert_releases([_premium(), mk("1", "StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265.PRT")])
    db.set_meta("parser_version", "0")
    db.reparse_if_needed({"blog"})
    back = {r.post_id: r for r in db.load_releases()}
    assert back["e1"].performers == ["Luna Lovehard", "Rion King"] and back["e1"].studio == "Sis Loves Me"
    assert back["1"].studio == "StudioName"


def test_legacy_rows_only_reparsed_for_title_only_sources(tmp_path):
    db = Database(tmp_path / "s.db")
    db.upsert_releases([_premium()])
    db.conn.execute("UPDATE posts SET parsed_fields=NULL")          # cached before provenance existed
    db.set_meta("parser_version", "0")
    db.reparse_if_needed({"blog"})                                  # encodedfap is not title-only
    assert db.load_releases()[0].performers == ["Luna Lovehard", "Rion King"]


def test_listing_restores_fields_reset_by_old_reparse(tmp_path):
    db = Database(tmp_path / "s.db")
    full = _premium(details_loaded=True)
    db.upsert_releases([full])
    db.conn.execute("UPDATE posts SET performers='[]', studio='Luna', title='Princess'")  # damage
    db.upsert_releases([_premium(details_loaded=False, filename="")])                     # a rescan's listing
    r = db.load_releases()[0]
    assert (r.studio, r.title, r.performers) == ("Sis Loves Me", "Luna Princess", ["Luna Lovehard", "Rion King"])
    assert r.filename.endswith(".mp4")                               # detail-only field kept


def test_parsed_fields_column_on_old_db(tmp_path):
    p = tmp_path / "old.db"
    db = Database(p)
    db.upsert_releases([mk("1", "StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265.PRT")])
    db.conn.execute("ALTER TABLE posts DROP COLUMN parsed_fields")   # what a build-21 database looks like
    db.set_meta("schema_version", "5")
    db.close()
    db = Database(p)
    assert "parsed_fields" in {r[1] for r in db.conn.execute("PRAGMA table_info(posts)")}
    assert db.load_releases()[0].parsed is None                       # unknown provenance for old rows
