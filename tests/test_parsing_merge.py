from datetime import date

import pytest

from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich, match_key, parse_title


def rel(title, source="blog", pid=None, thumb=None):
    return enrich(Release(source=source, post_id=pid or title, post_url="", raw_title=title, thumbnail_url=thumb))


@pytest.mark.parametrize("raw,studio,d,res", [
    ("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265-GRP", "StudioName", date(2024, 1, 15), "1080p"),
    ("StudioName 24 01 15 Jane Doe Some Title XXX 2160p MP4-GRP", "StudioName", date(2024, 1, 15), "2160p"),
    ("[StudioName] Jane Doe - Some Title (2024-01-15) [720p]", "StudioName", date(2024, 1, 15), "720p"),
    ("StudioName - Jane Doe - Some Title 4K", "StudioName", None, "2160p"),
])
def test_parse_basic(raw, studio, d, res):
    p = parse_title(raw)
    assert p["studio"] == studio
    assert p["scene_date"] == d
    assert p["resolution"] == res


def test_parse_bracket_performers():
    p = parse_title("[StudioName] Jane Doe, John Roe - Some Title (2024-01-15) [1080p]")
    assert p["performers"] == ["Jane Doe", "John Roe"]
    assert p["title"] == "Some Title"


def test_dotted_title_strips_encode_info():
    p = parse_title("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265-GRP")
    assert p["title"] == "Jane Doe Some Title"


def test_resolution_variants_share_key():
    a = rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265-GRP")
    b = rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.720p.MP4-OTHER")
    c = rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.2160p.MP4-GRP")
    assert match_key(a) == match_key(b) == match_key(c)


def test_merge_across_sites_and_resolutions():
    rs = [
        rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265-GRP", "blog", "1"),
        rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.720p.MP4-GRP", "blog", "2"),
        rel("[StudioName] Jane Doe - Some Title (2024-01-15) [2160p]", "premium", "a", thumb="http://t/1.jpg"),
        rel("StudioName.24.01.16.Other.Girl.Other.Title.XXX.1080p.MP4-GRP", "blog", "3"),
    ]
    scenes = merge_releases(rs)
    assert len(scenes) == 2
    big = next(s for s in scenes if len(s.releases) == 3)
    assert big.resolutions == ["2160p", "1080p", "720p"]
    assert big.sources == ["blog", "premium"]
    assert big.thumbnail_urls == ["http://t/1.jpg"]


def test_fuzzy_merge_extra_tag():
    a = rel("StudioName.24.01.15.Jane.Doe.Some.Long.Title.XXX.1080p.MP4-GRP", "blog", "1")
    b = rel("StudioName.24.01.15.Jane.Doe.Some.Long.Title.Part.XXX.720p.MP4-GRP", "premium", "2")
    assert len(merge_releases([a, b])) == 1


def test_different_dates_not_merged():
    a = rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.MP4-GRP", "blog", "1")
    b = rel("StudioName.24.02.15.Jane.Doe.Some.Title.XXX.1080p.MP4-GRP", "blog", "2")
    assert len(merge_releases([a, b])) == 2


def test_duplicate_posts_deduped():
    a = rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.MP4-GRP", "blog", "1")
    assert len(merge_releases([a, a])[0].releases) == 1


@pytest.mark.parametrize("raw,group", [
    ("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265.PRT", "PRT"),
    ("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.720p.MP4-GUSH", "GUSH"),
    ("[StudioName] Jane Doe - Some Title (2024-01-15) [1080p] [prt]", "PRT"),
    ("StudioName - Jane Doe - Some Title 1080p PRT", "PRT"),
    ("StudioName.24.01.15.Jane.Doe.XXX.2160p.HEVC.x265.SomeNewGrp", "SomeNewGrp"),
    ("StudioName.24.01.15.Jane.Doe.XXX.2160p.MP4-x265", ""),
    ("StudioName - Jane Doe - Some Title 1080p", ""),
])
def test_find_group(raw, group):
    from scenesorter.parsing import find_group
    assert find_group(raw) == group


