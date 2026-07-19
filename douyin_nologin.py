"""douyin_nologin.py - Login-free Douyin download via headless Chromium.

Douyin serves almost every video behind a *signed* API that the page's own
JavaScript calls after receiving anonymous session cookies (``ttwid``,
``odin_tt``, ``passport_csrf_token``). Those are set automatically by a real
browser on visit — they are **not** a logged-in ``sessionid``. yt-dlp and a
bare ``urllib`` fetch both fail because they have no cookie jar / can't run the
SPA's JS, but a headless browser gets the cookies and lets the SPA render.

So the strategy is:
  1. Launch Chromium, load the share page (SPA runs, anonymous cookies set).
  2. Intercept the network: capture the aweme-detail API JSON (which carries
     the play URL + title/creator) and/or the real ``.mp4`` CDN response.
  3. Download that CDN URL with the browser context's cookies + a Douyin
     ``Referer``.

Output is typically the **clean (no-watermark)** ``play_addr`` rendition that
Douyin's API exposes — a bonus of the login-free path. Some videos are fully
gated behind a login wall / captcha and will fail with a clear error rather than
hang.
"""

from __future__ import annotations

import datetime
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
from typing import Callable, Optional

from downloader import (
    detect_ffmpeg,
    sanitize_filename,
    validate_url,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Hosts that serve Douyin/iXigua video bytes. Used to filter media responses so
# we don't grab avatars/ads/images by accident.
MEDIA_HOST_HINTS = (
    "byteimg.com", "douyinvod.com", "douyincdn.com", "zjcdn.com",
    "ixigua.com", "toutiao.com", "douyin.com", "bytedance",
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class NoLoginError(Exception):
    """User-facing error raised when login-free capture/download fails."""


class NoLoginCancelled(Exception):
    """Raised when a login-free download is paused/aborted mid-transfer.

    The partial ``.part`` file is left on disk so the transfer can resume later.
    """


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class DouyinNoLoginDownloader:
    """Login-free Douyin downloader built on a headless browser."""

    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        headless: bool = True,
        timeout: int = 45,
    ):
        # ffmpeg is optional here: Douyin CDN URLs are already muxed MP4.
        self.ffmpeg_path = ffmpeg_path or detect_ffmpeg()
        self.headless = headless
        self.timeout = timeout

    # -- public API --------------------------------------------------------- #

    def resolve_video_url(
        self, url: str, timeout: Optional[int] = None
    ) -> tuple[Optional[str], dict, list]:
        """Return ``(cdn_url, normalized_meta, formats)`` without downloading.

        ``formats`` is a list of ``{"label", "url", "width", "height"}`` (sorted
        highest-first). Useful for a metadata / resolution preview. Launches its
        own browser session.
        """
        timeout = timeout or self.timeout
        cdn_url, meta, _cookies, formats = self._capture(url, timeout)
        return cdn_url, self._normalize_meta(meta, url), formats

    def list_formats(self, url: str, timeout: Optional[int] = None):
        """Convenience wrapper: return ``(formats, normalized_meta)``."""
        timeout = timeout or self.timeout
        _cdn, meta, _cookies, formats = self._capture(url, timeout)
        return formats, self._normalize_meta(meta, url)

    def download(
        self,
        url: str,
        output_dir: str,
        *,
        info: Optional[dict] = None,
        quality: object = "best",
        on_formats: Optional[Callable[[list, dict], None]] = None,
        on_metadata: Optional[Callable[[dict], None]] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        on_complete: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """Download *url* (login-free) to *output_dir* as a merged MP4.

        Convenience wrapper: captures the video, then streams it. For pause /
        resume without re-launching the browser, use :meth:`resolve` +
        :meth:`download_resolved` directly.
        """
        if not validate_url(url):
            self._report(on_error, "Not a valid Douyin URL.")
            return None
        try:
            resolved = self.resolve(url)
        except NoLoginError as exc:
            self._report(on_error, str(exc))
            return None
        return self.download_resolved(
            resolved, output_dir,
            quality=quality,
            on_formats=on_formats, on_metadata=on_metadata,
            on_progress=on_progress, on_complete=on_complete,
            on_error=on_error, on_warning=on_warning,
        )

    def resolve(self, url: str) -> tuple[str, str, list, dict]:
        """Capture the video and return ``(cdn_url, cookie_header, formats, norm)``.

        Launches the browser once and caches everything the download needs so a
        paused / resumed item can re-stream without re-launching Chromium.
        Raises :class:`NoLoginError` if the browser fails or the video is gated.
        """
        if not validate_url(url):
            raise NoLoginError("Not a valid Douyin URL.")
        try:
            cdn_url, meta, cookies, formats = self._capture(url, self.timeout)
        except Exception as exc:  # playwright/launch failure
            raise NoLoginError(
                f"Browser failed to start ({exc}). Install with: "
                "pip install playwright && playwright install chromium"
            ) from exc

        if not cdn_url:
            raise NoLoginError(
                "Could not load the video without login (login wall / captcha). "
                "This video may require an account — try the cookie mode instead."
            )

        cookie_header = "; ".join(
            f"{c.get('name')}={c.get('value')}" for c in cookies
        )
        norm = self._normalize_meta(meta, url)
        return cdn_url, cookie_header, formats, norm

    def download_resolved(
        self,
        resolved: tuple[str, str, list, dict],
        output_dir: str,
        *,
        quality: object = "best",
        abort: Optional[threading.Event] = None,
        on_formats: Optional[Callable[[list, dict], None]] = None,
        on_metadata: Optional[Callable[[dict], None]] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        on_complete: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """Stream a previously :meth:`resolve`d video to *output_dir*.

        *quality* selects the rendition by label (e.g. ``"720p"``) or ``"best"``
        (highest). Supports pause via *abort* (raises, caller treats as paused)
        and resume from the existing ``.part`` file via HTTP Range.
        """
        cdn_url, cookie_header, formats, norm = resolved
        if not cdn_url:
            self._report(on_error, "No playable URL was captured.")
            return None

        if on_formats:
            on_formats(formats, norm)
        if on_metadata:
            on_metadata(norm)

        chosen = self._pick_url(formats, cdn_url, quality)
        if quality not in ("best", "Best", None):
            norm = dict(norm)
            norm["quality"] = self._quality_label(formats, quality, chosen)

        temp = os.path.join(output_dir, f"{norm['id']}.part")
        try:
            self._stream_download(chosen, cookie_header, temp, on_progress, abort)
        except NoLoginCancelled:
            # Paused: leave the partial .part for resume, signal "not done".
            return None
        except urllib.error.HTTPError as exc:
            self._report(
                on_error,
                f"Download failed (HTTP {exc.code}). The video URL may have "
                "expired or require an account.",
            )
            return None
        except Exception as exc:
            self._report(on_error, f"Download failed: {exc}")
            return None

        if not os.path.isfile(temp) or os.path.getsize(temp) == 0:
            self._report(on_error, "Download finished but the file is empty.")
            return None

        final = self._build_target_path(output_dir, norm)
        try:
            shutil.move(temp, final)
        except OSError as exc:
            self._report(on_error, f"Failed to rename output file: {exc}")
            return None

        if on_complete:
            on_complete(final)
        return final

    # -- capture (browser session) ----------------------------------------- #

    def _capture(self, url: str, timeout: int) -> tuple[Optional[str], dict, list, list]:
        """Open a browser, render the page, and intercept the video URLs.

        Returns ``(cdn_url, meta, cookies, formats)``. ``cdn_url`` is ``None``
        if the page never exposed a playable video (login wall / captcha).
        ``formats`` lists every available rendition (highest-first).
        """
        from playwright.sync_api import sync_playwright

        captured: dict = {"url": None, "meta": {}, "cookies": [], "formats": []}

        def on_response(response) -> None:
            # 1) API JSON carrying the aweme play URLs + metadata.
            if not captured["url"]:
                if _is_aweme_api(response):
                    try:
                        data = response.json()
                    except Exception:
                        return
                    play = _find_play_url(data)
                    if play:
                        captured["url"] = play
                        captured["meta"] = _find_meta(data)
                        captured["formats"] = _extract_formats(data)
            # 2) The actual CDN media response (covers MediaSource/blob cases).
            if not captured.get("media") and _is_media(response):
                captured["media"] = response.url

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=UA,
                locale="zh-CN",
                accept_downloads=False,
            )
            page = context.new_page()
            page.on("response", on_response)
            try:
                page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout * 1000
                )
            except Exception:
                # Navigation may "time out" on a captcha wall; we still inspect
                # whatever responses arrived.
                pass
            # Nudge lazy loads, then poll for a captured URL.
            try:
                page.evaluate("window.scrollTo(0, 300)")
            except Exception:
                pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if captured.get("url") or captured.get("media"):
                    break
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break
            captured["cookies"] = context.cookies()
            browser.close()

        cdn_url = captured.get("media") or captured.get("url")
        return (
            cdn_url,
            captured.get("meta", {}),
            captured.get("cookies", []),
            captured.get("formats", []),
        )

    # -- download (stream) -------------------------------------------------- #

    def _stream_download(
        self, url: str, cookie_header: str, dest: str,
        on_progress: Optional[Callable[[dict], None]],
        abort: Optional[threading.Event] = None,
    ) -> int:
        # Resume from a partial file if one already exists (Pause -> Continue).
        existing = os.path.getsize(dest) if os.path.isfile(dest) else 0
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.douyin.com/",
            "Cookie": cookie_header,
            "Accept": "*/*",
        }
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            # 206 = partial content (true resume); 200 = server ignored Range,
            # so we restart from scratch.
            if resp.status == 200 and existing > 0:
                done = 0
                mode = "wb"
            else:
                done = existing
                mode = "ab" if existing > 0 else "wb"
            total = existing + int(resp.headers.get("Content-Length") or 0)
            with open(dest, mode) as fh:
                while True:
                    if abort is not None and abort.is_set():
                        raise NoLoginCancelled()
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress({
                            "status": "downloading",
                            "downloaded_bytes": done,
                            "total_bytes": total,
                            "speed": None,
                            "eta": None,
                        })
        if on_progress:
            on_progress({
                "status": "finished",
                "downloaded_bytes": done,
                "total_bytes": total,
            })
        return done

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _report(callback, message: str) -> None:
        if callback:
            callback(message)

    @staticmethod
    def _normalize_meta(meta: dict, url: str) -> dict:
        title = sanitize_filename(meta.get("title")) or "Untitled"
        creator = sanitize_filename(meta.get("creator")) or "UnknownCreator"
        vid = meta.get("id") or (abs(hash(url)) % 10 ** 12)
        return {"title": title, "creator": creator, "id": vid}

    @staticmethod
    def _build_target_path(output_dir: str, norm: dict) -> str:
        creator = norm.get("creator", "UnknownCreator")
        title = norm.get("title", "Untitled")
        date = datetime.datetime.now().strftime("%Y%m%d")
        q = norm.get("quality")
        stem_name = f"{creator}_{title}_{date}" + (f"_{q}" if q else "")
        base = os.path.join(output_dir, f"{stem_name}.mp4")
        if not os.path.exists(base):
            return base
        stem, ext = os.path.splitext(base)
        i = 1
        while os.path.exists(f"{stem}_{i}{ext}"):
            i += 1
        return f"{stem}_{i}{ext}"

    def _pick_url(self, formats: list, default_url: str, quality: object) -> str:
        """Resolve *quality* to a concrete CDN URL."""
        if not formats:
            return default_url
        if quality in ("best", "Best", None):
            return formats[0]["url"]  # formats are sorted highest-first
        if isinstance(quality, int) and 0 <= quality < len(formats):
            return formats[quality]["url"]
        if isinstance(quality, str):
            for f in formats:
                if f["label"] == quality:
                    return f["url"]
        return formats[0]["url"]

    @staticmethod
    def _quality_label(formats: list, quality: object, chosen_url: str) -> Optional[str]:
        """Best-effort label for the chosen quality (for the filename)."""
        for f in formats:
            if f["url"] == chosen_url:
                return f["label"]
        return None


