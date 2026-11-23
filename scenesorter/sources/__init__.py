"""Source registry. Add a new site by subclassing Source and registering it here."""
from __future__ import annotations

from .base import AuthRequired, RateLimited, Source, SourceConfig
from .encodedfapfiles import EncodedFapFilesSource
from .wordpress import WordPressSource

SOURCE_TYPES: dict[str, type[Source]] = {
    "wordpress": WordPressSource,
    "encodedfapfiles": EncodedFapFilesSource,
}


def build_source(cfg: SourceConfig) -> Source:
    try:
        cls = SOURCE_TYPES[cfg.type]
    except KeyError:
        raise ValueError(f"Unknown source type {cfg.type!r}; known: {sorted(SOURCE_TYPES)}") from None
    return cls(cfg)


__all__ = ["AuthRequired", "RateLimited", "Source", "SourceConfig", "SOURCE_TYPES", "build_source"]
