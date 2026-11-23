"""User settings, stored as JSON in the portable data folder next to the exe."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import os
import sys

from .sources.base import SourceConfig

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

DEFAULT_TEMPLATE = "{studio}/{studio} - {date} - {performers} - {title} [{resolution}]"


@dataclass
class Settings:
    # Relative paths are relative to the exe folder; absolute paths work too.
    download_dir: str = "downloads"
    library_dir: str = "library"
    rename_template: str = DEFAULT_TEMPLATE
    preferred_resolution: str = "1080p"
    max_parallel_downloads: int = 2
    auto_sort_completed: bool = True
    check_updates_on_start: bool = True
    refresh_on_start: bool = True
    # Incremental refresh stops at the first page with no new posts, or after this many pages.
    max_refresh_pages: int = 10
    # Release groups to prefer when picking which release to download (first = best).
    preferred_groups: list[str] = field(default_factory=lambda: ["PRT"])
    update_repo: str = "CuddleBear92/SceneSorter"
    sources: list[SourceConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "Settings":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        srcs = [SourceConfig(**s) for s in raw.pop("sources", [])]
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known, sources=srcs)

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
