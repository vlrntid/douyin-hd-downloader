# Douyin HD Downloader

A small desktop app for grabbing Douyin **and YouTube** videos at the highest
available quality and saving them as merged MP4 files — built for a YouTube
Shorts editing workflow.

- **Backend:** [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- **Muxing:** ffmpeg (best video + best audio → single MP4)
- **UI:** [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)
- **Sites:** Douyin (`douyin.com`, `iesdouyin.com`) and YouTube
  (`youtube.com`, `youtu.be`, shorts, music)

## Build it yourself (the `.exe`)

The app is distributed as a self-contained `dist/DouyinHD/DouyinHD.exe`
(Windows). It bundles ffmpeg and a Playwright Chromium browser alongside the
exe, so it needs no installation.

To rebuild from source (requires a venv — `py -3.12 -m venv venv` first):

```bash
"venv\Scripts\python.exe" -m pip install -r requirements.txt
"venv\Scripts\python.exe" -m pip install pyinstaller
"venv\Scripts\python.exe" -m PyInstaller DouyinHD.spec --noconfirm
# Bundle the runtime deps next to the exe:
copy ffmpeg.exe dist\DouyinHD\ffmpeg.exe
xcopy /E /I %LOCALAPPDATA%\ms-playwright dist\DouyinHD\ms-playwright
```

> After `PyInstaller` runs, `dist/DouyinHD` is recreated empty, so the
> `ffmpeg.exe` and `ms-playwright` folders must be copied in again each build.

## Automatic updates & self-repair

You should **never have to come back to fix things manually**:

- **yt-dlp auto-update** — on every launch the app checks PyPI for a newer
  yt-dlp and downloads it into your per-user data dir
  (`%LOCALAPPDATA%/DouyinHD/pkgs/`). The new version is used on the **next
  launch** (the status bar tells you to restart). This is what keeps downloads
  working when Douyin/YouTube change.
- **Self-repair** — if the managed yt-dlp copy is missing or corrupt, the app
  re-downloads it (or falls back to the bundled copy). ffmpeg and the
  Playwright browser are detected from the bundled folders or PATH.
- **Auto-repair toggle** — *Settings → Updates & Repair → "Auto-update yt-dlp
  on startup"* (on by default).
- **App self-update (optional)** — set *Settings → App update source* to a
  GitHub `owner/repo`. When a newer release is published, the app shows an
  **Install App Update** button. Leave it blank to disable app-update checks.
- **Logs** — everything (update checks, repairs, downloads) is written to
  `%LOCALAPPDATA%/DouyinHD/logs/download.log` (auto-rotated).

## No-login by default

The app defaults to **No-login (watermarked)** mode for Douyin, which captures
videos through a real browser session — no account, no cookies, and **no
Windows DPAPI cookie decryption** (the old failure). The cookie/login path and
**Import Cookies** button remain available as a fallback for login-gated
videos; toggle *No login (watermarked)* off to use them.

## Features

- Paste a Douyin **or YouTube** URL, click **Download**.
- Validates the URL, fetches the highest quality, and merges best video +
  best audio into one MP4.
- Choose where files are saved (defaults to `./downloads`).
- Live status: **title**, **creator**, **progress %**, **speed**, **ETA**.
- Auto-renames the result to `creator_title_date.mp4`.
- Detects ffmpeg at startup and warns if it is missing.

## Requirements

- Python 3.12 (create the environment with `py -3.12 -m venv venv` — see Install)
- [ffmpeg](https://ffmpeg.org/download.html) on your `PATH` (required to
  merge video + audio for the **cookie/login** path; optional for the
  **no-login** path, which downloads already-muxed MP4s).
- *(No-login mode only)* [Playwright](https://playwright.dev/) Chromium —
  see install note below.

## Install

```bash
# 1) Create a virtual environment with Python 3.12
py -3.12 -m venv venv
# 2) Activate it
venv\Scripts\activate
# 3) Install dependencies
pip install -r requirements.txt
# 4) No-login mode needs a one-time browser download:
playwright install chromium
```

### Install ffmpeg

- **Windows (winget):** `winget install ffmpeg`
- **Windows (choco):** `choco install ffmpeg`
- **macOS (brew):** `brew install ffmpeg`
- **Debian/Ubuntu:** `sudo apt install ffmpeg`

> If ffmpeg is installed somewhere unusual, point the app at it with the
> `FFMPEG_PATH` environment variable, e.g.:
> `set FFMPEG_PATH=C:\tools\ffmpeg\bin\ffmpeg.exe` (Windows) or
> `export FFMPEG_PATH=/opt/ffmpeg/bin/ffmpeg` (Linux/macOS).

## Run

```bash
venv\Scripts\python.exe main.py
```

## Usage

1. Paste a Douyin link, e.g. `https://v.douyin.com/5EN2Wx7HDT8/`.
2. (Optional) Click **Browse** to change the save folder.
3. Click **Download**.
4. When finished, the file appears in the chosen folder as
   `creator_title_date.mp4`.

## No-login mode (Douyin, resolution picker)

If you don't have (or don't want to use) a Douyin account, flip the
**"No login (watermarked)"** switch in the Cookies section. For Douyin links
this switches to a headless-Chromium engine (`douyin_nologin.py`) instead of
yt-dlp + browser cookies:

1. It launches Chromium and loads the share page. Douyin's *anonymous* session
   cookies (`ttwid`, `odin_tt`, …) are set automatically — no account needed.
2. It intercepts the video's real CDN URL(s) from the page's network traffic,
   including **every resolution the API exposes**.
3. It downloads the chosen URL (already a merged MP4, so **ffmpeg is optional**
   here).

**Choosing a resolution:** after the first resolve, the **Quality** dropdown
(enabled in no-login mode) lists the available renditions — e.g. `1080p`,
`720p`, `540p`, `360p` — highest first, defaulting to the best. Pick one and
click **Download** again to fetch that specific quality. Each resolution is
saved as `creator_title_date_<quality>.mp4` so they don't clash.

**Notes / limitations (by design, not bugs):**
- Output is typically the **clean, no-watermark** `play_addr` rendition that
  Douyin's API exposes — a bonus of the login-free path.
- Requires the one-time `playwright install chromium` step above.
- A few videos sit fully behind a login wall / captcha and will fail with a
  clear "needs an account" error. For those, use the cookie mode instead.
- Slower per download than yt-dlp (a browser has to launch).

> Why not just parse the page HTML? A probe showed Douyin's static HTML is a
> near-empty SPA shell with the video URL only available from a *signed* API
> the page's own JS calls — so a real browser is required to obtain it.

YouTube links always use the yt-dlp path, regardless of this switch.

## Project structure

```
douyin_downloader/
│
├── main.py            # App entry point
├── downloader.py      # yt-dlp wrapper: validation, fetch, download, merge, rename
├── douyin_nologin.py  # Headless-Chromium, login-free Douyin downloader
├── gui.py             # CustomTkinter UI
├── requirements.txt   # Dependencies
├── downloads/         # Default output folder
└── README.md
```

## Extending: AI video-editing pipeline

`downloader.py` is deliberately UI-free so it can be reused headlessly.
The `DouyinDownloader` class is the integration point:

```python
from downloader import DouyinDownloader

# Defaults: auto-detects ffmpeg and pulls Douyin session cookies from Brave.
dl = DouyinDownloader()
meta = dl.fetch_metadata(url)        # {"title", "creator", ...}
path = dl.download(url, "./downloads")  # -> "creator_title_date.mp4" or None

# Hand `path` + `meta` to your editing pipeline.
```

The browser used for cookies is configurable:

```python
DouyinDownloader(cookiesfrombrowser=("chrome",))  # or None to disable
```

## Cookies & the "could not copy cookies from database" error

Douyin serves almost all content behind a login, so the app needs session
cookies. By default it tries to pull them from your browser, but on modern
Windows this **frequently fails** with two distinct yt-dlp errors:

```
Could not copy Chrome cookie database.        # yt-dlp #7271  (browser has the DB locked)
Failed to decrypt with DPAPI.                  # yt-dlp #10927 (Chrome/Edge 127+ App-Bound Encryption)
```

Both come from the same root cause: yt-dlp reads the browser's `Cookies`
SQLite DB directly and decrypts it with Windows DPAPI. That breaks when the
**browser is open** (file lock) and on **Chrome/Edge 127+ App-Bound
Encryption**, where cookie values are no longer user-DPAPI decryptable.

> **Browser auto-extraction is unreliable on Windows — `cookies.txt` is the fix.**

### Recommended fix: export a `cookies.txt` (one time)

A browser extension reads cookies through the browser's *own* API, which
returns them already decrypted, so yt-dlp uses plaintext values with **no
DB copy, no lock, and no DPAPI**. This is the only method that reliably works.

1. Install a cookie exporter in your browser — e.g. **"Get cookies.txt
   LOCALLY"** (use the *LOCALLY* edition; avoid look-alike extensions that
   upload your cookies) or **"Cookie-Editor"**.
