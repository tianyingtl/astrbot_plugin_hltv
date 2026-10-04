"""AstrBot plugin data directory helpers."""

import os
import shutil
from pathlib import Path


PLUGIN_NAME = "astrbot_plugin_hltv"


def plugin_data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        root = Path(get_astrbot_data_path())
    except (ImportError, AttributeError):
        root = Path(os.environ.get("ASTRBOT_ROOT", Path.cwd())) / "data"
    return root / "plugin_data" / PLUGIN_NAME


def legacy_data_dir() -> Path:
    return Path.home() / f".{PLUGIN_NAME}"


def _copy_missing(source: Path, target: Path) -> bool:
    copied = False
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            copied = _copy_missing(item, target / item.name) or copied
    elif not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied = True
    return copied


def migrate_legacy_data() -> bool:
    """Copy old home-directory data into the plugin data directory.

    Existing canonical files win; the legacy directory is kept as a backup.
    """
    source = legacy_data_dir()
    target = plugin_data_dir()
    if not source.is_dir() or source.resolve() == target.resolve():
        return False

    return _copy_missing(source, target)