def test_group_not_in_match_key():
    a = rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265.PRT")
    b = rel("StudioName.24.01.15.Jane.Doe.Some.Title.XXX.720p.MP4-GUSH")
    assert match_key(a) == match_key(b)
    assert a.release_group == "PRT" and b.release_group == "GUSH"


def test_undated_messy_title_merges_with_dated():
    a = rel("StudioName.24.01.15.Jane.Doe.Some.Long.Title.XXX.1080p.HEVC.x265.PRT", "blog", "1")
    b = rel("StudioName.com - Jane Doe - Some Long Title [720p]", "premium", "2")
    assert len(merge_releases([a, b])) == 1


def test_post_metadata():
    from scenesorter.parsing import extract_post_metadata
    m = extract_post_metadata("Filename: X.24.01.15.Jane.XXX.1080p.HEVC.x265.PRT.mkv\nDuration: 00:32:15\n"
                              "Size: 1.25 GB\nTags: Blonde, POV")
    assert m["duration_s"] == 32 * 60 + 15
    assert m["size_bytes"] == int(1.25 * (1 << 30))
    assert m["tags"] == ["Blonde", "POV"]
    assert m["filename"].endswith("PRT.mkv")


def test_studio_domain_suffix_stripped():
    assert parse_title("StudioName.com - Jane Doe - Some Title [720p]")["studio"] == "StudioName"


def test_studio_not_in_tags():
    r = enrich(Release("blog", "1", "", "StudioName.24.01.15.Jane.Doe.XXX.1080p.MP4-PRT",
                       tags=["Studio Name", "Blonde", "studioname.com", "POV", "blonde"]))
    assert r.tags == ["Blonde", "POV"]


def _r(title, source, pid, dur=None):
    return enrich(Release(source, pid, "", title, duration_s=dur))


def test_merge_across_sites_with_different_dates_when_runtime_matches():
    # Real-world case: the two sites list different dates for the same scene.
    a = _r("MrLuckyPOV.23.08.17.Eve.Marlowe.Sensual.Sex.With.Blue.Haired.Babe.XXX.1080p.HEVC.x265.PRT",
           "blog", "1", 1772)
    b = _r("[Mr. LuckyPOV] Eve Marlowe, Tyler D. - Eve Marlowe Sensual Sex With Blue Haired Babe (2023-07-07) [720p]",
           "premium", "2", 1772)
    (s,) = merge_releases([a, b])
    assert s.sources == ["blog", "premium"] and s.resolutions == ["1080p", "720p"]


def test_other_date_needs_runtime_or_identical_long_title():
    t1 = "MrLuckyPOV.23.08.17.Eve.Marlowe.Sensual.Sex.With.Blue.Haired.Babe.XXX.1080p.HEVC.x265.PRT"
    t2 = "MrLuckyPOV.23.07.07.Eve.Marlowe.Sensual.Sex.With.Blue.Haired.Babe.XXX.720p.MP4-GUSH"
    # identical long titles, no runtimes -> merge
    assert len(merge_releases([_r(t1, "a", "1"), _r(t2, "b", "2")])) == 1
    # clearly different runtimes -> two scenes even with identical titles
    assert len(merge_releases([_r(t1, "a", "1", 1772), _r(t2, "b", "2", 900)])) == 2
    # short generic title on different dates, no runtime -> not merged
    g1, g2 = "Studio.23.01.01.Jane.Doe.Hot.XXX.1080p.MP4-PRT", "Studio.23.05.01.Jane.Doe.Hot.XXX.1080p.MP4-PRT"
    assert len(merge_releases([_r(g1, "a", "1"), _r(g2, "b", "2")])) == 2
    # same runtime but different titles -> not merged
    d1 = "Studio.23.01.01.Jane.Doe.Pool.Party.Fun.XXX.1080p.MP4-PRT"
    d2 = "Studio.23.03.01.Jane.Doe.Office.Overtime.XXX.1080p.MP4-PRT"
    assert len(merge_releases([_r(d1, "a", "1", 1800), _r(d2, "b", "2", 1800)])) == 2


