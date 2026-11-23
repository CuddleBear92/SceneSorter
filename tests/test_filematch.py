import os
import time
from pathlib import Path

import pytest

from scenesorter.filematch import SceneIndex, is_candidate_file, walk_videos
from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich

POSTS = [
    "BrazzersExxtra.24.02.10.Angela.White.Hot.Summer.Day.XXX.1080p.HEVC.x265.PRT",
    "BrazzersExxtra.24.02.10.Angela.White.Hot.Summer.Day.XXX.720p.MP4-GUSH",
    "BrazzersExxtra.24.02.11.Kira.Noir.Office.Party.XXX.1080p.HEVC.x265.PRT",
    "Tushy.24.02.10.Lily.Lou.Weekend.Away.XXX.2160p.MP4-GUSH",
    "Tushy.24.02.10.Lily.Lou.Weekend.Away.XXX.1080p.HEVC.x265.PRT",
    "Vixen - Emily Willis - Undated Title Here [1080p]",
]


@pytest.fixture(scope="module")
def index():
    rels = [enrich(Release("blog", str(i), "", t)) for i, t in enumerate(POSTS)]
    return SceneIndex(merge_releases(rels))


@pytest.mark.parametrize("fname,title,res,grp", [
    # original release names
    ("BrazzersExxtra.24.02.10.Angela.White.Hot.Summer.Day.XXX.1080p.HEVC.x265.PRT.mkv", "Hot Summer Day", "1080p", "PRT"),
    ("brazzersexxtra.24.02.10.angela.white.hot.summer.day.xxx.720p.mp4-gush.mp4", "Hot Summer Day", "720p", "GUSH"),
    # Whisparr-style renames
    ("Brazzers Exxtra - 2024-02-10 - Angela White - Hot Summer Day [WEBDL-1080p][PRT].mp4", "Hot Summer Day", "1080p", "PRT"),
    ("Tushy - 2024-02-10 - Lily Lou - Weekend Away [WEBDL-2160p].mkv", "Weekend Away", "2160p", "GUSH"),
    # our own rename template
    ("Tushy - 2024-02-10 - Lily Lou - Weekend Away [1080p].mp4", "Weekend Away", "1080p", "PRT"),
    # date off by one (timezones), title missing performer
    ("Brazzers Exxtra - 2024-02-12 - Office Party [1080p].mp4", "Office Party", "1080p", "PRT"),
    # undated on both sides
    ("Vixen - Emily Willis - Undated Title Here 1080p.mp4", "Undated Title Here", "1080p", ""),
])
def test_matches(index, fname, title, res, grp):
    m = index.match(fname)
    assert m.scene is not None, m.reason
    assert title.split()[0].lower() in " ".join(r.raw_title for r in m.scene.releases).lower()
    assert m.release.resolution == res
    if grp:
        assert m.release.release_group == grp


@pytest.mark.parametrize("fname", [
    "Tushy - 2024-03-01 - Lily Lou - Weekend Away [1080p].mp4",        # wrong date
    "SomeOtherStudio - 2024-05-05 - Nobody - Unrelated.mp4",
    "Vixen - Totally Different Scene [1080p].mp4",
    "holiday_video_2019.mp4",
])
def test_no_false_matches(index, fname):
    assert index.match(fname).scene is None


def test_candidate_files(tmp_path):
    for name in ["a.mp4", "b.mkv.part", "c.crdownload", "d.txt", "sample-e.mp4", "f.MP4", "x.trailer.mp4"]:
        (tmp_path / name).write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "g.wmv").write_text("x")
    found = sorted(p.name for p in walk_videos([tmp_path]))
    assert found == ["a.mp4", "f.MP4", "g.wmv"]


def test_recently_modified_skipped(tmp_path):
    f = tmp_path / "new.mp4"
    f.write_text("x")
    assert not is_candidate_file(f, min_age_s=60)
    old = time.time() - 120
    os.utime(f, (old, old))
    assert is_candidate_file(Path(f), min_age_s=60)


def test_wordless_name_only_when_unambiguous(index):
    assert index.match("Tushy.24.02.10.XXX.2160p.mp4").scene is not None  # one Tushy scene that day
    rels = [enrich(Release("blog", str(i), "", t)) for i, t in enumerate(POSTS + [
        "Tushy.24.02.10.Other.Girl.Second.Scene.XXX.1080p.MP4-PRT"])]
    two = SceneIndex(merge_releases(rels))
    assert two.match("Tushy.24.02.10.XXX.2160p.mp4").scene is None          # two that day: ambiguous
