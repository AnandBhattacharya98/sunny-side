"""Motion layer: every page loads the same animation files, served locally, and all of it respects reduce motion."""
import os

import dashboard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get(path):
    with dashboard.app.test_client() as c:
        return c.get(path)


def test_animation_files_are_served_locally():
    for path in ("/static/vendor/sunny-anim.min.js", "/static/vendor/lenis.min.js",
                 "/static/motion/app-motion.css", "/static/motion/app-motion.js"):
        res = _get(path)
        assert res.status_code == 200, path
        assert len(res.get_data()) > 500, path


def test_bundle_exposes_the_libraries():
    js = open(os.path.join(ROOT, "static/vendor/sunny-anim.min.js"), encoding="utf-8").read()
    assert "window.SunnyAnim" in js
    for name in ("autoAnimate", "confetti", "splitText", "stagger"):
        assert name in js, name


def test_sign_in_pages_load_the_motion_layer():
    for path in ("/login", "/signup", "/forgot"):
        html = _get(path).get_data(as_text=True)
        assert "/static/motion/app-motion.css" in html, path
        assert "/static/motion/app-motion.js" in html, path
        assert "/static/vendor/sunny-anim.min.js" in html, path


def test_homepage_is_animated_but_respects_reduce_motion():
    html = _get("/").get_data(as_text=True)
    assert "/static/vendor/sunny-anim.min.js" in html
    assert "prefers-reduced-motion: reduce" in html
    assert "@view-transition { navigation: none; }" in html
    # The headline is still read whole by screen readers while its words animate
    assert 'aria-label="Every job you apply to, tracked in one place."' in html


def test_motion_css_turns_everything_off_for_reduce_motion():
    css = open(os.path.join(ROOT, "static/motion/app-motion.css"), encoding="utf-8").read()
    reduce_block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "@view-transition { navigation: none; }" in reduce_block
    for sel in (".jcard", ".modal-backdrop .modal", ".auth-shell::before", ".pg-wbar"):
        assert sel in reduce_block, sel


def test_motion_js_checks_reduce_motion_and_never_blocks_a_move():
    js = open(os.path.join(ROOT, "static/motion/app-motion.js"), encoding="utf-8").read()
    assert "prefers-reduced-motion: reduce" in js
    assert "disableForReducedMotion: true" in js
    # The original move and Sunny's reaction still run even if the animation throws
    assert "return react.apply(this, arguments);" in js
    assert "return orig.apply(this, arguments);" in js


def test_logout_skips_the_page_transition_and_clears_cached_pages():
    js = open(os.path.join(ROOT, "static/motion/app-motion.js"), encoding="utf-8").read()
    assert 'a[href="/logout"]' in js and "skipTransition" in js
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 1
            s["username"] = "admin"
        res = c.get("/logout")
        assert res.status_code == 302
        assert res.headers["Cache-Control"] == "no-store"
        assert res.headers["Clear-Site-Data"] == '"cache"'


def test_signed_in_pages_are_not_cached():
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 1
            s["username"] = "admin"
        res = c.get("/progress")
        assert res.headers.get("Cache-Control") == "no-store"
