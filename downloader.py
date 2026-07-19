"""downloader.py - Core download logic for Douyin HD Downloader.

This module wraps :mod:`yt_dlp` with sensible defaults for grabbing the
highest-quality Douyin video and merging best video + best audio into a
single MP4. It is intentionally framework-agnostic so it can later be wired
into an AI video-editing pipeline without depending on the GUI.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import threading
from datetime import datetime
from typing import Callable, Optional

import yt_dlp

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Matches the common Douyin share / web link shapes.
DOUYIN_URL_RE = re.compile(
    r"^https?://"          # scheme
    r"(?:www\.|v\.)?"      # optional subdomain
    r"(?:douyin\.com|iesdouyin\.com)"  # host
    r"/",                  # path separator
    re.IGNORECASE,
)

# Matches YouTube watch / shorts / short links.
YOUTUBE_URL_RE = re.compile(
    r"^https?://"                              # scheme
    r"(?:(?:www|m|music|shorts|store)\.)?"     # optional subdomain
    r"(?:youtube\.com|youtu\.be)"              # host
    r"/",                                      # path separator
    re.IGNORECASE,
)

# Pulls a Douyin or YouTube URL out of free-text share snippets, e.g.:
#   "4.84 v@s.eB ... https://v.douyin.com/SCupJI5F674/ 复制此链接…"
#   "check this out https://www.youtube.com/shorts/abcd1234 nice"
VIDEO_LINK_RE = re.compile(
    r"https?://[^\s\"'<>]*(?:douyin\.com|youtube\.com|youtu\.be)[^\s\"'<>]*",
    re.IGNORECASE,
)

# Fields tried (in order) to find a human-friendly creator name.
CREATOR_KEYS = ("creator", "uploader", "channel", "artist", "uploader_id")
TITLE_KEYS = ("title", "fulltitle", "alt_title")

DEFAULT_FORMAT = "bestvideo+bestaudio/best"
RETRIES = 10
DATE_FMT = "%Y%m%d"

# Maps the UI's resolution choices to yt-dlp format selectors. "Best" keeps the
# original highest-quality behaviour; the others cap the video height so the
# chosen resolution is honoured when available, falling back gracefully.
QUALITY_TO_FORMAT = {
    "Best": DEFAULT_FORMAT,
    "2k": "bestvideo[height<=1440]+bestaudio/best[height<=1440]",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "540p": "bestvideo[height<=540]+bestaudio/best[height<=540]",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
}

# Shown when cookie loading fails and the cookie-less attempt also fails.
COOKIE_GUIDANCE = (
    "Close the browser completely and retry, or export a cookies.txt from "
    "your browser (e.g. the 'Get cookies.txt LOCALLY' extension) and load it "
    "with the Cookies: Browse button. Douyin usually requires login cookies."
)

# Shown specifically when Chromium DPAPI decryption fails (the common
# --cookies-from-browser failure on Windows). See yt-dlp issue #10927.
DPAPI_GUIDANCE = (
    "Your browser's cookies could not be decrypted (Windows DPAPI failure). "
    "This is a known Chromium issue. Use 'Import Cookies' to extract cookies "
    "via a fresh browser login, or load an exported cookies.txt with Browse."
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class DownloaderError(Exception):
    """Recoverable, user-facing error raised by the downloader."""


class _DownloadAborted(Exception):
    """Internal signal raised when an abort Event is set mid-download.

    Treated as a pause (not a failure) by the GUI's queue engine.
    """


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def validate_url(url: str) -> bool:
    """Return ``True`` if *url* looks like a Douyin or YouTube link."""
    if not url or not url.strip():
        return False
    url = url.strip()
    return bool(DOUYIN_URL_RE.match(url) or YOUTUBE_URL_RE.match(url))


def extract_video_url(text: str) -> Optional[str]:
    """Extract the first Douyin/YouTube URL from arbitrary pasted text.

    Douyin "copy link" snippets embed the URL inside a lot of extra text
    (captions, hashtags, @mentions, Chinese copy-prompt text). This returns
    just the ``https://…douyin.com/…`` or ``https://…youtube.com/…`` portion,
    or ``None`` if none is found.
    """
    if not text:
        return None
    match = VIDEO_LINK_RE.search(text)
    if not match:
        return None
    url = match.group(0).strip().rstrip(".,;")
    return url or None


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Make *name* safe to use as a file name on Windows / Linux / macOS."""
    if not name:
        return "untitled"
    # Strip characters that are illegal in file names.
    illegal = r'<>:"/\|?*'
    cleaned = "".join(ch for ch in name if ch not in illegal)
    # Collapse runs of whitespace.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Windows disallows leading/trailing dots and spaces.
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = "untitled"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(". ")
    return cleaned


