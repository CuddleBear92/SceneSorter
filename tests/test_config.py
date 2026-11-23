from pathlib import Path

from scenesorter import config
from scenesorter.config import Settings
from scenesorter.sources import SourceConfig


def test_data_dir_is_beside_app_not_appdata():
    assert config.DATA_DIR == config.app_dir() / "data" or "SCENESORTER_HOME" in __import__("os").environ
    assert "appdata" not in str(config.DATA_DIR).lower()


def test_settings_roundtrip(tmp_path):
    s = Settings(preferred_groups=["PRT", "GUSH"], sources=[SourceConfig("b", "wordpress", "http://x/")])
    s.save(tmp_path / "settings.json")
    back = Settings.load(tmp_path / "settings.json")
    assert back.preferred_groups == ["PRT", "GUSH"] and back.sources[0].id == "b"


def test_relative_paths_resolve_to_app_dir():
    assert Settings.resolve("Library") == config.app_dir() / "Library"
    assert Settings.resolve(str(Path("/abs/x").resolve())).is_absolute()
