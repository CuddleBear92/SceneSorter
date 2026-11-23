from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich
from scenesorter.performers import collect
from scenesorter.stashbox import pick_scene


def scenes(*titles):
    return merge_releases([enrich(Release("blog", str(i), "", t)) for i, t in enumerate(titles)])


def test_collect_from_posts_matches_and_titles():
    sc = scenes("[Tushy] Lily Lou, Kira Noir - Weekend Away (2024-02-10) [1080p]",
                "Tushy.24.03.01.Lily.Lou.Solo.Time.XXX.1080p.HEVC.x265.PRT",
                "Vixen.24.03.02.Angela.White.Something.XXX.1080p.HEVC.x265.PRT",
                "Vixen.24.03.03.Angel.Wicky.Other.XXX.1080p.HEVC.x265.PRT")
    vixen = next(s for s in sc if "Something" in s.title)
    matches = {vixen.key: [{"performers": [{"id": "x", "name": "Angela White"}]}]}
    perf, by_scene = collect(sc, matches)
    assert perf["lilylou"].scenes == {s.key for s in sc if "Lily" in s.primary.raw_title}
    assert perf["lilylou"].sources == {"post", "title"}
    assert perf["angelawhite"].sources == {"stashbox"}
    assert "angel" not in perf  # single words are never spotted in titles
    assert by_scene[vixen.key] == ["angelawhite"]


def test_pick_scene_requires_studio_and_date():
    (s,) = scenes("BrazzersExxtra.23.07.21.Angela.White.Unbound.Part.1.XXX.1080p.HEVC.x265.PRT")
    good = {"id": "1", "title": "Angela White : Unbound Part 1", "date": "2023-07-21", "studio": "Brazzers Exxtra",
            "parent": "Brazzers", "performers": [{"id": "a", "name": "Angela White"}], "image_url": None, "duration": 1}
    assert pick_scene(s, [good])["id"] == "1"
    assert pick_scene(s, [dict(good, date="2023-08-21")]) is None          # wrong date
    assert pick_scene(s, [dict(good, studio="Tushy", parent="")]) is None  # wrong studio
    assert pick_scene(s, [dict(good, studio="Something", parent="BrazzersExxtra")])["id"] == "1"  # parent ok


def _cand(**kw):
    base = {"id": "1", "title": "Angela White : Unbound Part 1", "date": "2023-07-21", "studio": "Brazzers Exxtra",
            "parent": "Brazzers", "performers": [{"id": "a", "name": "Angela White"}], "image_url": None,
            "duration": 4037}
    return dict(base, **kw)


def test_duration_rejects_and_breaks_ties():
    rels = [enrich(Release("blog", "1", "", "BrazzersExxtra.23.07.21.Angela.White.Unbound.XXX.1080p.HEVC.x265.PRT",
                           duration_s=4000))]
    (s,) = merge_releases(rels)
    # way too short -> rejected even though studio, date and words fit
    assert pick_scene(s, [_cand(duration=1500)]) is None
    # two same-day candidates with similar titles: the one with the right length wins
    best = pick_scene(s, [_cand(id="p2", title="Angela White : Unbound Part 2", duration=3700),
                          _cand(id="p1", duration=4010)])
    assert best["id"] == "p1" and best["duration_check"] == "close"
    # unknown duration on their side: falls back to the old rules
    assert pick_scene(s, [_cand(duration=None)])["duration_check"] == "unknown"


def test_duration_check_bounds():
    from scenesorter.stashbox import duration_check
    assert duration_check(2000, 2025) == "close"
    assert duration_check(2000, 2150) == ""        # intro/outro trims are normal
    assert duration_check(2000, 1000) == "far"
    assert duration_check(None, 1000) == ""
