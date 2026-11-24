"""'Flag for review' report: everything needed to see *why* a studio/performer name or a
merged scene came out wrong — the posts behind it, exactly as scraped."""
from __future__ import annotations

from datetime import datetime

from .models import Release, Scene
from .parsing import normalise_studio

SAMPLES = 8


def _post_line(r: Release) -> str:
    bits = [f"[{r.source}]", r.raw_title or "(no title)"]
    if r.filename and r.filename != r.raw_title:
        bits.append(f"| file: {r.filename}")
    if r.studio:
        bits.append(f"| studio: {r.studio}")
    if r.performers:
        bits.append(f"| performers: {', '.join(r.performers)}")
    if r.scene_date:
        bits.append(f"| date: {r.scene_date.isoformat()}")
    return " ".join(bits)


def build_report(flags: list[dict], scenes: list[Scene], scene_performers: dict[str, list[str]],
                 performer_sources: dict[str, set[str]], app_version: str = "") -> str:
    by_studio: dict[str, list[Scene]] = {}
    for s in scenes:
        by_studio.setdefault(normalise_studio(s.studio) if s.studio else "", []).append(s)
    by_key = {s.key: s for s in scenes}
    with_performer: dict[str, list[Scene]] = {}
    for key, perfs in scene_performers.items():
        s = by_key.get(key)
        if s is not None:
            for p in perfs:
                with_performer.setdefault(p, []).append(s)

    out = [f"SceneSorter review report — {datetime.now():%Y-%m-%d %H:%M}"
           + (f" — build {app_version}" if app_version else ""),
           f"{len(flags)} flagged item(s). Each lists the posts behind it, exactly as scraped.", ""]
    for kind, title in (("studio", "STUDIOS"), ("performer", "PERFORMERS"), ("scene", "SCENES")):
        items = [f for f in flags if f["kind"] == kind]
        if not items:
            continue
        out += [f"===== {title} ({len(items)}) =====", ""]
        for f in items:
            out.append(f"* {f['name']}   (key: {f['key']})")
            if f.get("note"):
                out.append(f"  note: {f['note']}")
            if kind == "studio":
                ss = by_studio.get(f["key"], [])
                out.append(f"  {len(ss)} scene(s). Sample posts:")
                out += [f"    {_post_line(r)}" for s in ss[:SAMPLES] for r in s.releases[:2]]
            elif kind == "performer":
                ss = with_performer.get(f["key"], [])
                src = ", ".join(sorted(performer_sources.get(f["key"], ()))) or "?"
                out.append(f"  {len(ss)} scene(s), found via: {src}. Sample posts:")
                out += [f"    {_post_line(r)}" for s in ss[:SAMPLES] for r in s.releases[:2]]
            else:
                s = by_key.get(f["key"])
                if s is None:
                    out.append("  (scene no longer exists in this form — probably merged/split differently now)")
                else:
                    out.append(f"  {len(s.releases)} post(s) merged into this scene:")
                    out += [f"    {_post_line(r)}" for r in s.releases]
            out.append("")
    return "\n".join(out)
