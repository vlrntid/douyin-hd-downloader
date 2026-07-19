"""main.py - Entry point for Douyin HD Downloader."""

from __future__ import annotations

import os
import sys

# --- Bootstrap: dirs, logging, and the managed yt-dlp override MUST run before
# anything imports yt_dlp (i.e. before importing gui/downloader). ------------- #
from updater import ensure_app_dirs, ensure_ytdlp_override, setup_logging

ensure_app_dirs()
setup_logging()
ensure_ytdlp_override()  # prepend managed yt-dlp to sys.path if present

import customtkinter as ctk  # noqa: E402

from gui import DouyinDownloaderApp  # noqa: E402


def _configure_browser_path() -> None:
    """Make the frozen app fully self-contained.

    Points Playwright at browsers bundled in a ``ms-playwright`` folder beside
    the ``.exe``, and tells the downloader where ``ffmpeg.exe`` lives, so neither
    needs to be installed on the target machine. In dev (no bundled folders) these
    fall back to Playwright's default browser path and whatever ffmpeg is on PATH.
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    bundled_browsers = os.path.join(base, "ms-playwright")
    if os.path.isdir(bundled_browsers):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled_browsers
    else:
        # Fall back to the default Playwright cache if no bundled copy ships.
        default_browsers = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "ms-playwright",
        )
        if os.path.isdir(default_browsers):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = default_browsers

    bundled_ffmpeg = os.path.join(base, "ffmpeg.exe")
    if os.path.isfile(bundled_ffmpeg):
        os.environ["FFMPEG_PATH"] = bundled_ffmpeg


_configure_browser_path()


def main() -> None:
    app = DouyinDownloaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
