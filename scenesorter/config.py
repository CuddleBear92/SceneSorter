"""User settings, stored as JSON in the per-user config directory."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir

from .sources.base import SourceConfig

APP = "SceneSorter"
CONFIG_DIR = Path(user_config_dir(APP, appauthor=False))
DATA_DIR = Path(user_data_dir(APP, appauthor=False))
CACHE_DIR = Path(user_cache_dir(APP, appauthor=False))
CONFIG_FILE = CONFIG_DIR / "settings.json"

DEFAULT_TEMPLATE = "{studio}/{studio} - {date} - {performers} - {title} [{resolution}]"


@dataclass
class Settings:
    download_dir: str = str(Path.home() / "Downloads" / "SceneSorter")
    library_dir: str = str(Path.home() / "Videos" / "Scenes")
    rename_template: str = DEFAULT_TEMPLATE
    preferred_resolution: str = "1080p"
    max_parallel_downloads: int = 2
    auto_sort_completed: bool = True
    check_updates_on_start: bool = True
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

    def save(self, path: Path = CONFIG_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)