def test_same_date_but_far_runtime_not_merged():
    a = _r("Studio.24.01.15.Jane.Doe.Unbound.Part.1.XXX.1080p.MP4-PRT", "a", "1", 4000)
    b = _r("Studio.24.01.15.Jane.Doe.Unbound.Part.1.Trailer.XXX.1080p.MP4-PRT", "b", "2", 120)
    assert len(merge_releases([a, b])) == 2


def test_file_and_stashbox_match_either_date():
    from scenesorter.filematch import SceneIndex
    from scenesorter.stashbox import pick_scene
    a = _r("MrLuckyPOV.23.08.17.Eve.Marlowe.Sensual.Sex.With.Blue.Haired.Babe.XXX.1080p.HEVC.x265.PRT", "blog", "1", 1772)
    b = _r("[Mr. LuckyPOV] Eve Marlowe - Eve Marlowe Sensual Sex With Blue Haired Babe (2023-07-07) [720p]",
           "premium", "2", 1772)
    (s,) = merge_releases([a, b])
    idx = SceneIndex([s])
    for name in ["Mr. LuckyPOV - 2023-07-07 - Eve Marlowe - Sensual Sex With Blue Haired Babe [WEBDL-720p].mp4",
                 "Mr. LuckyPOV - 2023-08-17 - Eve Marlowe - Sensual Sex With Blue Haired Babe [WEBDL-1080p].mp4"]:
        assert idx.match(name).scene is s
    cand = {"id": "x", "title": "Sensual Sex With Blue Haired Babe", "date": "2023-07-07", "studio": "Mr Lucky POV",
            "parent": "", "performers": [{"id": "e", "name": "Eve Marlowe"}], "image_url": None, "duration": 1770}
    assert pick_scene(s, [cand])["id"] == "x"


def test_same_release_name_on_both_sites_merges():
    from datetime import date
    name = "SisLovesMe.26.11.21.Luna.Princess.XXX.{res}.HEVC.x265.PRT"
    blog = [_r(name.format(res=res), "pornrips", f"p{res}", dur) for res, dur in (("1080p", 2957), ("720p", 2957))]
    # the premium site supplies its own structured fields + a file name
    prem = [enrich(Release("encodedfap", f"e{res}", "", "Luna Princess", studio="Sis Loves Me",
                           scene_date=date(2026, 11, 21), performers=["Luna Lovehard", "Rion King"],
                           title="Luna Princess", filename=name.format(res=res) + ".mp4", duration_s=2956))
            for res in ("1080p", "720p")]
    scenes = merge_releases(blog + prem)
    assert len(scenes) == 1
    assert scenes[0].sources == ["encodedfap", "pornrips"] and len(scenes[0].releases) == 4


def test_same_day_matching_runtime_uses_title_words_only():
    from datetime import date
    a = _r("SisLovesMe.26.11.21.Luna.Princess.XXX.1080p.HEVC.x265.PRT", "a", "1", 2957)
    b = enrich(Release("b", "2", "", "Luna Princess", studio="Sis Loves Me", scene_date=date(2026, 11, 21),
                       performers=["Luna Lovehard", "Rion King"], title="Luna Princess", duration_s=2956))
    assert len(merge_releases([a, b])) == 1
    c = enrich(Release("b", "3", "", "Other Thing", studio="Sis Loves Me", scene_date=date(2026, 11, 21),
                       performers=["Luna Lovehard"], title="Other Thing", duration_s=2956))
    assert len(merge_releases([a, c])) == 2      # same day + runtime but a different title


def test_resolution_tags_dropped():
    r = enrich(Release("b", "1", "", "Studio.24.01.15.Jane.XXX.1080p.MP4-PRT", tags=["1080p", "720p", "HEVC", "POV"]))
    assert r.tags == ["POV"]


def _blog(name, pid):
    return enrich(Release("pornrips", pid, "", name))


def _prem(title, studio, performers, filename, pid):
    # Shape of real EncodedFapFiles posts: plain title + studio/performers from the site,
    # no scene date; the scene-style release name only exists as the file name.
    return enrich(Release("encodedfap", pid, "", title, studio=studio, title=title,
                          performers=performers, filename=filename))