# --------------------------------------------------------------------------- #
# Response classification helpers
# --------------------------------------------------------------------------- #

def _is_aweme_api(response) -> bool:
    url = response.url
    ctype = (response.headers.get("content-type") or "").lower()
    return (
        ("aweme" in url or "/video/" in url)
        and ("json" in ctype or "javascript" in ctype or url.endswith(".json"))
    )


def _is_media(response) -> bool:
    url = response.url.lower()
    # Douyin CDN URLs carry a query string (...&req_cdn_type=), so check the
    # *path* for the .mp4 extension, not the whole URL.
    path = urllib.parse.urlparse(url).path
    if not path.endswith(".mp4"):
        return False
    host = urllib.parse.urlparse(url).netloc
    return any(hint in host for hint in MEDIA_HOST_HINTS)


def _find_play_url(node):
    """Recursively find the first ``play_addr``/``playwm`` ``url_list[0]``."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in ("play_addr", "playwm"):
                val = cur.get(key)
                if isinstance(val, dict) and val.get("url_list"):
                    return val["url_list"][0]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _find_meta(node) -> dict:
    """Find the aweme item carrying a desc + author/nickname."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "desc" in cur and ("author" in cur or "nickname" in cur):
                author = cur.get("author") or {}
                nickname = author.get("nickname") if isinstance(author, dict) else None
                return {
                    "title": cur.get("desc"),
                    "creator": nickname or cur.get("nickname"),
                    "id": cur.get("aweme_id") or cur.get("awemeId"),
                }
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return {}


