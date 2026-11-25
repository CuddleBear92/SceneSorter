from scenesorter.placeholders import PlaceholderTracker, digest, sweep_cache


def test_same_picture_for_many_scenes_is_a_placeholder(tmp_path):
    t = PlaceholderTracker(tmp_path / "p.json")
    h = digest(b"picture removed")
    # one scene's three resolutions sharing a picture is normal
    for res in ("2160", "1080", "720"):
        assert t.check(h, "scene-a", f"https://x/a{res}.jpg", f"/c/a{res}") is None
    assert t.check(h, "scene-b", "https://x/b.jpg", "/c/b") is None
    assert t.check(h, "scene-c", "https://x/c.jpg", "/c/c") is None
    others = t.check(h, "scene-d", "https://x/d.jpg", "/c/d")
    assert sorted(k for k, _u, _p in others) == ["scene-a", "scene-b", "scene-c"]
    assert t.is_dead("https://x/d.jpg") and t.is_dead("https://x/b.jpg")
    # from now on it's recognised straight away, and remembered across restarts
    assert t.check(h, "scene-e", "https://x/e.jpg", "/c/e") == []
    t.save()
    t2 = PlaceholderTracker(tmp_path / "p.json")
    assert h in t2.hashes and t2.is_dead("https://x/e.jpg")
    # real, distinct pictures are never flagged
    for i in range(10):
        assert t2.check(digest(bytes([i])), f"s{i}", f"https://x/{i}.jpg", "") is None


def test_sweep_cache_removes_repeated_files(tmp_path):
    for i in range(9):
        (tmp_path / f"{i:02d}.jpg").write_bytes(b"removed")
    (tmp_path / "real1.jpg").write_bytes(b"a")
    (tmp_path / "real2.jpg").write_bytes(b"a")  # same scene, two resolutions: kept
    (tmp_path / "known.jpg").write_bytes(b"known")
    found, deleted = sweep_cache(tmp_path, {digest(b"known")})
    assert found == {digest(b"removed"), digest(b"known")} and deleted == 10
    assert sorted(f.name for f in tmp_path.iterdir()) == ["real1.jpg", "real2.jpg"]
