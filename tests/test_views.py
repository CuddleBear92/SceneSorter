from datetime import date

from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich, find_codec
from scenesorter.views import ViewSpec, ViewState, compute_view


def scenes():
    titles = [("Tushy.24.02.10.Lily.Lou.Weekend.XXX.1080p.HEVC.x265.PRT", date(2024, 3, 1), 1800),
              ("Brazzers.24.01.05.Angela.White.Office.XXX.1080p.MP4-GUSH", date(2024, 3, 5), 3600),
              ("Vixen.23.12.01.Emily.Willis.Party.XXX.2160p.AV1-PRT", date(2024, 2, 1), 2400),
              ("Brazzers.24.02.20.Kira.Noir.Pool.XXX.720p.x264-PRT", date(2024, 2, 25), 1200)]
    return merge_releases([enrich(Release("blog", str(i), "", t, posted=p, duration_s=d))
                           for i, (t, p, d) in enumerate(titles)])


def titles(view):
    return [s.title.split()[-1] for s in view]


def test_codec_detection():
    assert find_codec("Studio.XXX.1080p.HEVC.x265.PRT") == "HEVC"
    assert find_codec("Studio.XXX.720p.x264-PRT") == "AVC"
    assert find_codec("Something [H.264]") == "AVC"
    assert find_codec("Studio.XXX.2160p.AV1-PRT") == "AV1"
    assert find_codec("Studio.XXX.1080p.MP4-GUSH") == ""
    assert find_codec("", "", "h265") == "HEVC"     # from tags


def test_sorts():
    sc, st = scenes(), ViewState()
    assert titles(compute_view(sc, ViewSpec(), st)) == ["Office", "Weekend", "Pool", "Party"]          # posted
    assert titles(compute_view(sc, ViewSpec(sort="date"), st)) == ["Pool", "Weekend", "Office", "Party"]
    # studio A-Z, newest posted first within a studio
    assert titles(compute_view(sc, ViewSpec(sort="studio"), st)) == ["Office", "Pool", "Weekend", "Party"]
    assert titles(compute_view(sc, ViewSpec(sort="duration"), st)) == ["Office", "Party", "Weekend", "Pool"]


def test_filters():
    sc = scenes()
    by = {s.title.split()[-1]: s for s in sc}
    st = ViewState(status={by["Party"].key: "complete"}, hidden={by["Office"].key})
    assert len(compute_view(sc, ViewSpec(), st)) == 3                      # one hidden
    assert len(compute_view(sc, ViewSpec(show_hidden=True), st)) == 4
    assert titles(compute_view(sc, ViewSpec(codec="HEVC"), st)) == ["Weekend"]
    assert titles(compute_view(sc, ViewSpec(codec="AVC"), st)) == ["Pool"]
    assert titles(compute_view(sc, ViewSpec(text="kira pool"), st)) == ["Pool"]
    assert len(compute_view(sc, ViewSpec(have="any", show_hidden=True), st)) == 1
