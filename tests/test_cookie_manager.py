"""Tests for the DPAPI-free CookieManager workflow.

Run with:  python -m unittest tests.test_cookie_manager -v
"""

import os
import tempfile
import unittest

from cookie_manager import CookieManager, CookieError

SAMPLE_COOKIES = [
    {
        "name": "ttwid",
        "value": "abc",
        "domain": ".douyin.com",
        "path": "/",
        "secure": True,
        "expires": 9999999999,
    },
    {
        "name": "odin_tt",
        "value": "xyz",
        "domain": "douyin.com",
        "path": "/",
        "secure": False,
        "expires": 0,
    },
    {
        "name": "passport_csrf_token",
        "value": "tok",
        "domain": ".douyin.com",
        "path": "/",
        "secure": True,
        "expires": 1234567890,
    },
]


def _write(tmp_dir: str, cookies) -> str:
    out = os.path.join(tmp_dir, "cookies.txt")
    CookieManager()._write_netscape(cookies, out)
    return out


class TestCookieManager(unittest.TestCase):
    def test_netscape_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            out = _write(d, SAMPLE_COOKIES)
            cm = CookieManager(cookies_path=out)
            parsed = cm.parse_netscape(out)
            self.assertEqual(parsed.get("ttwid"), "abc")
            self.assertEqual(parsed.get("odin_tt"), "xyz")
            self.assertEqual(parsed.get("passport_csrf_token"), "tok")
            # All three required Douyin keys present -> valid.
            self.assertTrue(cm.validate_douyin_cookies(out))
            self.assertEqual(cm.get_cookie_source(), "file")
            self.assertEqual(cm.get_cookies_for_yt_dlp(), out)

    def test_validation_fails_without_required_keys(self):
        with tempfile.TemporaryDirectory() as d:
            out = _write(d, [
                {
                    "name": "sessionid",
                    "value": "1",
                    "domain": ".douyin.com",
                    "path": "/",
                    "secure": True,
                    "expires": 0,
                }
            ])
            cm = CookieManager(cookies_path=out)
            self.assertFalse(cm.validate_douyin_cookies(out))

    def test_get_cookie_source_none_when_missing(self):
        cm = CookieManager(cookies_path="/no/such/file.txt")
        self.assertEqual(cm.get_cookie_source(), "none")
        self.assertIsNone(cm.get_cookies_for_yt_dlp())

    def test_set_cookies_file_rejects_missing(self):
        cm = CookieManager()
        with self.assertRaises(CookieError):
            cm.set_cookies_file("/no/such/file.txt")

    def test_set_cookies_file_accepts_existing(self):
        with tempfile.TemporaryDirectory() as d:
            out = _write(d, SAMPLE_COOKIES)
            cm = CookieManager()
            cm.set_cookies_file(out)
            self.assertEqual(cm.cookies_path, out)
            self.assertEqual(cm.get_cookie_source(), "file")

    def test_default_cookies_path_inside_app_dir(self):
        cm = CookieManager()
        self.assertEqual(os.path.basename(cm.default_cookies_path()), "cookies.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