def _extract_formats(node) -> list:
    """Collect the available renditions from the aweme video object.

    Douyin exposes many near-duplicate encodes per resolution (different
    bitrates / gears). We bucket them by resolution and keep the highest-bitrate
    encode of each, producing a clean list like ``1080p / 720p / 540p``.

    Returns a list of ``{"label", "url", "width", "height", "bitrate"}`` sorted
    highest-resolution first.
    """
    # bucket keyed by the conventional resolution number (min dimension).
    buckets: dict = {}
    stack = [node]
    videos = []
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            # A "video" object has a real play_addr dict AND a list-typed
            # bit_rate. This avoids matching inner bit_rate entries (int).
            if isinstance(cur.get("play_addr"), dict) and isinstance(
                cur.get("bit_rate"), list
            ):
                videos.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)

    def consider(pa: dict, bitrate: int) -> None:
        if not (isinstance(pa, dict) and pa.get("url_list")):
            return
        h = pa.get("height") or 0
        w = pa.get("width") or 0
        res = min(w, h) or max(w, h)  # conventional label uses smaller dim
        if not res:
            return
        prev = buckets.get(res)
        if prev is None or bitrate > prev["bitrate"]:
            buckets[res] = {
                "label": f"{res}p",
                "url": pa["url_list"][0],
                "width": w, "height": h, "bitrate": bitrate,
            }

    for v in videos:
        # Top-level play_addr (usually the default/best clean rendition).
        consider(v.get("play_addr"), bitrate=1)
        for br in v.get("bit_rate") or []:
            if isinstance(br, dict):
                consider(br.get("play_addr"), bitrate=int(br.get("bit_rate") or 0))

    # Fallback: no structured video object — surface any loose play_addr.
    if not buckets:
        _collect_loose_play_addrs(node, buckets)

    return sorted(buckets.values(), key=lambda f: f["height"], reverse=True)


def _collect_loose_play_addrs(node, buckets: dict) -> None:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in ("play_addr", "play_addr_h264", "play_addr_low", "playwm"):
                pa = cur.get(key)
                if isinstance(pa, dict) and pa.get("url_list"):
                    h = pa.get("height") or 0
                    w = pa.get("width") or 0
                    res = min(w, h) or max(w, h) or len(buckets)
                    buckets.setdefault(res, {
                        "label": f"{res}p" if res else "video",
                        "url": pa["url_list"][0],
                        "width": w, "height": h, "bitrate": 0,
                    })
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
