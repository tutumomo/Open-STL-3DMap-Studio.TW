"""Central app identity: name, version, and where updates and feedback go."""

from __future__ import annotations

APP_NAME = "Open STL-3DMap Studio"
APP_VERSION = "1.1.1"

# Public repository under the OpenAEC Foundation.
GITHUB_OWNER = "OpenAEC-Foundation"
GITHUB_REPO = "Open-STL-3DMap-Studio"

REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
RELEASES_URL = f"{REPO_URL}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
ISSUES_NEW_URL = f"{REPO_URL}/issues/new"

# Ping marker so a second launch recognises an already-running instance.
INSTANCE_MARKER = "open-stl-3dmap-studio"
