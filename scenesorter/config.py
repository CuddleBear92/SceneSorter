"""User settings, stored as JSON in the portable data folder next to the exe."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import os
import sys

from .sources.base import SourceConfig
from .stashbox import DEFAULT_STASHBOXES, StashBoxConfig

APP = "SceneSorter"


def app_dir() -> Path:
    """Folder the app lives in: next to SceneSorter.exe, or the repo root from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Portable: everything lives in a "data" folder beside the exe — no AppData.
# SCENESORTER_HOME overrides it (handy for tests or a shared location).
DATA_DIR = Path(os.environ.get("SCENESORTER_HOME") or app_dir() / "data")
CONFIG_DIR = DATA_DIR
CACHE_DIR = DATA_DIR / "cache"
CONFIG_FILE = DATA_DIR / "settings.json"

DEFAULT_TEMPLATE = "{studio}/{studio} - {date} - {performers} - {title} [{resolution}] [{group}]"
OLD_DEFAULT_TEMPLATES = {"{studio}/{studio} - {date} - {performers} - {title} [{resolution}]"}


@dataclass
class Settings:
    # Relative paths are relative to the exe folder; absolute paths work too.
    download_dir: str = "downloads"
    library_dir: str = "library"
    rename_template: str = DEFAULT_TEMPLATE
    preferred_resolution: str = "1080p"
    max_parallel_downloads: int = 2
    # Torrent black hole: .torrent files are saved here for your torrent client's watch folder.
    torrent_blackhole_dir: str = ""
    magnets_to_blackhole: bool = True   # also write magnet links as .magnet files
    # Import folders: scanned regularly; video files found are matched by name and flagged as downloaded.
    import_dirs: list[str] = field(default_factory=list)
    import_scan_minutes: int = 5
    scan_library_dir: bool = True       # treat the library folder as an import folder too
    # stash-box scene matching (needs API keys under stashboxes)
    scene_matching: bool = True
    match_interval_s: int = 3           # one scene lookup every N seconds
    auto_sort_completed: bool = True    # move finished downloads into the library
    rename_files: bool = True           # ...renamed with rename_template (off = keep the original name)
    preferred_source: str = ""          # source id picked first by "Download preferred" ("" = list order)
    check_updates_on_start: bool = True
    refresh_on_start: bool = True
    # Incremental refresh stops at the first page with no new posts, or after this many pages.
    max_refresh_pages: int = 10
    # Background crawler: slowly walks every listing page until the whole site is cached.
    crawl_enabled: bool = True
    crawl_interval_s: int = 30          # one page request every N seconds (across all sources)
    auto_refresh_minutes: int = 30      # check for new posts this often (0 = off)
    # Optional size limit for cached *scene thumbnails* (0 = no limit, keep everything).
    # Studio logos and performer photos are always kept.
    thumb_cache_limit_mb: int = 0
    # Scenes from a hidden studio still show when a favourite performer is in them.
    fav_performers_override_hidden_studios: bool = False
    rescan_interval_s: int = 5          # "Rescan all pages": one page request every N seconds
    # Release groups to prefer when picking which release to download (first = best).
    preferred_groups: list[str] = field(default_factory=lambda: ["PRT"])
    update_repo: str = "CuddleBear92/SceneSorter"
    sources: list[SourceConfig] = field(default_factory=list)
    # stash-box instances for studio logos / scene matching. Keys are stored in
    # data/settings.json (portable), so keep that folder private.
    stashboxes: list[StashBoxConfig] = field(
        default_factory=lambda: [StashBoxConfig(**asdict(c)) for c in DEFAULT_STASHBOXES])

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "Settings":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        srcs = [SourceConfig(**s) for s in raw.pop("sources", [])]
        boxes = raw.pop("stashboxes", None)
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        s = cls(**known, sources=srcs)
        if s.rename_template in OLD_DEFAULT_TEMPLATES:  # untouched old default -> new default (adds {group})
            s.rename_template = DEFAULT_TEMPLATE
        if boxes is not None:
            s.stashboxes = [StashBoxConfig(**b) for b in boxes]
        return s

    @staticmethod
    def resolve(p: str) -> Path:
        """Relative paths are relative to the exe folder (keeps a portable install portable)."""
        path = Path(os.path.expandvars(os.path.expanduser(p)))
        return path if path.is_absolute() else app_dir() / path

    def save(self, path: Path = CONFIG_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)
