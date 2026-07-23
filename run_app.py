"""Entry point for the packaged application.

Also the child process for the folder dialog: the frozen exe re-invokes itself
with --pick-folder, so that flag has to be handled before anything heavy is
imported.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

PREFERRED_PORT = 8765


def _redirect_streams(log_path: Path) -> None:
    """A windowed build has no console, so sys.stdout is None.

    Any library that writes to it - uvicorn, for one - would crash. Point both
    streams at a log file instead, which also gives us something to read when a
    user reports that "it does not start".
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _free_port(preferred: int = PREFERRED_PORT) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


def _already_running(port: int = PREFERRED_PORT) -> bool:
    """True if our app is already serving on the preferred port.

    Double-clicking the icon while it runs should just bring up the existing
    window, not spin up a second server on a random port.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            import json
            return json.load(r).get("app") == "3d-kaart-bouwer-nl"
    except Exception:  # noqa: BLE001 - anything means "not our app there"
        return False


def main() -> int:
    from app.folder_dialog import PICK_FLAG

    if len(sys.argv) > 1 and sys.argv[1] == PICK_FLAG:
        from app.folder_dialog import main as dialog_main
        return dialog_main(sys.argv[2:])

    # If it is already up, just open the existing window and leave.
    if _already_running(PREFERRED_PORT):
        webbrowser.open(f"http://127.0.0.1:{PREFERRED_PORT}")
        return 0

    from app.paths import data_dir

    _redirect_streams(data_dir() / "log.txt")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )

    import uvicorn

    from app.main import app

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    logging.getLogger("3dmaps").info("starting on %s", url)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
