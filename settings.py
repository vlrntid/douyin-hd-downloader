"""settings.py - Persistent JSON settings for Douyin HD Downloader.

Settings live in the per-user data dir (see :func:`updater.user_data_dir`) so
they survive app updates / rebuilds and never get wiped when the exe folder is
replaced. Reads are defensive: a missing or corrupt file yields defaults rather
than crashing the app.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from updater import user_data_dir

SETTINGS_FILENAME = "settings.json"

DEFAULTS: Dict[str, Any] = {
    # Auto-check and update the yt-dlp engine on startup.
    "auto_update_ytdlp": True,
    # GitHub "owner/repo" for app self-updates. Blank = dormant (no app-update
    # checks; yt-dlp updates + self-repair still run).
    "update_repo": "",
    # Browser used for the "Import Cookies" login flow.
    "preferred_browser": "Chrome",
    # Where downloads are saved (blank = use the default downloads dir).
    "download_dir": "",
    # Explicit ffmpeg path (blank = auto-detect / bundled).
    "ffmpeg_path": "",
    # No-login (browser capture) mode default for Douyin.
    "nologin_default": True,
}


def settings_path() -> str:
    return os.path.join(user_data_dir(), SETTINGS_FILENAME)


def load_settings() -> Dict[str, Any]:
    """Return settings merged over defaults (never raises)."""
    data: Dict[str, Any] = dict(DEFAULTS)
    path = settings_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        # Corrupt / unreadable file: fall back to defaults.
        pass
    return data


def save_settings(settings: Dict[str, Any]) -> None:
    """Persist *settings* (best-effort; failures are swallowed)."""
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Only persist known keys to keep the file clean.
        to_write = {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(to_write, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def update_setting(key: str, value: Any) -> Dict[str, Any]:
    """Load, set one key, save, and return the updated settings dict."""
    settings = load_settings()
    if key in DEFAULTS:
        settings[key] = value
        save_settings(settings)
    return settings