def detect_ffmpeg() -> Optional[str]:
    """Return the path to ``ffmpeg`` if available, else ``None``.

    Checks ``$FFMPEG_PATH`` first (useful for portable installs), then the
    system ``PATH``.
    """
    env_path = os.environ.get("FFMPEG_PATH")
    candidates = []
    if env_path:
        candidates.append(env_path)
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


# --------------------------------------------------------------------------- #
# Downloader
# --------------------------------------------------------------------------- #

class DouyinDownloader:
    """High-level wrapper around yt-dlp for Douyin videos."""

    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        cookies: Optional[str] = None,
        cookiesfrombrowser: Optional[tuple] = None,
    ):
        # Fall back to auto-detection if no explicit path is given.
        self.ffmpeg_path = ffmpeg_path or detect_ffmpeg()
        # A Netscape-format cookies.txt file (exported from the browser) is the
        # most reliable option and avoids the browser-DB lock / DPAPI issues.
        # Convenience: auto-use a cookies.txt dropped next to the app.
        if cookies is None:
            cookies = self._find_local_cookies()
        self.cookies = cookies
        # NOTE: We deliberately default to None. Reading the browser's on-disk
        # cookie DB via --cookies-from-browser is what triggers the Windows
        # DPAPI "Failed to decrypt" failures (yt-dlp issue #10927). Users
        # should import cookies via CookieManager (Playwright) instead. Pass a
        # tuple like ("chrome",) only if you intentionally want DPAPI access.
        self.cookiesfrombrowser = cookiesfrombrowser

    @staticmethod
    def _find_local_cookies() -> Optional[str]:
        """Use a cookies.txt in the app folder / CWD if present (no cookie DB)."""
        here = pathlib.Path(__file__).resolve().parent
        for cand in (pathlib.Path.cwd() / "cookies.txt", here / "cookies.txt"):
            if cand.is_file():
                return str(cand)
        return None

    # -- public API --------------------------------------------------------- #

    def fetch_metadata(
        self, url: str, on_warning: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Fetch video metadata without downloading.

        Returns a dict containing at least ``title`` and ``creator`` keys.
        Raises :class:`DownloaderError` if the URL cannot be resolved.

        If cookie loading fails (e.g. the browser has the cookie DB locked),
        this automatically retries without cookies and reports a warning
        instead of hard-failing.
        """
        if not validate_url(url):
            raise DownloaderError("Not a valid Douyin URL.")
        opts = self._base_opts(simulate=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.YoutubeDLError as exc:
            if self._cookie_source() and self._is_cookie_error(exc):
                # Retry once without cookies rather than giving up.
                opts2 = self._base_opts(simulate=True, use_cookies=False)
                try:
                    with yt_dlp.YoutubeDL(opts2) as ydl:
                        info = ydl.extract_info(url, download=False)
                except yt_dlp.utils.YoutubeDLError as second_exc:
                    # Both failed: report the real cause, not the cookie symptom.
                    guidance = (
                        DPAPI_GUIDANCE if self._is_dpapi_error(exc)
                        else COOKIE_GUIDANCE
                    )
                    raise DownloaderError(
                        f"Could not read browser cookies ({exc}). "
                        f"Without cookies it also failed: {second_exc}. {guidance}"
                    ) from second_exc
                self._report(
                    on_warning,
                    "Browser cookies unavailable (browser may be open / locked); "
                    "continuing without them.",
                )
            else:
                raise DownloaderError(f"Failed to fetch video info: {exc}") from exc
        if not info:
            raise DownloaderError("No video information returned.")
        return self._normalize_info(info)

    def download(
        self,
        url: str,
        output_dir: str,
        *,
        info: Optional[dict] = None,
        quality: str = "Best",
        abort: Optional[threading.Event] = None,
        on_metadata: Optional[Callable[[dict], None]] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        on_complete: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """Download *url* to *output_dir* as a merged MP4.

        Returns the final path of the renamed MP4, or ``None`` on failure.
        Progress / metadata / errors are delivered through the callbacks.
        """
        if not validate_url(url):
            self._report(on_error, "Not a valid Douyin URL.")
            return None
        if not self.ffmpeg_path:
            self._report(
                on_error,
                "ffmpeg not found. It is required to merge video + audio. "
                "Install ffmpeg and add it to PATH, or set FFMPEG_PATH.",
            )
            return None

        os.makedirs(output_dir, exist_ok=True)

        # Reuse pre-fetched metadata when supplied (avoids a duplicate fetch).
        if info is None:
            try:
                info = self.fetch_metadata(url, on_warning=on_warning)
            except DownloaderError as exc:
                self._report(on_error, str(exc))
                return None
        if on_metadata:
            on_metadata(info)

        captured: list[str] = []  # final file path reported by the hook

        def _progress_hook(d: dict) -> None:
            if abort is not None and abort.is_set():
                # Stop the transfer; the GUI treats this as a pause.
                raise _DownloadAborted()
            if d.get("status") == "finished":
                # The last 'finished' event is the post-merge output file.
                captured.append(d.get("filename"))
            if on_progress:
                on_progress(d)

        opts = self._base_opts()
        fmt = QUALITY_TO_FORMAT.get(quality, DEFAULT_FORMAT)
        opts.update({
            "format": fmt,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "retries": RETRIES,
            "progress_hooks": [_progress_hook],
            # Temp name; we rename to creator_title_date.mp4 afterwards.
            "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        })

        try:
            info = self._extract_with_fallback(
                url, download=True, opts=opts, on_warning=on_warning)
        except _DownloadAborted:
            # Paused: leave the partial file for resume, signal "not done".
            return None
        except yt_dlp.utils.YoutubeDLError as exc:
            # Covers download errors AND post-processing (merge) failures.
            self._report(on_error, f"Download failed: {exc}")
            return None

        norm = self._normalize_info(info)

        # Resolve the path of the file yt-dlp actually wrote.
        downloaded = self._resolve_output_path(norm, captured)
        if not downloaded or not os.path.isfile(downloaded):
            self._report(on_error, "Download finished but output file not found.")
            return None

        final_path = self._build_target_path(output_dir, norm)
        try:
            shutil.move(downloaded, final_path)
        except OSError as exc:
            self._report(on_error, f"Failed to rename output file: {exc}")
            return None

        if on_complete:
            on_complete(final_path)
        return final_path

    # -- internals ---------------------------------------------------------- #

    @staticmethod
    def _report(callback, message: str) -> None:
        if callback:
            callback(message)

    def _base_opts(self, simulate: bool = False, use_cookies: bool = True) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": RETRIES,
            "ffmpeg_location": self.ffmpeg_path,
        }
        if use_cookies:
            if self.cookies:
                # Netscape-format cookies.txt — most reliable, no browser lock.
                opts["cookies"] = self.cookies
            elif self.cookiesfrombrowser:
                # Pull session cookies from the browser so login-gated or
                # rate-limited Douyin content can be fetched.
                opts["cookiesfrombrowser"] = self.cookiesfrombrowser
        if simulate:
            opts["simulate"] = True  # skip the actual download
        return opts

    # -- cookie handling ---------------------------------------------------- #

    def _cookie_source(self) -> Optional[str]:
        """Return ``"file"``, ``"browser"`` or ``None`` for the active source."""
        if self.cookies:
            return "file"
        if self.cookiesfrombrowser:
            return "browser"
        return None

    @staticmethod
    def _is_cookie_error(exc: Exception) -> bool:
        """Heuristic: did this yt-dlp error come from cookie loading?"""
        msg = str(exc).lower()
        return (
            "cookie" in msg
            or "dpapi" in msg
            or "failed to decrypt" in msg
        )

    @staticmethod
    def _is_dpapi_error(exc: Exception) -> bool:
        """Heuristic: is this specifically a Windows DPAPI decrypt failure?"""
        msg = str(exc).lower()
        return "dpapi" in msg or "failed to decrypt" in msg

    def _extract_with_fallback(self, url, download, opts, on_warning=None):
        """Run ``extract_info`` but retry without cookies on cookie errors.

        Returns the info dict. Raises :class:`DownloaderError` with an
        actionable message if even the cookie-less attempt fails (or if the
        failure was not cookie-related).
        """
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except yt_dlp.utils.YoutubeDLError as first_exc:
            if self._cookie_source() and self._is_cookie_error(first_exc):
                # Build a copy of the opts with all cookie settings stripped.
                opts_no_cookies = {k: v for k, v in opts.items()
                                   if k not in ("cookies", "cookiesfrombrowser")}
                try:
                    with yt_dlp.YoutubeDL(opts_no_cookies) as ydl:
                        info = ydl.extract_info(url, download=download)
                except yt_dlp.utils.YoutubeDLError as second_exc:
                    # Both failed: the video likely needs login cookies. Report
                    # the real second error instead of the cookie symptom.
                    guidance = (
                        DPAPI_GUIDANCE if self._is_dpapi_error(first_exc)
                        else COOKIE_GUIDANCE
                    )
                    raise DownloaderError(
                        f"Could not read browser cookies ({first_exc}). "
                        f"Without cookies it also failed: {second_exc}. {guidance}"
                    ) from second_exc
                self._report(
                    on_warning,
                    "Browser cookies unavailable (browser may be open / locked); "
                    "continuing without them.",
                )
                return info
            raise DownloaderError(str(first_exc)) from first_exc

    @staticmethod
    def _normalize_info(info: dict) -> dict:
        """Return *info* with consistent ``title`` / ``creator`` keys added."""
        title = None
        for key in TITLE_KEYS:
            if info.get(key):
                title = info[key]
                break
        creator = None
        for key in CREATOR_KEYS:
            if info.get(key):
                creator = info[key]
                break

        info = dict(info)  # copy so we never mutate the source
        info["title"] = sanitize_filename(title) if title else "Untitled"
        info["creator"] = sanitize_filename(creator) if creator else "UnknownCreator"
        return info

    @staticmethod
    def _resolve_output_path(norm: dict, captured: list[str]) -> Optional[str]:
        """Find the real output file path from yt-dlp's result / hooks."""
        if norm.get("requested_downloads"):
            path = norm["requested_downloads"][0].get("filepath")
            if path:
                return path
        if captured:
            return captured[-1]
        if norm.get("filepath"):
            return norm["filepath"]
        return None

    @staticmethod
    def _build_target_path(output_dir: str, norm: dict) -> str:
        """Build ``creator_title_date.mp4`` and avoid clobbering existing files."""
        creator = norm.get("creator", "UnknownCreator")
        title = norm.get("title", "Untitled")
        date = datetime.now().strftime(DATE_FMT)
        base = os.path.join(output_dir, f"{creator}_{title}_{date}.mp4")
        if not os.path.exists(base):
            return base
        stem, ext = os.path.splitext(base)
        i = 1
        while os.path.exists(f"{stem}_{i}{ext}"):
            i += 1
        return f"{stem}_{i}{ext}"
