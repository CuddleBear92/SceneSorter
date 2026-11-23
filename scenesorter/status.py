"""Download status of releases and scenes.

Per release (a post = one resolution on one site), stored in the ``flags`` table:

    grabbed   download started / sent to the torrent client or black hole
    done      finished (HTTP download completed, or marked by hand)
    failed    download failed or was cancelled — still wanted
    verified  file found on disk (set by a library scan)

Per scene, derived from its releases:

    none      nothing grabbed or done
    grabbed   something is on its way, nothing finished yet
    partial   something finished, but not at the wanted resolution
              (e.g. 720p done, 1080p never completed) — still wanted
    complete  a release at (or above) the wanted resolution is done/verified
"""
from __future__ import annotations

from .models import Release, Scene

GRABBED, DONE, FAILED, VERIFIED = "grabbed", "done", "failed", "verified"
FINISHED = {DONE, VERIFIED}

NONE, S_GRABBED, PARTIAL, COMPLETE = "none", "grabbed", "partial", "complete"

# Old flag names from build 3.
LEGACY = {"started": GRABBED, "manual": DONE}


def _rank(res: str) -> int:
    try:
        return int(res.rstrip("p"))
    except ValueError:
        return 0


def target_rank(scene: Scene, preferred_resolution: str) -> int:
    """The resolution that counts as 'complete': the preferred one if it's posted,
    else the best posted. Empty preference = best available."""
    available = [r.resolution_rank for r in scene.releases if r.resolution_rank]
    if not available:
        return 0
    pref = _rank(preferred_resolution) if preferred_resolution else 0
    if pref and pref in available:
        return pref
    return max(available) if not pref else min((a for a in available if a >= pref), default=max(available))


def scene_status(scene: Scene, flags: dict[tuple[str, str], str], preferred_resolution: str) -> str:
    states = [(r, flags.get((r.source, r.post_id))) for r in scene.releases]
    finished = [r for r, st in states if st in FINISHED]
    target = target_rank(scene, preferred_resolution)
    if any(r.resolution_rank >= target for r in finished):
        return COMPLETE
    if finished:
        return PARTIAL
    if any(st == GRABBED for _r, st in states):
        return S_GRABBED
    return NONE


def target_release(scene: Scene, preferred_resolution: str, order_key) -> Release:
    """Release to flag when the user marks the whole scene as downloaded."""
    target = target_rank(scene, preferred_resolution)
    candidates = [r for r in scene.releases if r.resolution_rank == target] or scene.releases
    return sorted(candidates, key=order_key)[0]


def source_order(source_ids: list[str], preferred_source: str) -> list[str]:
    """Settings order, with the preferred source moved to the front."""
    return ([preferred_source] if preferred_source in source_ids else []) + \
        [s for s in source_ids if s != preferred_source]


def preferred_release(scene: Scene, preferred_resolution: str, groups: list[str], sources: list[str]) -> Release:
    """What "Download preferred" grabs: wanted resolution (or the fallback that
    counts as complete), then preferred release group, then preferred source."""
    def key(r: Release) -> tuple[int, int]:
        return (groups.index(r.release_group) if r.release_group in groups else len(groups),
                sources.index(r.source) if r.source in sources else len(sources))
    return target_release(scene, preferred_resolution, key)
