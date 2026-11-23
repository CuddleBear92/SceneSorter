from scenesorter.stashbox import pick_studio

RESULTS = [
    {"id": "a", "name": "Brazzers Live", "aliases": [], "images": []},
    {"id": "b", "name": "Brazzers Exxtra", "aliases": ["BZX"],
     "images": [{"url": "small", "width": 100}, {"url": "big", "width": 800}]},
]


def test_pick_studio_exact_normalised():
    m = pick_studio("BrazzersExxtra", RESULTS, "ep")
    assert (m.stash_id, m.logo_url) == ("b", "big")


def test_pick_studio_alias_and_no_fuzzy():
    assert pick_studio("bzx", RESULTS, "ep").stash_id == "b"
    assert pick_studio("Brazzers", RESULTS, "ep") is None
