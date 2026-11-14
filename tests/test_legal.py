"""Legal pages, site footer and cookie consent wiring."""
import re

import pytest

import dashboard
import legal


@pytest.fixture
def anon():
    return dashboard.app.test_client()


@pytest.mark.parametrize("slug", ["legal", *legal.PAGES])
def test_legal_pages_are_public(anon, slug):
    res = anon.get("/" + slug)
    assert res.status_code == 200, slug
    html = res.get_data(as_text=True)
    assert "/static/site/consent.js" in html and "ss-footer" in html
    assert "Created by" in html and f"&copy; {legal.site_info()['year']} Sunny Side" in html


def test_aliases_redirect(anon):
    for path, target in (("/privacy-policy", "/privacy"), ("/tos", "/terms"), ("/terms-of-service", "/terms"),
                         ("/cookie-policy", "/cookies")):
        res = anon.get(path)
        assert res.status_code == 301 and res.headers["Location"].endswith(target)


def test_owner_and_contact_come_from_env(anon, monkeypatch):
    monkeypatch.setenv("SITE_OWNER_NAME", "Jane Maker")
    monkeypatch.setenv("SITE_CONTACT_EMAIL", "hello@example.com")
    html = anon.get("/contact").get_data(as_text=True)
    assert "Jane Maker" in html and 'mailto:hello@example.com' in html
    assert "being set up" not in html


def test_contact_without_email_shows_placeholder(anon, monkeypatch):
    monkeypatch.delenv("SITE_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("SITE_OWNER_NAME", raising=False)
    html = anon.get("/contact").get_data(as_text=True)
    assert "being set up" in html and "mailto:" not in html and ">AB<" in html


def test_homepage_and_auth_pages_link_the_policies(anon):
    home = anon.get("/").get_data(as_text=True)
    for href in ("/privacy", "/terms", "/cookies", "/your-data", "/contact"):
        assert f'href="{href}"' in home
    assert "data-cookie-settings" in home
    signup = anon.get("/signup").get_data(as_text=True)
    assert 'href="/terms"' in signup and 'href="/privacy"' in signup and "18 or older" in signup
    login = anon.get("/login").get_data(as_text=True)
    assert "ss-minifoot" in login and 'data-cookie-settings' in login


def test_consent_script_loads_before_theme_is_read(anon):
    for path in ("/", "/login", "/privacy"):
        html = anon.get(path).get_data(as_text=True)
        head = html.split("</head>")[0]
        assert head.index("/static/site/consent.js") < head.index("localStorage.getItem('theme')"), path


def test_board_sidebar_has_legal_links():
    from db import get_conn, DB_PATH
    conn = get_conn(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, resume_text) VALUES (651, 'legal651', 'x', 'my resume')")
    conn.commit()
    conn.close()
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 651
        s["username"] = "legal651"
    html = c.get("/").get_data(as_text=True)
    assert "ss-sidefoot" in html and 'href="/privacy"' in html


ESSENTIAL_KEYS = {"mic_consent_accepted"}


def test_cookie_policy_lists_every_preference_key_the_app_writes():
    """Every localStorage key the pages write must be named in the Cookie Policy and treated as a preference."""
    src = open("dashboard.html").read() + open("landing.html").read()
    keys = set(re.findall(r"localStorage\.setItem\('([^']+)'", src))
    keys |= set(re.findall(r"vaSet\('localStorage', '([^']+)'", src))
    keys |= {k + "*" for k in re.findall(r"vaSet\('localStorage', '([^']+)' \+", src)}
    policy = open("legal/cookies.html").read()
    consent = open("static/site/consent.js").read()
    assert keys, "expected to find storage keys"
    for key in keys:
        name = key.rstrip("*")
        assert name in policy, f"{key} missing from Cookie Policy"
        if name in ESSENTIAL_KEYS:
            continue  # a consent record, kept like the ss_consent cookie
        assert name.startswith("va_") or f"'{name}'" in consent, f"{key} not guarded in consent.js"


def test_static_assets_served(anon):
    assert anon.get("/static/site/consent.js").status_code == 200
    assert anon.get("/static/site/site.css").status_code == 200
