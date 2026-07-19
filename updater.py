"""updater.py - Self-update & self-repair engine for Douyin HD Downloader.

Stdlib-only (urllib / json / zipfile / shutil / logging) so it runs unchanged
inside a frozen PyInstaller exe with no pip and no third-party deps.

Responsibilities:
  * yt-dlp engine: check PyPI for a newer version, download the wheel, extract
    it into a managed per-user dir, and activate it via sys.path so the app can
    be refreshed without a rebuild.
  * Self-repair: if the managed yt-dlp is missing/corrupt, re-provision it (or
    fall back to the bundled copy) so the app never ends up unable to import
    its engine.
  * App self-update: optionally check a GitHub releases repo for a newer
    DouyinHD.exe and stage an in-place replacement. Dormant unless a repo is
    configured.
  * Environment: create the per-user data dirs and configure rotating logs.

Design rule: **nothing here may crash or block the app**. Every network / IO
path is wrapped and degrades to a logged warning. The GUI calls these on a
background thread and shows status via callbacks.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional, Tuple
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------- #
# Version / constants
# --------------------------------------------------------------------------- #

APP_VERSION = "1.0.0"
APP_NAME = "DouyinHD"

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
GITHUB_LATEST_RELEASE = "https://api.github.com/repos/{repo}/releases/latest"

NET_TIMEOUT = 15  # seconds — short so startup never hangs
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

logger = logging.getLogger(APP_NAME)

StatusCb = Optional[Callable[[str], None]]


def _report(cb: StatusCb, message: str) -> None:
    logger.info(message)
    if cb:
        try:
            cb(message)
        except Exception:  # noqa: BLE001 - a bad callback must not break updates
            pass


# --------------------------------------------------------------------------- #
# Paths / environment
# --------------------------------------------------------------------------- #

def user_data_dir() -> str:
    """Per-user writable dir for settings, managed pkgs, logs, downloads."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def pkgs_dir() -> str:
    return os.path.join(user_data_dir(), "pkgs")


def logs_dir() -> str:
    return os.path.join(user_data_dir(), "logs")


def default_downloads_dir() -> str:
    return os.path.join(user_data_dir(), "downloads")


