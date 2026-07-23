"""Check GitHub Releases for a newer version. Best-effort and offline-safe."""

from __future__ import annotations

import logging
import re

import requests

from .appinfo import APP_VERSION, LATEST_RELEASE_API, RELEASES_URL

log = logging.getLogger(__name__)


def _parse(version: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Missing/garbage parts become 0."""
    nums = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def check_for_update(timeout: float = 6.0) -> dict:
    """Return {current, latest, update_available, url}. Never raises."""
    result = {
        "current": APP_VERSION,
        "latest": None,
        "update_available": False,
        "url": RELEASES_URL,
        "checked": False,
    }
    try:
        r = requests.get(LATEST_RELEASE_API, timeout=timeout,
                         headers={"Accept": "application/vnd.github+json",
                                  "User-Agent": "open-stl-3dmap-studio"})
        if r.status_code == 404:
            # No releases published yet - not an error.
            result["checked"] = True
            return result
        r.raise_for_status()
        data = r.json()
        latest = data.get("tag_name") or data.get("name")
        result["latest"] = latest
        result["url"] = data.get("html_url") or RELEASES_URL
        result["update_available"] = _parse(latest) > _parse(APP_VERSION)
        result["checked"] = True
    except requests.RequestException as exc:
        log.info("update check skipped: %s", exc)
    return result
