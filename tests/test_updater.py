"""Tests for updater.py (self-update / self-repair engine).

Run with:  python -m unittest tests.test_updater -v

Network functions are monkeypatched so no live HTTP happens.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

import updater


class TestVersionHelpers(unittest.TestCase):
    def test_parse_version_date_style(self):
        self.assertEqual(updater.parse_version("2026.07.04"), (2026, 7, 4))

    def test_parse_version_app_style(self):
        self.assertEqual(updater.parse_version("1.0.0"), (1, 0, 0))

    def test_parse_version_strips_v(self):
        self.assertEqual(updater.parse_version("v1.2"), (1, 2))

    def test_needs_update(self):
        self.assertTrue(updater.needs_update("2026.06.01", "2026.07.04"))
        self.assertFalse(updater.needs_update("2026.07.04", "2026.07.04"))
        self.assertFalse(updater.needs_update("2026.07.04", ""))
        self.assertTrue(updater.needs_update("", "2026.07.04"))


class TestEnsureYtdlpOverride(unittest.TestCase):
    def test_no_active_dir_is_noop(self):
        # Point pkgs dir at an empty temp dir so active.txt is absent.
        with tempfile.TemporaryDirectory() as d:
            orig = updater.pkgs_dir
            updater.pkgs_dir = lambda: d  # type: ignore[assignment]
            try:
                before = list(sys.path)
                result = updater.ensure_ytdlp_override()
                self.assertIsNone(result)
                self.assertEqual(list(sys.path), before)
            finally:
                updater.pkgs_dir = orig  # type: ignore[assignment]

    def test_active_dir_inserted_into_sys_path(self):
        with tempfile.TemporaryDirectory() as d:
            # Build a fake managed yt-dlp dir with a package marker.
            pkg = os.path.join(d, "yt_dlp-9.9.9")
            os.makedirs(os.path.join(pkg, "yt_dlp"))
            pointer = os.path.join(d, "active.txt")
            with open(pointer, "w", encoding="utf-8") as fh:
                fh.write(pkg)

            orig = updater.pkgs_dir
            updater.pkgs_dir = lambda: d  # type: ignore[assignment]
            try:
                result = updater.ensure_ytdlp_override()
                self.assertEqual(result, pkg)
                self.assertIn(pkg, sys.path)
                self.assertEqual(sys.path[0], pkg)
            finally:
                updater.pkgs_dir = orig  # type: ignore[assignment]
                if pkg in sys.path:
                    sys.path.remove(pkg)


class TestDownloadYtdlpToPkgs(unittest.TestCase):
    def _fake_wheel(self, version: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("yt_dlp/__init__.py", "")
            zf.writestr("yt_dlp/version.py", f'__version__ = "{version}"\n')
            zf.writestr(
                f"yt_dlp-{version}.dist-info/METADATA", "Name: yt-dlp\n")
        return buf.getvalue()

    def test_download_extracts_and_activates(self):
        with tempfile.TemporaryDirectory() as d:
            orig_pkgs = updater.pkgs_dir

            class _Resp:
                def __init__(self, data):
                    self._data = data

                def read(self):
                    return self._data

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            captured = {}

            def fake_http_download(url, dest):
                captured["url"] = url
                with open(dest, "wb") as fh:
                    fh.write(self._fake_wheel("9.9.9"))
                return True

            updater.pkgs_dir = lambda: d  # type: ignore[assignment]
            updater._http_download = fake_http_download  # type: ignore[assignment]
            updater._pypi_wheel_url = (  # type: ignore[assignment]
                lambda pkg, ver: "http://fake/wheel.whl")
            try:
                target = updater.download_ytdlp_to_pkgs("9.9.9")
                self.assertIsNotNone(target)
                self.assertTrue(os.path.isdir(os.path.join(target, "yt_dlp")))
                pointer = os.path.join(d, "active.txt")
                self.assertTrue(os.path.isfile(pointer))
                with open(pointer, "r", encoding="utf-8") as fh:
                    self.assertEqual(fh.read().strip(), target)
            finally:
                updater.pkgs_dir = orig_pkgs  # type: ignore[assignment]


class TestUserDirs(unittest.TestCase):
    def test_user_data_dir_created(self):
        # ensure_app_dirs should not raise even if dirs exist.
        updater.ensure_app_dirs()
        self.assertTrue(os.path.isdir(updater.user_data_dir()))


class TestAppUpdateCheck(unittest.TestCase):
    def test_check_app_update_dormant_without_repo(self):
        self.assertIsNone(updater.check_app_update("", "1.0.0"))

    def test_check_app_update_detects_newer_release(self):
        release = {"tag": "2.0.0", "exe_url": "http://x/app.exe"}

        def fake_get_latest(repo):
            return release

        orig = updater.get_latest_app_release
        updater.get_latest_app_release = fake_get_latest  # type: ignore[assignment]
        try:
            result = updater.check_app_update("owner/repo", "1.0.0")
            self.assertEqual(result, release)
        finally:
            updater.get_latest_app_release = orig  # type: ignore[assignment]

    def test_check_app_update_none_when_current(self):
        release = {"tag": "1.0.0", "exe_url": "http://x/app.exe"}

        def fake_get_latest(repo):
            return release

        orig = updater.get_latest_app_release
        updater.get_latest_app_release = fake_get_latest  # type: ignore[assignment]
        try:
            self.assertIsNone(updater.check_app_update("owner/repo", "1.0.0"))
        finally:
            updater.get_latest_app_release = orig  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main(verbosity=2)