def app_install_dir() -> str:
    """Directory containing the running exe (frozen) or this source (dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def ensure_app_dirs() -> None:
    """Create all per-user dirs the app relies on (idempotent)."""
    for path in (
        user_data_dir(), pkgs_dir(), logs_dir(),
        default_downloads_dir(), os.path.join(user_data_dir(), "bin"),
    ):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create %s: %s", path, exc)


def setup_logging() -> None:
    """Configure a rotating log at ``logs/download.log`` (idempotent)."""
    try:
        os.makedirs(logs_dir(), exist_ok=True)
        log_path = os.path.join(logs_dir(), "download.log")
        # Avoid duplicate handlers if called twice.
        if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
            return
        handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.info("=== %s %s starting ===", APP_NAME, APP_VERSION)
    except OSError:
        # Logging is best-effort; never block startup on it.
        pass


# --------------------------------------------------------------------------- #
# Version helpers
# --------------------------------------------------------------------------- #

def parse_version(v: str) -> Tuple[int, ...]:
    """Parse a dotted version string into a comparable int tuple.

    Handles yt-dlp's date-style versions (``2026.07.04``) and app-style
    (``1.0.0``). Non-numeric parts are ignored. A leading ``v`` is stripped.
    """
    if not v:
        return (0,)
    v = v.strip().lstrip("vV")
    parts = []
    for chunk in v.replace("-", ".").split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        if num:
            parts.append(int(num))
    return tuple(parts) or (0,)


def needs_update(installed: str, latest: str) -> bool:
    """True if *latest* is strictly newer than *installed*."""
    if not latest:
        return False
    if not installed:
        return True
    return parse_version(latest) > parse_version(installed)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _http_json(url: str) -> Optional[dict]:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/json"})
        with urlopen(req, timeout=NET_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - offline / rate-limited / etc.
        logger.warning("HTTP JSON fetch failed for %s: %s", url, exc)
        return None


def _http_download(url: str, dest: str) -> bool:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=NET_TIMEOUT) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Download failed for %s: %s", url, exc)
        return False


# --------------------------------------------------------------------------- #
# yt-dlp managed override
# --------------------------------------------------------------------------- #

def _active_pointer() -> str:
    return os.path.join(pkgs_dir(), "active.txt")


def active_ytdlp_dir() -> Optional[str]:
    """Return the currently-activated managed yt-dlp dir, if valid."""
    try:
        pointer = _active_pointer()
        if not os.path.isfile(pointer):
            return None
        with open(pointer, "r", encoding="utf-8") as fh:
            path = fh.read().strip()
        if path and os.path.isdir(os.path.join(path, "yt_dlp")):
            return path
    except OSError:
        pass
    return None


def ensure_ytdlp_override() -> Optional[str]:
    """Prepend the managed yt-dlp dir to sys.path BEFORE ``import yt_dlp``.

    Call this in ``main.py`` before importing anything that imports yt_dlp.
    Returns the activated dir, or None (bundled yt-dlp will be used).
    """
    path = active_ytdlp_dir()
    if path and path not in sys.path:
        sys.path.insert(0, path)
        logger.info("Activated managed yt-dlp at %s", path)
    return path


def installed_ytdlp_version() -> Optional[str]:
    """Version of whatever yt_dlp is currently importable."""
    try:
        import yt_dlp.version as v  # type: ignore
        return v.__version__
    except Exception:  # noqa: BLE001
        return None


def get_latest_pypi_version(package: str = "yt-dlp") -> Optional[str]:
    data = _http_json(PYPI_JSON_URL.format(package=package))
    if not data:
        return None
    try:
        return data["info"]["version"]
    except (KeyError, TypeError):
        return None


def _pypi_wheel_url(package: str, version: str) -> Optional[str]:
    data = _http_json(PYPI_JSON_URL.format(package=package))
    if not data:
        return None
    try:
        releases = data["releases"].get(version, [])
    except (KeyError, TypeError):
        return None
    # Prefer a pure-python wheel; yt-dlp ships a py3-none-any wheel.
    for f in releases:
        if f.get("packagetype") == "bdist_wheel" and f.get("url", "").endswith(
            "py3-none-any.whl"
        ):
            return f["url"]
    for f in releases:
        if f.get("url", "").endswith(".whl"):
            return f["url"]
    return None


def download_ytdlp_to_pkgs(
    version: Optional[str] = None, on_status: StatusCb = None
) -> Optional[str]:
    """Download + extract a yt-dlp wheel into a versioned managed dir.

    Returns the new active dir on success, else None. Activates it by writing
    ``pkgs/active.txt``.
    """
    version = version or get_latest_pypi_version("yt-dlp")
    if not version:
        _report(on_status, "Could not reach PyPI for yt-dlp version info.")
        return None

    url = _pypi_wheel_url("yt-dlp", version)
    if not url:
        _report(on_status, f"No yt-dlp wheel found for {version}.")
        return None

    ensure_app_dirs()
    target_dir = os.path.join(pkgs_dir(), f"yt_dlp-{version}")
    if os.path.isdir(os.path.join(target_dir, "yt_dlp")):
        # Already downloaded; just activate.
        _write_active(target_dir)
        _report(on_status, f"yt-dlp {version} already present; activated.")
        return target_dir

    _report(on_status, f"Downloading yt-dlp {version}…")
    with tempfile.TemporaryDirectory() as tmp:
        wheel_path = os.path.join(tmp, "yt_dlp.whl")
        if not _http_download(url, wheel_path):
            _report(on_status, "yt-dlp download failed (offline?).")
            return None
        staging = target_dir + ".partial"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            os.makedirs(staging, exist_ok=True)
            with zipfile.ZipFile(wheel_path) as zf:
                zf.extractall(staging)
        except (OSError, zipfile.BadZipFile) as exc:
            _report(on_status, f"yt-dlp extract failed: {exc}")
            shutil.rmtree(staging, ignore_errors=True)
            return None
        # Promote staging -> target_dir atomically-ish.
        shutil.rmtree(target_dir, ignore_errors=True)
        os.replace(staging, target_dir)

    if not os.path.isdir(os.path.join(target_dir, "yt_dlp")):
        _report(on_status, "yt-dlp wheel missing package dir after extract.")
        shutil.rmtree(target_dir, ignore_errors=True)
        return None

    _write_active(target_dir)
    _prune_old_pkgs(keep=target_dir)
    _report(on_status, f"yt-dlp {version} installed (active on next launch).")
    return target_dir


def _write_active(path: str) -> None:
    try:
        os.makedirs(pkgs_dir(), exist_ok=True)
        with open(_active_pointer(), "w", encoding="utf-8") as fh:
            fh.write(path)
    except OSError as exc:
        logger.warning("Could not write active pointer: %s", exc)


def _prune_old_pkgs(keep: str) -> None:
    """Delete stale managed yt-dlp dirs, keeping the active one."""
    try:
        for name in os.listdir(pkgs_dir()):
            full = os.path.join(pkgs_dir(), name)
            if os.path.isdir(full) and name.startswith("yt_dlp-") and full != keep:
                shutil.rmtree(full, ignore_errors=True)
    except OSError:
        pass


def self_repair_ytdlp(on_status: StatusCb = None) -> bool:
    """Ensure yt_dlp is importable; re-provision if the managed copy is broken.

    Returns True if yt_dlp is importable afterwards (managed or bundled).
    """
    path = active_ytdlp_dir()
    if path:
        # Verify the managed copy actually imports.
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import importlib
            import yt_dlp  # type: ignore # noqa: F401
            importlib.import_module("yt_dlp.version")
            return True
        except Exception as exc:  # noqa: BLE001 - corrupt managed copy
            _report(on_status, f"Managed yt-dlp is corrupt ({exc}); repairing…")
            try:
                shutil.rmtree(path, ignore_errors=True)
                if os.path.isfile(_active_pointer()):
                    os.remove(_active_pointer())
            except OSError:
                pass
            new_dir = download_ytdlp_to_pkgs(on_status=on_status)
            if new_dir and new_dir not in sys.path:
                sys.path.insert(0, new_dir)
            return installed_ytdlp_version() is not None

    # No managed copy — rely on the bundled yt-dlp.
    if installed_ytdlp_version() is not None:
        return True
    # Nothing importable at all — try to fetch one.
    _report(on_status, "No yt-dlp found; downloading…")
    new_dir = download_ytdlp_to_pkgs(on_status=on_status)
    if new_dir and new_dir not in sys.path:
        sys.path.insert(0, new_dir)
    return installed_ytdlp_version() is not None


def check_ytdlp(auto_install: bool = True, on_status: StatusCb = None) -> dict:
    """Check for a newer yt-dlp and optionally install it.

    Returns a dict: ``{installed, latest, updated, error}``.
    """
    result = {"installed": installed_ytdlp_version(), "latest": None,
              "updated": False, "error": None}
    latest = get_latest_pypi_version("yt-dlp")
    result["latest"] = latest
    if not latest:
        result["error"] = "offline"
        _report(on_status, "Update check skipped (offline).")
        return result

    if needs_update(result["installed"] or "", latest):
        _report(on_status, f"yt-dlp update available: {result['installed']} → {latest}")
        if auto_install:
            new_dir = download_ytdlp_to_pkgs(latest, on_status=on_status)
            result["updated"] = bool(new_dir)
    else:
        _report(on_status, f"yt-dlp is up to date ({result['installed']}).")
    return result


# --------------------------------------------------------------------------- #
# App self-update (GitHub releases) — dormant unless a repo is configured
# --------------------------------------------------------------------------- #

def get_latest_app_release(repo: str) -> Optional[dict]:
    """Return ``{tag, exe_url, zip_url}`` for the repo's latest release, or None.

    ``exe_url`` is a bare ``.exe`` (single-file replace); ``zip_url`` is a folder
    bundle (our distribution). Either can drive an in-place update.
    """
    if not repo or "/" not in repo:
        return None
    data = _http_json(GITHUB_LATEST_RELEASE.format(repo=repo))
    if not data:
        return None
    tag = data.get("tag_name")
    if not tag:
        return None
    exe_url = None
    zip_url = None
    for asset in data.get("assets", []):
        name = (asset.get("name") or "").lower()
        url = asset.get("browser_download_url")
        if name.endswith(".exe") and exe_url is None:
            exe_url = url
        elif name.endswith(".zip") and zip_url is None:
            zip_url = url
    return {"tag": tag, "exe_url": exe_url, "zip_url": zip_url}


def check_app_update(repo: str, current: str = APP_VERSION,
                     on_status: StatusCb = None) -> Optional[dict]:
    """Return release info if a newer app version exists, else None."""
    if not repo:
        return None
    release = get_latest_app_release(repo)
    if not release:
        return None
    if needs_update(current, release["tag"]):
        _report(on_status, f"App update available: {current} → {release['tag']}")
        return release
    _report(on_status, f"App is up to date ({current}).")
    return None


def apply_app_update(exe_url: str = None, zip_url: str = None,
                     on_status: StatusCb = None) -> Optional[str]:
    """Download a newer app package and stage an in-place replace + relaunch.

    Handles two package shapes from the GitHub release:
      * a bare ``.exe`` — single-file replace of the running exe; or
      * a ``.zip`` folder bundle (our distribution) — extracted over the install
        dir after the app exits.

    Returns the path of the helper script the caller should run on exit, or
    None on failure / unsupported platform.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        _report(on_status, "Auto app-update only works for the built Windows exe.")
        return None
    if exe_url:
        return _stage_exe_update(exe_url, on_status)
    if zip_url:
        return _stage_zip_update(zip_url, on_status)
    _report(on_status, "No app-update package found in the release.")
    return None