def test_date_taken_from_file_name_when_post_has_none():
    r = _prem("Compilation", "ATK Girlfriends", [], "ATKGirlfriends.25.09.04.Compilation.XXX.1080p.HEVC.x265.PRT.mp4", "e")
    assert r.scene_date == date(2025, 9, 4) and "scene_date" in r.parsed


def test_premium_compilations_without_dates_do_not_collapse():
    rels = []
    for i, d in enumerate(["25.09.04", "25.09.12", "25.10.03", "26.11.23"]):
        for res in ("1080p", "720p"):
            n = f"ATKGirlfriends.{d}.Compilation.XXX.{res}.HEVC.x265.PRT"
            rels += [_blog(n, f"a{i}{res}"), _prem("Compilation", "ATK Girlfriends", [], n + ".mp4", f"ae{i}{res}")]
    scenes = merge_releases(rels)
    assert len(scenes) == 4 and {len(s.releases) for s in scenes} == {4}


def test_same_title_two_dates_each_premium_post_joins_its_own_date():
    rels = []
    for d in ("26.11.09", "26.11.23"):
        for res in ("1080p", "720p"):
            n = f"NebraskaCoeds.{d}.Veronika.Lei.XXX.{res}.HEVC.x265.PRT"
            rels += [_blog(n, f"n{d}{res}"),
                     _prem("Veronika Lei", "Nebraska Coeds", ["Veronika Lei"], n + ".mp4", f"ne{d}{res}")]
    scenes = merge_releases(rels)
    assert len(scenes) == 2
    for s in scenes:
        assert len(s.releases) == 4 and len(s.scene_dates) == 1        # both sites, one date each


def test_legacy_premium_rows_get_missing_date_filled_on_reparse():
    from scenesorter.parsing import reparse
    r = _prem("Veronika Lei", "Nebraska Coeds", ["Veronika Lei"],
              "NebraskaCoeds.26.11.23.Veronika.Lei.XXX.1080p.HEVC.x265.PRT.mp4", "x")
    r.scene_date, r.parsed = None, None                              # cached by an older build
    reparse(r, title_only_source=False)
    assert r.scene_date == date(2026, 11, 23) and r.performers == ["Veronika Lei"]


def _series(n_dates=6, dur=1800):
    days = ["24.11.29", "24.12.06", "24.12.13", "25.01.03", "25.01.10", "25.02.14", "25.03.07"][:n_dates]
    return [_r(f"StoryHotWives.{d}.Cum.Compilation.XXX.{res}.HEVC.x265.PRT", "pornrips", f"{d}{res}", dur)
            for d in days for res in ("1080p", "720p")]


def test_generic_title_series_is_not_chained_together():
    # Same studio, same two-word title, same runtime, different dates: different scenes.
    assert len(merge_releases(_series())) == 6


def test_undated_post_with_generic_title_does_not_chain_a_series():
    from datetime import date  # noqa: F401
    und = enrich(Release("encodedfap", "u1", "", "Cum Compilation", studio="Story Hot Wives",
                         title="Cum Compilation", duration_s=1800))
    scenes = merge_releases(_series() + [und])
    assert len(scenes) == 7                                  # ambiguous -> stays on its own
    assert max(len(s.releases) for s in scenes) == 2         # nothing got glued together


def test_same_site_different_dates_never_merge():
    a = _r("Studio.24.01.01.Jane.Doe.Long.Specific.Scene.Title.XXX.1080p.MP4-PRT", "pornrips", "1", 1800)
    b = _r("Studio.24.03.01.Jane.Doe.Long.Specific.Scene.Title.XXX.1080p.MP4-PRT", "pornrips", "2", 1800)
    assert len(merge_releases([a, b])) == 2                  # same site, two dates = two posts/scenes
    c = _r("[Studio] Jane Doe - Long Specific Scene Title (2024-02-01) [720p]", "premium", "3", 1800)
    # the other site's copy may join one of them (unambiguous? no: it fits both) -> stays separate
    assert len(merge_releases([a, b, c])) == 3


