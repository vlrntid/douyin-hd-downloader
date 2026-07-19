"""gui.py - CustomTkinter GUI for Douyin HD Downloader.

A thin, event-driven view over :class:`downloader.DouyinDownloader` and
:class:`douyin_nologin.DouyinNoLoginDownloader`. All network/CPU-heavy work runs
in a background thread; widget updates are scheduled back onto the main Tk thread
via ``after``.

Downloads run through an **inline queue**: paste a link and hit Download to
enqueue it. Items run one-at-a-time in order; you can keep adding more while one
is downloading. Each item has its own Pause/Resume and Remove controls. Pausing
stops the active transfer and leaves the partial ``.part`` file so Resume can
continue from where it left off.
"""

from __future__ import annotations

import itertools
import os
import threading

import customtkinter as ctk

from cookie_manager import CookieError, CookieManager
from downloader import (
    DouyinDownloader,
    DownloaderError,
    detect_ffmpeg,
    extract_video_url,
    validate_url,
)
from douyin_nologin import DouyinNoLoginDownloader, NoLoginError
from settings import load_settings, save_settings
from updater import (
    APP_VERSION,
    apply_app_update,
    check_app_update,
    check_ytdlp,
    default_downloads_dir,
    installed_ytdlp_version,
    self_repair_ytdlp,
)

ERROR_COLOR = "#e74c3c"
WARN_COLOR = "#f39c12"
DONE_COLOR = "#2ecc71"
IDLE_COLOR = ("gray60", "gray40")
ACCENT_COLOR = ("#3a7ebf", "#1f6aa5")
SIDEBAR_COLOR = ("gray85", "gray17")
NAV_ACTIVE_COLOR = ("gray75", "gray25")

DEFAULT_DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")

# Resolution choices offered up-front (no "resolve first" step). On download the
# label is mapped to a real rendition; if unavailable we fall back to Best.
QUALITY_CHOICES = ["Best", "2k", "1080p", "720p", "540p", "360p"]

# Browser -> yt-dlp cookiesfrombrowser tuple. "None" disables browser cookies.
BROWSER_MAP = {
    "Brave": ("brave",),
    "Chrome": ("chrome",),
    "Edge": ("edge",),
    "Firefox": ("firefox",),
    "None": None,
}


# --------------------------------------------------------------------------- #
# Queue item model
# --------------------------------------------------------------------------- #

class QueueItem:
    """One download in the queue, plus its row widgets and control state."""

    _ids = itertools.count(1)

    def __init__(self, url: str, quality: str, use_nologin: bool):
        self.id = next(self._ids)
        self.url = url
        self.quality = quality
        self.use_nologin = use_nologin
        self.status = "queued"  # queued|downloading|paused|done|error
        self.message = ""
        self.title = url
        self.progress = 0.0
        self.abort = threading.Event()
        self.final_path: str | None = None
        # Cached no-login capture (cdn_url, cookie_header, formats, norm) so a
        # paused/resumed item can re-stream without re-launching the browser.
        self.resolved = None
        self.widgets: dict = {}


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