2. Open **douyin.com** and **log in**.
3. Click the extension → export → save as **Netscape `cookies.txt`**.
4. In the app, click **Cookies file: Browse** and select that file. (Or just
   drop `cookies.txt` into this project folder — it is auto-detected.)
5. Download. The browser can stay open.

```python
DouyinDownloader(cookies="cookies.txt")  # Netscape file takes precedence
```

A `cookies.txt` placed in the app folder / current directory is
auto-detected, which is ideal for the headless AI pipeline.

### What the app does about it

- **Graceful fallback** — if cookie loading fails, the download is retried
  *without* cookies and a warning is shown. If the video still needs login,
  you get the real error plus guidance instead of a misleading cookie message.
- **`cookies.txt` support** — the **Cookies file: Browse** button (or an
  auto-detected `cookies.txt` in the folder) bypasses the browser entirely.
- **"Import Cookies" button (recommended fix)** — on the Download tab, click
  **Import Cookies**. This launches your selected browser via Playwright,
  lets you log in to Douyin in the **opened window**, then saves a portable
  `cookies.txt` next to the app. It reads cookies from the live browser
  context — **never** the on-disk cookie DB — so it is immune to DPAPI
  failures (yt-dlp #10927) and works even while the browser is open. After
  import, the app automatically uses the saved file for all downloads.
- **"Refresh Cookies"** — on the Settings tab, re-runs the import flow to
  refresh an expired session.
- **Browser picker** — the **Browser** dropdown selects Brave / Chrome /
  Edge / Firefox / None. It is remembered for the next import/refresh (it no
  longer passes cookies directly to yt-dlp, which is what triggered DPAPI).

> **Why not `--cookies-from-browser`?** yt-dlp reads the browser's `Cookies`
> SQLite DB and decrypts it with Windows DPAPI. We removed that path from the
> default download flow; the **Import Cookies** button achieves the same
> result (a usable `cookies.txt`) through a DPAPI-free API instead.

Callbacks (`on_metadata`, `on_progress`, `on_complete`, `on_error`,
`on_warning`) let you
stream progress into any consumer (queue, websocket, logger, etc.).
