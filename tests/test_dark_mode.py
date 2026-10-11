"""Dark mode: every page follows the saved theme or the device setting, with no forced light pages."""
import os

import dashboard


def _get(path):
    with dashboard.app.test_client() as c:
        return c.get(path).get_data(as_text=True)


def test_sign_in_pages_follow_the_theme():
    for path in ("/login", "/signup", "/forgot"):
        html = _get(path)
        assert "setAttribute('data-theme', 'light')" not in html, path
        assert "prefers-color-scheme: dark" in html, path


def test_homepage_has_dark_colors():
    html = _get("/")
    assert '[data-theme="dark"]' in html
    assert "localStorage.getItem('theme')" in html


def test_picks_strip_has_no_hard_coded_light_colors():
    html = open(os.path.join(os.path.dirname(dashboard.__file__), "dashboard.html"), encoding="utf-8").read()
    strip = html[html.index('id="daily-picks-strip"'):][:200]
    assert "#FFF" not in strip
    assert '[data-theme="dark"] .dp-strip' in html
