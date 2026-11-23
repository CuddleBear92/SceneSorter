import random
from datetime import date

from scenesorter.merge import cache_from_groups, group_rows, merge_incremental, merge_releases, studio_key
from scenesorter.models import Release
from scenesorter.parsing import enrich


def library(n=400, seed=3):
    random.seed(seed)
    words = [f"w{i}" for i in range(300)]
    rels = []
    for i in range(n):
        st = f"Studio{random.randint(0, 30)}"
        y, m, d = random.randint(20, 25), random.randint(1, 12), random.randint(1, 28)
        t = ".".join(random.sample(words, 4))
        base = f"{st}.{y}.{m:02d}.{d:02d}.{t}"
        for res in ("1080p", "720p"):
            rels.append(enrich(Release("blog", f"b{i}{res}", "", f"{base}.XXX.{res}.HEVC.x265.PRT",
                                       posted=date(2000 + y, m, d))))
        if i % 3 == 0:
            rels.append(enrich(Release("prem", f"p{i}", "", t.replace(".", " "), studio=st, title=t.replace(".", " "),
                                       scene_date=date(2000 + y, m, d), filename=f"{base}.XXX.1080p.HEVC.x265.PRT.mp4")))
    return rels


def shape(scenes):
    return sorted(tuple(sorted((r.source, r.post_id) for r in s.releases)) for s in scenes)


def test_per_studio_merge_equals_full_merge():
    rels = library()
    full = merge_releases(rels)
    inc, cache, merged = merge_incremental(rels, None, None)
    assert shape(inc) == shape(full) and merged == {studio_key(r) for r in rels}


def test_only_dirty_studios_are_merged_and_result_matches():
    rels = library()
    _scenes, cache, _ = merge_incremental(rels, None, None)
    extra = library(n=20, seed=99)                           # a new page of posts
    for r in extra:
        r.post_id = "new" + r.post_id
    dirty = {studio_key(r) for r in extra}
    inc, cache2, merged = merge_incremental(rels + extra, cache, dirty)
    assert merged == dirty
    assert shape(inc) == shape(merge_releases(rels + extra))


def test_saved_groups_rebuild_the_cache():
    rels = library()
    scenes, cache, _ = merge_incremental(rels, None, None)
    rows = group_rows(cache, cache.keys())
    groups = {(s, p): key for s, p, key, _st in rows}
    cache2, dirty = cache_from_groups(rels, groups, stale=set())
    assert not dirty
    again, _, merged = merge_incremental(rels, cache2, set())
    assert not merged and shape(again) == shape(scenes)
    # a post without a saved group, or one changed since, makes only its studio dirty
    stale = {(rels[0].source, rels[0].post_id)}
    _c, dirty = cache_from_groups(rels, groups, stale)
    assert dirty == {studio_key(rels[0])}


def test_db_roundtrip_and_staleness(tmp_path):
    import time
    from scenesorter.db import Database
    db = Database(tmp_path / "s.db")
    rels = library(n=60)
    db.upsert_releases(rels)
    _s, cache, merged = merge_incremental(rels, None, None)
    db.save_scene_groups(group_rows(cache, merged), merged, "v1")
    groups, stale = db.load_scene_groups("v1")
    assert len(groups) == len(rels) and not stale
    assert db.load_scene_groups("v2") == ({}, set())                 # other version -> ignored
    time.sleep(0.01)
    db.upsert_releases([rels[0]])                                   # post updated after grouping
    _g, stale = db.load_scene_groups("v1")
    assert stale == {(rels[0].source, rels[0].post_id)}
    db.clear_scene_groups()
    assert db.load_scene_groups("v1") == ({}, set())
