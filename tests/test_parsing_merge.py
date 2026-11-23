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
