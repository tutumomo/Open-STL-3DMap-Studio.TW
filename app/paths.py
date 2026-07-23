"""Where files live, in development and once installed.

Installed, the program sits in Program Files, which is read-only for a normal
user. So anything the app writes - cache, uploads, generated models, settings -
moves to the per-user AppData folder, while the bundled web assets are read from
the PyInstaller extraction directory.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .appinfo import APP_NAME  # noqa: F401 - re-exported for callers


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Read-only files shipped with the app (the web/ folder)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Writable working data."""
    if is_frozen():
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        target = base / APP_NAME
    else:
        target = Path(__file__).resolve().parent.parent
    target.mkdir(parents=True, exist_ok=True)
    return target


def default_export_dir() -> Path:
    """Where generated models go until the user picks somewhere else."""
    documents = Path.home() / "Documents"
    base = documents if documents.is_dir() else Path.home()
    return base / "Open STL-3DMap Studio"


# --------------------------------------------------------------------------- #
# small persisted settings file (separate from a project)
# --------------------------------------------------------------------------- #
def _settings_path() -> Path:
    return data_dir() / "settings.json"


def read_settings() -> dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_settings(values: dict[str, Any]) -> None:
    current = read_settings()
    current.update(values)
    try:
        _settings_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    except OSError:
        pass


def export_dir() -> Path:
    stored = read_settings().get("export_dir")
    if stored:
        try:
            return Path(stored)
        except (TypeError, ValueError):
            pass
    return default_export_dir()


def set_export_dir(path: Path) -> None:
    write_settings({"export_dir": str(path)})


def open_in_explorer(path: Path) -> bool:
    """Show a folder in Windows Explorer."""
    try:
        os.startfile(str(path))  # noqa: S606 - local desktop app, user asked for it
        return True
    except (OSError, AttributeError):
        return False
