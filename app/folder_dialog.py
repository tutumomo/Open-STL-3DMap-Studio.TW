"""Native "kies een map" dialog.

Tkinter insists on owning the main thread, which the web server already has, so
the dialog runs in a short-lived child process. The result comes back through a
temp file rather than stdout: the installed build has no console, so stdout is
not connected to anything and anything printed there is lost.

Frozen, the child is this same exe re-invoked with --pick-folder; in development
it is `python -m app.folder_dialog`.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

PICK_FLAG = "--pick-folder"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def show_dialog(initial: str = "", title: str = "Kies een map") -> str:
    """Run the actual dialog. Only call this from a process of its own."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        start = initial if initial and Path(initial).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(initialdir=start, title=title, mustexist=False)
    finally:
        root.destroy()
    return chosen or ""


def pick_folder(initial: str = "", title: str = "Kies een map") -> str | None:
    """Ask the user for a folder. Returns None if they cancelled or it failed."""
    handle = tempfile.NamedTemporaryFile(prefix="3dmaps-dir-", suffix=".txt",
                                         delete=False, mode="w", encoding="utf-8")
    handle.close()
    result_path = Path(handle.name)

    args = [PICK_FLAG, initial, title, str(result_path)]
    cmd = [sys.executable, *args] if getattr(sys, "frozen", False) \
        else [sys.executable, "-m", "app.folder_dialog", *args[1:]]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       creationflags=_NO_WINDOW,
                       cwd=str(Path(__file__).resolve().parent.parent))
        chosen = result_path.read_text(encoding="utf-8").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        result_path.unlink(missing_ok=True)

    return chosen or None


def main(argv: list[str]) -> int:
    """argv: <initial> <title> <result_file>"""
    initial = argv[0] if len(argv) > 0 else ""
    title = argv[1] if len(argv) > 1 else "Kies een map"
    chosen = show_dialog(initial, title)
    if len(argv) > 2:
        Path(argv[2]).write_text(chosen, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
