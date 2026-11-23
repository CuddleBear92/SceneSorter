from scenesorter.merge import merge_releases
from scenesorter.models import Release
from scenesorter.parsing import enrich
from scenesorter.status import COMPLETE, NONE, PARTIAL, S_GRABBED, scene_status, target_release


def scene(*resolutions):
    rels = [enrich(Release("blog", res, "", f"Studio.24.01.15.Jane.Doe.Title.XXX.{res}.MP4-PRT"))
            for res in resolutions]
    (s,) = merge_releases(rels)
    return s


def test_none_and_grabbed():
    s = scene("1080p", "720p")
    assert scene_status(s, {}, "1080p") == NONE
    assert scene_status(s, {("blog", "1080p"): "grabbed"}, "1080p") == S_GRABBED


def test_partial_when_only_lower_res_finished():
    s = scene("1080p", "720p")
    flags = {("blog", "1080p"): "failed", ("blog", "720p"): "done"}
    assert scene_status(s, flags, "1080p") == PARTIAL


def test_complete_at_or_above_wanted():
    s = scene("2160p", "1080p", "720p")
    assert scene_status(s, {("blog", "1080p"): "done"}, "1080p") == COMPLETE
    assert scene_status(s, {("blog", "2160p"): "verified"}, "1080p") == COMPLETE


def test_wanted_res_not_posted_falls_back():
    s = scene("720p", "480p")
    assert scene_status(s, {("blog", "720p"): "done"}, "1080p") == COMPLETE   # best available
    assert scene_status(s, {("blog", "480p"): "done"}, "1080p") == PARTIAL
    s2 = scene("2160p", "720p")  # wanted 1080p missing -> next one up counts
    assert scene_status(s2, {("blog", "720p"): "done"}, "1080p") == PARTIAL
    assert scene_status(s2, {("blog", "2160p"): "done"}, "1080p") == COMPLETE


def test_best_available_when_no_preference():
    s = scene("2160p", "1080p")
    assert scene_status(s, {("blog", "1080p"): "done"}, "") == PARTIAL


def test_target_release():
    s = scene("2160p", "1080p", "720p")
    assert target_release(s, "1080p", lambda r: 0).resolution == "1080p"