def test_date_not_hidden_by_digits_in_studio_name():
    from scenesorter.parsing import parse_title
    p = parse_title("Studio12.26.12.25.Jane.Doe.Some.Title.XXX.1080p.HEVC.x265.PRT")
    assert p["scene_date"] == date(2026, 12, 25) and p["studio"] == "Studio12"


def test_compilation_series_across_two_sites():
    # 60 weekly "Compilation" posts from one studio on both sites: 60 scenes, each with
    # 1080p + 720p from both sites — not one giant scene.
    from datetime import timedelta
    rels, d = [], date(2025, 10, 20)
    for i in range(60):
        dd = d + timedelta(days=7 * i)
        for res in ("1080p", "720p"):
            name = f"ATKGirlfriends.{dd:%y.%m.%d}.Compilation.XXX.{res}.HEVC.x265.PRT"
            rels.append(_r(name, "pornrips", f"p{i}{res}", 3600 + (i % 5) * 20))
            rels.append(enrich(Release("encodedfap", f"e{i}{res}", "", "Compilation", studio="ATK Girlfriends",
                                       title="Compilation", scene_date=dd, filename=name + ".mp4",
                                       duration_s=3600 + (i % 5) * 20)))
    scenes = merge_releases(rels)
    assert len(scenes) == 60 and {len(s.releases) for s in scenes} == {4}


def test_file_name_date_wins_over_site_date():
    # The site's own date field said 11.09 for a file released 11.23.
    r = enrich(Release("encodedfap", "x", "", "NebraskaCoeds.26.11.23.Veronika.Lei.XXX.1080p.HEVC.x265.PRT.mp4",
                       studio="Nebraska Coeds", performers=["Veronika Lei"], scene_date=date(2026, 11, 9),
                       filename="NebraskaCoeds.26.11.23.Veronika.Lei.XXX.1080p.HEVC.x265.PRT.mp4"))
    assert r.scene_date == date(2026, 11, 23) and "scene_date" in r.parsed


def test_site_dates_shared_by_a_series_do_not_merge_it():
    rels = []
    for i, d in enumerate(["25.09.04", "25.09.12", "26.11.23"]):
        for res in ("1080p", "720p"):
            n = f"ATKGirlfriends.{d}.Compilation.XXX.{res}.HEVC.x265.PRT"
            rels.append(_blog(n, f"a{i}{res}"))
            rels.append(enrich(Release("encodedfap", f"e{i}{res}", "", n + ".mp4", studio="ATK Girlfriends",
                                       scene_date=date(2026, 1, 1), filename=n + ".mp4")))   # same site date for all
    scenes = merge_releases(rels)
    assert len(scenes) == 3 and {len(s.releases) for s in scenes} == {4}


@pytest.mark.parametrize("raw", ["080225-001-CARIB", "1903-Russia-1080p-H", "15", "174",
                                 "18OnlyGirls.com - Gina Gerson ( Good Vibrations ) 1080p"])
def test_implausible_studios_rejected(raw):
    st = parse_title(raw)["studio"]
    assert st in ("", "18OnlyGirls")


@pytest.mark.parametrize("name", ["1000Facials", "21Sextury", "18 Lust", "Tiny4K", "#Navarasa", "TeamSkeet Labs"])
def test_real_studios_kept(name):
    from scenesorter.parsing import plausible_studio
    assert plausible_studio(name)


def test_generic_performer_names_rejected():
    from scenesorter.parsing import plausible_performer
    for bad in ["Scene 1", "Part 2", "Episode 3", "Vol. 4", "S01E02", "1080p", "15", "www.site.com"]:
        assert not plausible_performer(bad), bad
    for good in ["Angela White", "Lily Lou", "Tyler D.", "Abigaiil Morris", "Alexis"]:
        assert plausible_performer(good), good
    p = parse_title("Studio - Jane Doe, Scene 2 - Some Title")
    assert p["performers"] == ["Jane Doe"]