def _stage_exe_update(exe_url: str, on_status: StatusCb) -> Optional[str]:
    """Stage a single-file exe replace (legacy / external builds)."""
    current_exe = sys.executable
    staging = os.path.join(user_data_dir(), "update_staging.exe")
    _report(on_status, "Downloading app update…")
    if not _http_download(exe_url, staging):
        _report(on_status, "App update download failed.")
        return None
    bat_path = os.path.join(user_data_dir(), "apply_update.bat")
    try:
        with open(bat_path, "w", encoding="utf-8") as fh:
            fh.write(
                "@echo off\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                f'move /y "{staging}" "{current_exe}" >nul\r\n'
                f'start "" "{current_exe}"\r\n'
                'del "%~f0"\r\n'
            )
    except OSError as exc:
        _report(on_status, f"Could not stage update: {exc}")
        return None
    _report(on_status, "App update ready — restart to apply.")
    return bat_path


def _stage_zip_update(zip_url: str, on_status: StatusCb) -> Optional[str]:
    """Stage a folder-bundle update.

    Downloads the zip, extracts it, and writes a bat that copies the extracted
    app over the install dir *after the app exits* (avoids file locks on the
    running exe / ``_internal``). Chromium (``ms-playwright``) is excluded from
    the copy — it is large and rarely changes between app releases, so updates
    stay small and never touch locked browser files.
    """
    install_dir = app_install_dir()
    staging = os.path.join(user_data_dir(), "update_staging.zip")
    _report(on_status, "Downloading app update…")
    if not _http_download(zip_url, staging):
        _report(on_status, "App update download failed.")
        return None

    extract_root = tempfile.mkdtemp(prefix="douyinhd_update_")
    try:
        with zipfile.ZipFile(staging, "r") as zf:
            zf.extractall(extract_root)
    except Exception as exc:  # noqa: BLE001
        _report(on_status, f"Could not extract update: {exc}")
        return None

    source_dir = _resolve_update_source(extract_root, os.path.basename(install_dir))
    if not source_dir or not os.path.isdir(source_dir):
        _report(on_status, "Update package layout unrecognized.")
        return None

    shutil.rmtree(os.path.join(source_dir, "ms-playwright"), ignore_errors=True)

    bat_path = os.path.join(user_data_dir(), "apply_update.bat")
    try:
        with open(bat_path, "w", encoding="utf-8") as fh:
            fh.write(
                "@echo off\r\n"
                f'del /q "{staging}" >nul 2>&1\r\n'
                "timeout /t 2 /nobreak >nul\r\n"
                f'xcopy /E /I /Y /Q "{source_dir}\\*" "{install_dir}\\" >nul\r\n'
                f'rmdir /s /q "{extract_root}" >nul 2>&1\r\n'
                f'start "" "{os.path.join(install_dir, "DouyinHD.exe")}"\r\n'
                'del "%~f0"\r\n'
            )
    except OSError as exc:
        _report(on_status, f"Could not stage update: {exc}")
        return None
    _report(on_status, "App update ready — restart to apply.")
    return bat_path


