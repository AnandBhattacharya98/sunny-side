"""Installable web app files and the phone apps' sign-in guard. Run with: python -m pytest tests/"""
import json

import dashboard


def client():
    return dashboard.app.test_client()


def test_manifest_served_without_login():
    r = client().get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.mimetype == "application/manifest+json"
    data = json.loads(r.data)
    assert data["display"] == "standalone"
    for icon in data["icons"]:
        assert client().get(icon["src"]).status_code == 200


def test_service_worker_and_offline_page_served_without_login():
    sw = client().get("/sw.js")
    assert sw.status_code == 200
    assert "javascript" in sw.mimetype
    assert sw.headers["Cache-Control"] == "no-cache"
    assert client().get("/offline").status_code == 200


def test_service_worker_never_caches_pages_or_api():
    src = client().get("/sw.js").get_data(as_text=True)
    # Only the fixed shell list goes in the cache; pages fall back to /offline when offline.
    assert "cache.put" not in src and ".put(" not in src


def test_pages_link_the_manifest():
    html = client().get("/login").get_data(as_text=True)
    assert 'rel="manifest"' in html and "/sw.js" in html


def test_google_sign_in_inside_app_explains_instead_of_redirecting(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc")
    ua = "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36 SunnySideApp"
    r = client().get("/auth/google", headers={"User-Agent": ua})
    assert r.status_code == 200
    assert "work inside the app yet" in r.get_data(as_text=True)

    r = client().get("/auth/google", headers={"User-Agent": "Mozilla/5.0 Chrome/140"})
    assert r.status_code == 302 and "accounts.google.com" in r.headers["Location"]
