from datetime import date

from scenesorter.db import Database
from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich
from scenesorter.views import ViewSpec, ViewState, compute_view, is_hidden


def scenes():
    return merge_releases([enrich(Release("b", str(i), "", t, posted=date(2024, 1, i + 1)))
                           for i, t in enumerate(["Tushy.24.01.01.Lily.Lou.Weekend.XXX.1080p.MP4-PRT",
                                                  "Tushy.24.01.02.Kira.Noir.Office.XXX.1080p.MP4-PRT",
                                                  "Vixen.24.01.03.Lily.Lou.Party.XXX.1080p.MP4-PRT"])])


def state(sc, **kw):
    perf = {s.key: (["lilylou"] if "Lily" in s.primary.raw_title else ["kiranoir"]) for s in sc}
    return ViewState(scene_performers=perf, **kw)


def titles(v):
    return sorted(s.title.split()[-1] for s in v)


def test_hidden_performer_hides_their_scenes():
    sc = scenes()
    st = state(sc, hidden_performers={"lilylou"})
    assert titles(compute_view(sc, ViewSpec(), st)) == ["Office"]
    assert len(compute_view(sc, ViewSpec(show_hidden=True), st)) == 3


def test_favourite_performer_overrides_hidden_studio_only_when_enabled():
    sc = scenes()
    st = state(sc, hidden_studios={"tushy"}, fav_performers={"lilylou"})
    assert titles(compute_view(sc, ViewSpec(), st)) == ["Party"]                       # override off
    st.fav_overrides_hidden_studio = True
    assert titles(compute_view(sc, ViewSpec(), st)) == ["Party", "Weekend"]            # Lily Lou's Tushy scene back
    hidden_scene = next(s for s in sc if "Weekend" in s.title)
    st.hidden = {hidden_scene.key}                                                     # an explicitly hidden scene stays hidden
    assert is_hidden(hidden_scene, st)


def test_db_hidden_performers_and_flags(tmp_path):
    db = Database(tmp_path / "s.db")
    db.hide_performer("lilylou", "Lily Lou")
    assert db.hidden_performers() == {"lilylou": "Lily Lou"}
    db.hide_performer("lilylou", "Lily Lou", hide=False)
    assert db.hidden_performers() == {}
    db.flag("studio", "080225", "080225", "catalogue code, not a studio")
    db.flag("performer", "scene1", "Scene 1")
    assert [(f["kind"], f["name"], f["note"]) for f in db.review_flags()] == [
        ("performer", "Scene 1", ""), ("studio", "080225", "catalogue code, not a studio")]
    db.flag("performer", "scene1", "Scene 1", on=False)
    assert len(db.review_flags()) == 1


def test_review_report_lists_posts_behind_each_flag():
    from scenesorter.review import build_report
    sc = scenes()
    perf = {s.key: (["lilylou"] if "Lily" in s.primary.raw_title else ["kiranoir"]) for s in sc}
    party = next(s for s in sc if "Party" in s.title)
    flags = [{"kind": "studio", "key": "tushy", "name": "Tushy", "note": "test"},
             {"kind": "performer", "key": "lilylou", "name": "Lily Lou", "note": ""},
             {"kind": "scene", "key": party.key, "name": "Party", "note": "wrong merge?"}]
    text = build_report(flags, sc, perf, {"lilylou": {"title"}}, "29")
    assert "===== STUDIOS (1) =====" in text and "note: test" in text
    assert "Tushy.24.01.01.Lily.Lou.Weekend" in text and "found via: title" in text
    assert "1 post(s) merged into this scene" in text and "wrong merge?" in text