def _resolve_update_source(extract_root: str, install_name: str) -> Optional[str]:
    """Locate the extracted app folder to copy over the install dir.

    Prefers a top-level folder matching the install dir name (e.g. ``DouyinHD``);
    falls back to the zip's sole top-level directory, then to the root itself.
    """
    try:
        entries = os.listdir(extract_root)
    except OSError:
        return None
    if install_name in entries:
        return os.path.join(extract_root, install_name)
    dirs = [e for e in entries
            if os.path.isdir(os.path.join(extract_root, e))]
    if len(dirs) == 1:
        return os.path.join(extract_root, dirs[0])
    return extract_root


# --------------------------------------------------------------------------- #
# Dependency checks (ffmpeg / playwright)
# --------------------------------------------------------------------------- #

def check_playwright(on_status: StatusCb = None) -> bool:
    """Verify a Playwright browser is available for no-login capture."""
    # Bundled browsers dir next to the exe (set by main._configure_browser_path)
    bundled = os.path.join(app_install_dir(), "ms-playwright")
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    for candidate in (env_path, bundled):
        if candidate and os.path.isdir(candidate) and os.listdir(candidate):
            return True
    try:
        import playwright  # type: ignore # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        _report(on_status,
                "No-login browser missing. Run: playwright install chromium")
        return False


def check_ffmpeg(on_status: StatusCb = None) -> Optional[str]:
    """Return an ffmpeg path (env / bundled / PATH) or None with guidance."""
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    bundled = os.path.join(app_install_dir(), "ffmpeg.exe")
    if os.path.isfile(bundled):
        os.environ["FFMPEG_PATH"] = bundled
        return bundled
    found = shutil.which("ffmpeg")
    if found:
        return found
    _report(on_status,
            "ffmpeg not found — merging needs it (see README to install).")
    return None