class DouyinDownloaderApp(ctk.CTk):
    """Main application window."""

    def __init__(self):
        super().__init__()

        self.title("Douyin HD Downloader")
        self.geometry("880x680")
        self.minsize(640, 560)
        self.appearance_mode = "System"
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.downloader = DouyinDownloader()
        # Persistent settings (survive updates / rebuilds).
        self.settings = load_settings()
        self.download_dir = (
            self.settings.get("download_dir") or default_downloads_dir()
            or DEFAULT_DOWNLOAD_DIR
        )
        self.cookie_manager = CookieManager()
        # Bring the downloader's cookies into sync with the manager.
        self.downloader.cookies = self.cookie_manager.get_cookies_for_yt_dlp()
        self.cookies_file = self.downloader.cookies  # optional Netscape cookies.txt
        # No-login (browser capture) is the default so Douyin works without a
        # login / DPAPI; the cookie path stays available as a fallback.
        self.nologin_var = ctk.BooleanVar(
            value=bool(self.settings.get("nologin_default", True)))
        self.quality_var = ctk.StringVar(value="Best")

        # Queue state.
        self.queue: list[QueueItem] = []
        self._active: QueueItem | None = None

        os.makedirs(self.download_dir, exist_ok=True)

        self._build_widgets()
        self._refresh_ffmpeg_status()
        self._refresh_cookie_label()
        # Apply the persisted no-login default to the cookie UI state.
        self.on_nologin_change()
        # Kick off a background update / self-repair check.
        self._start_update_check()

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_widgets(self) -> None:
        self.grid_columnconfigure(0, weight=0)  # sidebar (fixed)
        self.grid_columnconfigure(1, weight=1)  # content (fills)
        self.grid_rowconfigure(0, weight=1)

        pad = 18

        # ------------------------------------------------------------------ #
        # Sidebar
        # ------------------------------------------------------------------ #
        self.sidebar = ctk.CTkFrame(
            self, width=210, corner_radius=0, fg_color=SIDEBAR_COLOR)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(5, weight=1)  # spacer pushes footer down
        self.sidebar.grid_propagate(False)

        ctk.CTkLabel(
            self.sidebar, text="Douyin HD",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, padx=pad, pady=(pad, 0), sticky="w")
        ctk.CTkLabel(
            self.sidebar, text="Downloader",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=ACCENT_COLOR,
        ).grid(row=1, column=0, padx=pad, pady=(0, 18), sticky="w")

        self.nav_buttons: dict = {}
        for i, (key, label) in enumerate(
            [("download", "Download"), ("queue", "Queue"), ("settings", "Settings")],
            start=2,
        ):
            btn = ctk.CTkButton(
                self.sidebar, text=label, height=40, anchor="w",
                corner_radius=8, fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=NAV_ACTIVE_COLOR,
                command=lambda k=key: self._show_page(k),
            )
            btn.grid(row=i, column=0, padx=pad, pady=4, sticky="ew")
            self.nav_buttons[key] = btn

        ctk.CTkLabel(
            self.sidebar, text="Theme", text_color=IDLE_COLOR,
        ).grid(row=6, column=0, padx=pad, pady=(4, 0), sticky="w")
        self.appearance_var = ctk.StringVar(value="System")
        ctk.CTkOptionMenu(
            self.sidebar, variable=self.appearance_var,
            values=["System", "Light", "Dark"], width=160,
            command=self._on_appearance_change,
        ).grid(row=7, column=0, padx=pad, pady=(2, pad), sticky="ew")

        # ------------------------------------------------------------------ #
        # Content area (pages + status bar)
        # ------------------------------------------------------------------ #
        self.content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        # ---- Download page -------------------------------------------------
        self.download_page = ctk.CTkFrame(
            self.content, fg_color="transparent", corner_radius=0)
        self.download_page.grid(
            row=0, column=0, sticky="nsew", padx=pad, pady=pad)
        self.download_page.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self.download_page, text="Download",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, padx=0, pady=(0, 4), sticky="w")
        ctk.CTkLabel(
            self.download_page,
            text="Paste a Douyin or YouTube link and click Download to add it to the queue.",
            text_color=IDLE_COLOR,
        ).grid(row=1, column=0, padx=0, pady=(0, 12), sticky="w")

        # URL entry
        self.url_entry = ctk.CTkEntry(
            self.download_page,
            placeholder_text="https://v.douyin.com/5EN2Wx7HDT8/ or https://youtu.be/abcd1234",
            height=42,
        )
        self.url_entry.grid(row=2, column=0, padx=0, pady=(0, 12), sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self.add_to_queue())

        # Cookie / mode / quality settings
        cookies_frame = ctk.CTkFrame(self.download_page)
        cookies_frame.grid(row=3, column=0, padx=0, pady=(0, 12), sticky="ew")
        cookies_frame.grid_columnconfigure(1, weight=1)
        cookies_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(cookies_frame, text="Browser:").grid(
            row=0, column=0, padx=12, pady=(10, 4), sticky="w")
        self.browser_var = ctk.StringVar(
            value=self.settings.get("preferred_browser", "Chrome"))
        self.browser_menu = ctk.CTkOptionMenu(
            cookies_frame, variable=self.browser_var,
            values=list(BROWSER_MAP.keys()), width=110,
            command=self.on_browser_change,
        )
        self.browser_menu.grid(row=0, column=1, padx=12, pady=(10, 4), sticky="w")

        ctk.CTkLabel(cookies_frame, text="Cookies file:").grid(
            row=1, column=0, padx=12, pady=(4, 10), sticky="w")
        self.cookies_path = ctk.CTkLabel(
            cookies_frame, text="(none — using browser)", anchor="w")
        self.cookies_path.grid(row=1, column=1, columnspan=2, padx=12, pady=(4, 10), sticky="ew")
        self.cookies_browse_btn = ctk.CTkButton(
            cookies_frame, text="Browse", width=100, command=self.choose_cookies,
        )
        self.cookies_browse_btn.grid(row=1, column=3, padx=12, pady=(4, 10), sticky="e")

        # "Import Cookies" — extract cookies via a fresh browser login (no DPAPI).
        self.import_cookies_btn = ctk.CTkButton(
            cookies_frame, text="Import Cookies",
            width=130, command=self.import_cookies,
        )
        self.import_cookies_btn.grid(
            row=2, column=3, padx=12, pady=(0, 10), sticky="e")
        ctk.CTkLabel(
            cookies_frame,
            text="Opens your browser to log in, then saves cookies.txt "
                 "(avoids browser DB DPAPI errors).",
            text_color=IDLE_COLOR, anchor="w",
        ).grid(row=2, column=1, columnspan=2, padx=12, pady=(0, 10), sticky="w")

        # No-login (watermarked) toggle.
        self.nologin_switch = ctk.CTkSwitch(
            cookies_frame, text="No login (watermarked)", variable=self.nologin_var,
            command=self.on_nologin_change,
        )
        self.nologin_switch.grid(
            row=3, column=0, columnspan=2, padx=12, pady=(0, 10), sticky="w")
        ctk.CTkLabel(
            cookies_frame, text="Renders the page in Chromium — no account needed.",
            text_color=IDLE_COLOR, anchor="w",
        ).grid(row=3, column=2, columnspan=2, padx=12, pady=(0, 10), sticky="ew")

        # Quality picker — always available; applies to both paths.
        ctk.CTkLabel(cookies_frame, text="Quality:").grid(
            row=4, column=0, padx=12, pady=(0, 10), sticky="w")
        self.quality_menu = ctk.CTkOptionMenu(
            cookies_frame, variable=self.quality_var,
            values=QUALITY_CHOICES, width=160,
        )
        self.quality_menu.grid(
            row=4, column=1, columnspan=2, padx=12, pady=(0, 10), sticky="w")
        ctk.CTkLabel(
            cookies_frame, text="Falls back to Best if unavailable.",
            text_color=IDLE_COLOR, anchor="w",
        ).grid(row=4, column=3, padx=12, pady=(0, 10), sticky="e")

        # Download folder selector
        folder_frame = ctk.CTkFrame(self.download_page)
        folder_frame.grid(row=4, column=0, padx=0, pady=(0, 12), sticky="ew")
        folder_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(folder_frame, text="Save to:").grid(
            row=0, column=0, padx=12, pady=10, sticky="w")
        self.folder_path = ctk.CTkLabel(folder_frame, text=self.download_dir, anchor="w")
        self.folder_path.grid(row=0, column=1, padx=12, pady=10, sticky="ew")
        ctk.CTkButton(
            folder_frame, text="Browse", width=100, command=self.choose_folder,
        ).grid(row=0, column=2, padx=12, pady=10, sticky="e")

        # Action row: Download (enqueue) + Clear finished
        action_frame = ctk.CTkFrame(self.download_page, fg_color="transparent")
        action_frame.grid(row=5, column=0, padx=0, pady=(0, 0), sticky="ew")
        action_frame.grid_columnconfigure(0, weight=1)
        self.download_btn = ctk.CTkButton(
            action_frame, text="Download", height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.add_to_queue,
        )
        self.download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            action_frame, text="Clear finished", width=130, height=42,
            fg_color=("gray70", "gray30"), hover_color=("gray60", "gray40"),
            command=self.clear_finished,
        ).grid(row=0, column=1, sticky="e")

        # ---- Queue page ---------------------------------------------------
        self.queue_page = ctk.CTkFrame(
            self.content, fg_color="transparent", corner_radius=0)
        self.queue_page.grid(
            row=0, column=0, sticky="nsew", padx=pad, pady=pad)
        self.queue_page.grid_columnconfigure(0, weight=1)
        self.queue_page.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self.queue_page, text="Queue",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, padx=0, pady=(0, 8), sticky="w")

        self.queue_frame = ctk.CTkScrollableFrame(self.queue_page, label_text="")
        self.queue_frame.grid(row=1, column=0, sticky="nsew")
        self.queue_frame.grid_columnconfigure(0, weight=1)

        self.empty_label = ctk.CTkLabel(
            self.queue_frame, text="Queue is empty — add a link from the Download tab.",
            text_color=IDLE_COLOR,
        )
        self.empty_label.grid(row=0, column=0, padx=12, pady=12, sticky="w")

        # ---- Settings page -------------------------------------------------
        self.settings_page = ctk.CTkFrame(
            self.content, fg_color="transparent", corner_radius=0)
        self.settings_page.grid(
            row=0, column=0, sticky="nsew", padx=pad, pady=pad)
        self.settings_page.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self.settings_page, text="Settings",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, padx=0, pady=(0, 12), sticky="w")

        ffmpeg_box = ctk.CTkFrame(self.settings_page)
        ffmpeg_box.grid(row=1, column=0, padx=0, pady=(0, 12), sticky="ew")
        ffmpeg_box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            ffmpeg_box, text="ffmpeg", font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(10, 0), sticky="w")
        self.ffmpeg_label = ctk.CTkLabel(
            ffmpeg_box, text="", text_color="gray", anchor="w")
        self.ffmpeg_label.grid(row=1, column=0, padx=12, pady=(2, 10), sticky="ew")

        about_box = ctk.CTkFrame(self.settings_page)
        about_box.grid(row=2, column=0, padx=0, pady=(0, 12), sticky="ew")
        about_box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            about_box, text="About", font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")
        ctk.CTkLabel(
            about_box,
            text=("Douyin HD Downloader\n"
                  "Downloads Douyin and YouTube videos with pause / resume, "
                  "quality selection, and a no-login mode.\n"
                  "Change the theme from the sidebar (bottom)."),
            text_color=IDLE_COLOR, anchor="w", justify="left",
        ).grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")

        # Updates management box.
        updates_box = ctk.CTkFrame(self.settings_page)
        updates_box.grid(row=3, column=0, padx=0, pady=(0, 12), sticky="ew")
        updates_box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            updates_box, text="Updates & Repair", font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 4), sticky="w")

        self.auto_update_var = ctk.BooleanVar(
            value=bool(self.settings.get("auto_update_ytdlp", True)))
        ctk.CTkSwitch(
            updates_box, text="Auto-update yt-dlp on startup",
            variable=self.auto_update_var, command=self._on_auto_update_toggle,
        ).grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 6), sticky="w")

        ctk.CTkLabel(updates_box, text="App update source:").grid(
            row=2, column=0, padx=12, pady=(0, 4), sticky="w")
        self.repo_entry = ctk.CTkEntry(
            updates_box, placeholder_text="owner/repo (GitHub) — blank = off",
            width=240)
        self.repo_entry.insert(0, self.settings.get("update_repo", "") or "")
        self.repo_entry.bind("<FocusOut>", self._on_repo_change)
        self.repo_entry.bind("<Return>", self._on_repo_change)
        self.repo_entry.grid(row=2, column=1, padx=12, pady=(0, 4), sticky="ew")

        self.ytdlp_version_label = ctk.CTkLabel(
            updates_box, text="yt-dlp: …", text_color=IDLE_COLOR, anchor="w")
        self.ytdlp_version_label.grid(
            row=3, column=0, columnspan=2, padx=12, pady=(0, 2), sticky="w")
        ctk.CTkLabel(
            updates_box, text=f"App version: {APP_VERSION}",
            text_color=IDLE_COLOR, anchor="w",
        ).grid(row=4, column=0, columnspan=2, padx=12, pady=(0, 6), sticky="w")

        ctk.CTkButton(
            updates_box, text="Check for Updates", width=160,
            command=self.check_updates_now,
        ).grid(row=5, column=0, padx=12, pady=(0, 6), sticky="w")
        # Hidden until a newer app release is detected.
        self.install_update_btn = ctk.CTkButton(
            updates_box, text="Install App Update", width=160,
            fg_color=DONE_COLOR, command=self.install_app_update,
        )
        # Start hidden; revealed by _show_app_update_button when an update exists.
        self.install_update_btn.grid(row=5, column=1, padx=12, pady=(0, 6), sticky="w")
        self.install_update_btn.grid_remove()
        self._pending_app_update = None
        self._pending_update_script = None
        self._refresh_version_labels()

        # Cookies management box.
        cookies_box = ctk.CTkFrame(self.settings_page)
        cookies_box.grid(row=4, column=0, padx=0, pady=(0, 12), sticky="ew")
        cookies_box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            cookies_box, text="Cookies", font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")
        ctk.CTkLabel(
            cookies_box,
            text=(
                "If downloads fail with a 'DPAPI' or 'Failed to decrypt' error, "
                "use 'Import Cookies' on the Download tab: it logs into your "
                "browser and saves a portable cookies.txt."
            ),
            text_color=IDLE_COLOR, anchor="w", justify="left",
        ).grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        self.refresh_cookies_btn = ctk.CTkButton(
            cookies_box, text="Refresh Cookies", width=160,
            command=self.refresh_cookies,
        )
        self.refresh_cookies_btn.grid(
            row=2, column=0, padx=12, pady=(0, 10), sticky="w")

        # ---- Status bar (visible on every page) ---------------------------
        self.status_bar = ctk.CTkFrame(
            self.content, height=32, corner_radius=0, fg_color=SIDEBAR_COLOR)
        self.status_bar.grid(row=1, column=0, sticky="ew")
        self.status_bar.grid_columnconfigure(0, weight=1)
        self.status_line = ctk.CTkLabel(
            self.status_bar, text="Idle", text_color=IDLE_COLOR,
            anchor="w", wraplength=640)
        self.status_line.grid(row=0, column=0, padx=12, pady=5, sticky="ew")

        self._show_page("download")

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def _show_page(self, name: str) -> None:
        """Raise one page (download/queue/settings) and hide the others."""
        pages = {
            "download": self.download_page,
            "queue": self.queue_page,
            "settings": self.settings_page,
        }
        for key, frame in pages.items():
            if key == name:
                frame.grid()
            else:
                frame.grid_remove()
        for key, btn in self.nav_buttons.items():
            active = key == name
            btn.configure(
                fg_color=NAV_ACTIVE_COLOR if active else "transparent")
        self.current_page = name

    def _on_appearance_change(self, choice: str) -> None:
        self.appearance_mode = choice
        ctk.set_appearance_mode(choice)

    # ------------------------------------------------------------------ #
    # Settings actions
    # ------------------------------------------------------------------ #

    def choose_folder(self) -> None:
        folder = ctk.filedialog.askdirectory(initialdir=self.download_dir)
        if folder:
            self.download_dir = folder
            self.folder_path.configure(text=folder)
            self._persist("download_dir", folder)

    def choose_cookies(self) -> None:
        path = ctk.filedialog.askopenfilename(
            title="Select cookies.txt",
            filetypes=[("Cookies", "*.txt"), ("All files", "*.*")],
            initialdir=self.download_dir,
        )
        if path:
            try:
                self.cookie_manager.set_cookies_file(path)
            except CookieError as exc:
                self._set_status(str(exc), ERROR_COLOR)
                return
            self.cookies_file = path
            self.downloader.cookies = self.cookie_manager.get_cookies_for_yt_dlp()
            self._refresh_cookie_label()

    def import_cookies(self) -> None:
        """Extract cookies from the selected browser via Playwright (no DPAPI)."""
        browser = self.browser_var.get()
        if browser == "None":
            browser = "Chrome"
        self._set_status(f"Importing cookies from {browser}…", IDLE_COLOR)
        self.import_cookies_btn.configure(state="disabled")
        threading.Thread(
            target=self._run_import_cookies, args=(browser,), daemon=True
        ).start()

    def _run_import_cookies(self, browser: str) -> None:
        try:
            saved = self.cookie_manager.extract_cookies_from_browser(
                browser, on_status=lambda m: self._set_status_async(m, IDLE_COLOR)
            )
            self.cookies_file = saved
            self.downloader.cookies = self.cookie_manager.get_cookies_for_yt_dlp()
            ok = self.cookie_manager.validate_douyin_cookies(saved)
            self.after(0, self._refresh_cookie_label)
            self._set_status_async(
                "Cookies imported. " + (
                    "Douyin login cookies detected." if ok
                    else "Tip: log in to Douyin in the opened window for full access."
                ),
                DONE_COLOR if ok else WARN_COLOR,
            )
        except CookieError as exc:
            self._set_status_async(str(exc), ERROR_COLOR)
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures
            self._set_status_async(f"Cookie import failed: {exc}", ERROR_COLOR)
        finally:
            self.after(0, lambda: self.import_cookies_btn.configure(state="normal"))

    def refresh_cookies(self) -> None:
        """Re-extract cookies from the selected browser (overwrites cookies.txt)."""
        # Import = extract from browser; Refresh re-runs the same flow.
        self.import_cookies()

    # ------------------------------------------------------------------ #
    # Updates / self-repair
    # ------------------------------------------------------------------ #

    def _start_update_check(self) -> None:
        """Run yt-dlp + app update checks and self-repair in the background."""
        threading.Thread(target=self._run_update_check, daemon=True).start()

    def _run_update_check(self, manual: bool = False) -> None:
        try:
            # 1) Self-repair: guarantee yt-dlp is importable before anything.
            self_repair_ytdlp(on_status=lambda m: self._set_status_async(m, IDLE_COLOR))

            # 2) yt-dlp version check / auto-update.
            auto = bool(self.settings.get("auto_update_ytdlp", True))
            result = check_ytdlp(
                auto_install=auto,
                on_status=lambda m: self._set_status_async(m, IDLE_COLOR),
            )
            if result.get("updated"):
                self._set_status_async(
                    f"yt-dlp updated to {result.get('latest')} — restart to apply.",
                    DONE_COLOR)
            self.after(0, self._refresh_version_labels)

            # 3) App self-update (only if a repo is configured).
            repo = (self.settings.get("update_repo") or "").strip()
            if repo:
                release = check_app_update(
                    repo, APP_VERSION,
                    on_status=lambda m: self._set_status_async(m, IDLE_COLOR),
                )
                if release and release.get("exe_url"):
                    self._pending_app_update = release
                    self._set_status_async(
                        f"App update {release['tag']} available — see Settings "
                        "→ Install App Update.", WARN_COLOR)
                    self.after(0, self._show_app_update_button)
            elif manual:
                self._set_status_async(
                    "No app-update source set (Settings → Update source).",
                    IDLE_COLOR)
        except Exception as exc:  # noqa: BLE001 - updates must never crash the app
            self._set_status_async(f"Update check skipped: {exc}", IDLE_COLOR)

    def check_updates_now(self) -> None:
        self._set_status("Checking for updates…", IDLE_COLOR)
        threading.Thread(
            target=self._run_update_check, kwargs={"manual": True}, daemon=True
        ).start()

    def _show_app_update_button(self) -> None:
        if hasattr(self, "install_update_btn"):
            self.install_update_btn.grid()

    def install_app_update(self) -> None:
        release = getattr(self, "_pending_app_update", None)
        if not release or not release.get("exe_url"):
            self._set_status("No app update is pending.", IDLE_COLOR)
            return
        self._set_status("Downloading app update…", IDLE_COLOR)
        threading.Thread(
            target=self._run_install_app_update,
            args=(release["exe_url"],), daemon=True,
        ).start()

    def _run_install_app_update(self, exe_url: str) -> None:
        script = apply_app_update(
            exe_url, on_status=lambda m: self._set_status_async(m, IDLE_COLOR))
        if script:
            self._pending_update_script = script
            self._set_status_async(
                "Update downloaded. Close the app to finish installing.",
                DONE_COLOR)

    def _refresh_version_labels(self) -> None:
        if hasattr(self, "ytdlp_version_label"):
            ver = installed_ytdlp_version() or "unknown"
            self.ytdlp_version_label.configure(text=f"yt-dlp: {ver}")

    def _on_auto_update_toggle(self) -> None:
        self._persist("auto_update_ytdlp", bool(self.auto_update_var.get()))

    def _on_repo_change(self, _event=None) -> None:
        repo = self.repo_entry.get().strip()
        self._persist("update_repo", repo)
        self._set_status(
            f"Update source set to: {repo}" if repo
            else "App-update source cleared.", IDLE_COLOR)

    def on_browser_change(self, choice: str) -> None:
        # A browser cookie DB read is what triggers DPAPI failures, so we no
        # longer pass cookiesfrombrowser to yt-dlp. The user imports cookies
        # explicitly via "Import Cookies" instead. We just remember the choice
        # for the next import/refresh.
        self.downloader.cookiesfrombrowser = None
        self._persist("preferred_browser", choice)
        self._refresh_cookie_label()

    def _persist(self, key: str, value) -> None:
        """Update one setting in memory and on disk (best-effort)."""
        self.settings[key] = value
        save_settings(self.settings)

    def on_nologin_change(self) -> None:
        on = self.nologin_var.get()
        state = "disabled" if on else "normal"
        self.browser_menu.configure(state=state)
        self.cookies_browse_btn.configure(state=state)
        self.import_cookies_btn.configure(state=state)
        if on:
            self.cookies_path.configure(text="(off — no-login mode)")
        else:
            self._refresh_cookie_label()
        self._persist("nologin_default", on)

    def _refresh_cookie_label(self) -> None:
        """Update the cookies file label from the cookie manager's state."""
        source = self.cookie_manager.get_cookie_source()
        if source == "file" and self.cookie_manager.cookies_path:
            self.cookies_path.configure(
                text=os.path.basename(self.cookie_manager.cookies_path))
        elif self.downloader.cookies:
            self.cookies_path.configure(
                text=os.path.basename(self.downloader.cookies))
        else:
            self.cookies_path.configure(text="(none — import cookies to enable)")

    # ------------------------------------------------------------------ #
    # Queue management
    # ------------------------------------------------------------------ #

    def add_to_queue(self) -> None:
        raw = self.url_entry.get().strip()
        link = extract_video_url(raw)
        if not link:
            self._set_status(
                "No Douyin/YouTube link found. Paste the share text or URL.",
                ERROR_COLOR)
            return
        if not validate_url(link):
            self._set_status(
                "Found a link, but it is not a Douyin or YouTube URL.",
                ERROR_COLOR)
            return

        link_l = link.lower()
        use_nologin = self.nologin_var.get() and (
            "douyin.com" in link_l or "iesdouyin.com" in link_l
        )

        item = QueueItem(link, self.quality_var.get(), use_nologin)
        self.queue.append(item)
        self._build_item_row(item)
        self.url_entry.delete(0, "end")
        self._set_status(f"Added to queue: {link}", IDLE_COLOR)
        self._pump()

    def clear_finished(self) -> None:
        for item in list(self.queue):
            if item.status in ("done", "error"):
                self._destroy_item(item)
        self._refresh_empty_label()

    def _pump(self) -> None:
        """Start the next queued item if nothing is currently downloading."""
        if self._active is not None:
            return
        nxt = next((i for i in self.queue if i.status == "queued"), None)
        if nxt is None:
            return
        self._active = nxt
        nxt.status = "downloading"
        nxt.abort.clear()
        self._update_row(nxt)
        threading.Thread(target=self._run_item, args=(nxt,), daemon=True).start()

    def pause_item(self, item: QueueItem) -> None:
        if item.status == "downloading":
            item.abort.set()
            self._set_status("Pausing…", IDLE_COLOR)
        elif item.status == "queued":
            # Not started yet: just hold it out of the run order.
            item.status = "paused"
            self._update_row(item)

    def resume_item(self, item: QueueItem) -> None:
        if item.status == "paused":
            item.status = "queued"
            item.abort.clear()
            self._update_row(item)
            self._pump()

    def remove_item(self, item: QueueItem) -> None:
        if item is self._active:
            item.abort.set()  # stop the transfer; worker's finally will pump
        self._destroy_item(item)
        self._refresh_empty_label()

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #

    def _run_item(self, item: QueueItem) -> None:
        try:
            if item.use_nologin:
                self._run_nologin(item)
            else:
                self._run_login(item)
        except Exception as exc:  # noqa: BLE001 - never let a worker die silently
            item.status = "error"
            item.message = f"Unexpected error: {exc}"
            self.after(0, lambda: self._update_row(item))
        finally:
            # If paused mid-transfer the status was set to "paused" already.
            if item.status == "downloading":
                # Worker returned without complete/error: treat as paused.
                item.status = "paused"
                self.after(0, lambda: self._update_row(item))
            self._active = None
            self.after(0, self._pump)

    def _run_nologin(self, item: QueueItem) -> None:
        dl = DouyinNoLoginDownloader()
        if item.resolved is None:
            self._set_status_async("Launching browser (no login)…", IDLE_COLOR)
            try:
                item.resolved = dl.resolve(item.url)
            except NoLoginError as exc:
                item.status = "error"
                item.message = str(exc)
                self.after(0, lambda: self._update_row(item))
                return
        # Cache title for the row label.
        _cdn, _hdr, _fmts, norm = item.resolved
        item.title = norm.get("title") or item.url
        dl.download_resolved(
            item.resolved, self.download_dir,
            quality=item.quality,
            abort=item.abort,
            on_metadata=lambda m: self._on_metadata(item, m),
            on_progress=lambda d: self._on_progress(item, d),
            on_complete=lambda p: self._on_complete(item, p),
            on_error=lambda m: self._on_error(item, m),
            on_warning=lambda m: self._on_warning(item, m),
        )

    def _run_login(self, item: QueueItem) -> None:
        try:
            meta = self.downloader.fetch_metadata(
                item.url, on_warning=lambda m: self._on_warning(item, m))
        except DownloaderError as exc:
            item.status = "error"
            item.message = str(exc)
            self.after(0, lambda: self._update_row(item))
            return
        self._on_metadata(item, meta)
        self.downloader.download(
            item.url, self.download_dir, info=meta,
            quality=item.quality,
            abort=item.abort,
            on_progress=lambda d: self._on_progress(item, d),
            on_complete=lambda p: self._on_complete(item, p),
            on_error=lambda m: self._on_error(item, m),
            on_warning=lambda m: self._on_warning(item, m),
        )

    # ------------------------------------------------------------------ #
    # Download callbacks (scheduled onto the main thread)
    # ------------------------------------------------------------------ #

    def _on_metadata(self, item: QueueItem, info: dict) -> None:
        item.title = info.get("title") or item.url
        self.after(0, lambda: self._update_row(item))

    def _on_progress(self, item: QueueItem, d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total) if total else 0.0
            item.progress = min(pct, 1.0)
            item.message = (
                f"{pct * 100:.1f}%   •   {self._fmt_speed(d.get('speed'))}   "
                f"•   ETA {self._fmt_eta(d.get('eta'))}"
            )
        elif status == "finished":
            item.progress = 1.0
            item.message = "Merging / finalizing…"
        self.after(0, lambda: self._update_row(item))

    def _on_complete(self, item: QueueItem, final_path: str) -> None:
        item.status = "done"
        item.progress = 1.0
        item.final_path = final_path
        item.message = f"Done: {os.path.basename(final_path)}"
        self.after(0, lambda: self._update_row(item))

    def _on_error(self, item: QueueItem, message: str) -> None:
        item.status = "error"
        item.message = message
        self.after(0, lambda: self._update_row(item))

    def _on_warning(self, item: QueueItem, message: str) -> None:
        self._set_status_async(f"Warning: {message}", WARN_COLOR)

    # ------------------------------------------------------------------ #
    # Row widgets
    # ------------------------------------------------------------------ #

    def _build_item_row(self, item: QueueItem) -> None:
        self.empty_label.grid_remove()
        row = ctk.CTkFrame(self.queue_frame)
        row.grid(sticky="ew", padx=4, pady=4)
        row.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            row, text=self._row_title(item), anchor="w", wraplength=360)
        title.bind("<Button-1>", lambda _e: self._on_title_click(item))
        title.grid(row=0, column=0, padx=10, pady=(8, 2), sticky="ew")

        status = ctk.CTkLabel(row, text="Queued", anchor="w", text_color=IDLE_COLOR)
        status.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")

        progress = ctk.CTkProgressBar(row)
        progress.set(0)
        progress.grid(row=2, column=0, columnspan=4, padx=10, pady=(0, 8), sticky="ew")

        pause_btn = ctk.CTkButton(
            row, text="Pause", width=74, command=lambda: self._toggle_pause(item))
        pause_btn.grid(row=0, column=1, rowspan=2, padx=(4, 4), pady=6)
        remove_btn = ctk.CTkButton(
            row, text="Remove", width=74,
            fg_color=("gray70", "gray30"), hover_color=ERROR_COLOR,
            command=lambda: self.remove_item(item))
        remove_btn.grid(row=0, column=2, rowspan=2, padx=(0, 4), pady=6)
        folder_btn = ctk.CTkButton(
            row, text="Folder", width=74,
            fg_color=("gray70", "gray30"), hover_color=("gray60", "gray40"),
            command=lambda: self._open_folder(item))
        folder_btn.grid(row=0, column=3, rowspan=2, padx=(0, 8), pady=6)

        item.widgets = {
            "row": row, "title": title, "status": status,
            "progress": progress, "pause": pause_btn,
            "remove": remove_btn, "folder": folder_btn,
        }
        self._update_row(item)

    def _toggle_pause(self, item: QueueItem) -> None:
        if item.status in ("downloading", "queued"):
            self.pause_item(item)
        elif item.status == "paused":
            self.resume_item(item)

    def _update_row(self, item: QueueItem) -> None:
        w = item.widgets
        if not w or not w["row"].winfo_exists():
            return
        w["title"].configure(text=self._row_title(item))
        w["progress"].set(item.progress)

        status_text, color = {
            "queued": ("Queued", IDLE_COLOR),
            "downloading": (item.message or "Downloading…", None),
            "paused": ("Paused" + (f" — {item.message}" if item.message else ""), WARN_COLOR),
            "done": (item.message or "Done", DONE_COLOR),
            "error": (f"Error: {item.message}", ERROR_COLOR),
        }.get(item.status, (item.status, IDLE_COLOR))
        w["status"].configure(
            text=status_text, text_color=color if color else IDLE_COLOR)

        # Title is clickable (opens the video) only once the download is done.
        w["title"].configure(cursor="hand2" if item.status == "done" else "arrow")

        # Pause button label / availability.
        if item.status in ("downloading", "queued"):
            w["pause"].configure(text="Pause", state="normal")
        elif item.status == "paused":
            w["pause"].configure(text="Resume", state="normal")
        else:  # done / error
            w["pause"].configure(text="Pause", state="disabled")

        # Folder button opens the containing folder once finished.
        w["folder"].configure(
            state="normal" if item.status == "done" else "disabled")

    def _row_title(self, item: QueueItem) -> str:
        tag = "🌐 " if item.use_nologin else ""
        return f"{tag}{item.title}   [{item.quality}]"

    # -- click-to-open ------------------------------------------------------- #

    def _on_title_click(self, item: QueueItem) -> None:
        """Open the finished video in its default player when the title is clicked."""
        try:
            if item.status == "done" and item.final_path and os.path.isfile(item.final_path):
                self._open_file(item.final_path)
            elif item.status == "done" and item.final_path:
                # File moved / renamed elsewhere — at least reveal the folder.
                self._open_folder(item)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the UI
            self._set_status(f"Could not open file: {exc}", ERROR_COLOR)

    def _open_folder(self, item: QueueItem) -> None:
        """Open the folder containing the finished video in the file manager."""
        if not item.final_path:
            return
        try:
            self._open_file(os.path.dirname(item.final_path))
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the UI
            self._set_status(f"Could not open folder: {exc}", ERROR_COLOR)

    @staticmethod
    def _open_file(path: str) -> None:
        """Open *path* with the OS default handler (file → player, dir → explorer)."""
        os.startfile(os.path.normpath(path))  # type: ignore[attr-defined]

    def _destroy_item(self, item: QueueItem) -> None:
        if item in self.queue:
            self.queue.remove(item)
        w = item.widgets
        if w and w.get("row") and w["row"].winfo_exists():
            w["row"].destroy()
        self._refresh_empty_label()

    def _refresh_empty_label(self) -> None:
        if not self.queue:
            self.empty_label.grid()

    # ------------------------------------------------------------------ #
    # Status + formatting helpers
    # ------------------------------------------------------------------ #

    def _set_status(self, text: str, color) -> None:
        self.status_line.configure(text=text, text_color=color)

    def _set_status_async(self, text: str, color) -> None:
        self.after(0, lambda: self._set_status(text, color))

    @staticmethod
    def _fmt_speed(bps: float | None) -> str:
        if not bps:
            return "—"
        if bps >= 1 << 20:
            return f"{bps / (1 << 20):.2f} MB/s"
        return f"{bps / 1024:.0f} KB/s"

    @staticmethod
    def _fmt_eta(eta: float | None) -> str:
        if eta is None:
            return "—"
        eta = int(eta)
        minutes, seconds = divmod(eta, 60)
        return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    # ------------------------------------------------------------------ #
    # ffmpeg status
    # ------------------------------------------------------------------ #

    def _refresh_ffmpeg_status(self) -> None:
        path = detect_ffmpeg()
        self.downloader.ffmpeg_path = path  # keep downloader in sync
        if path:
            self.ffmpeg_label.configure(text=f"ffmpeg: OK ({path})", text_color=DONE_COLOR)
        else:
            self.ffmpeg_label.configure(
                text="ffmpeg: NOT FOUND — merging requires ffmpeg (see README).",
                text_color=ERROR_COLOR,
            )
