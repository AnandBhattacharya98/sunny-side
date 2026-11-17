"""Opens the site's key pages the way a visitor would and fails loudly if any of them break.

  python tools/smoke_test.py http://localhost:8000
  python tools/smoke_test.py https://your-staging-site.onrender.com

It checks the health check, the public pages, then signs up a throwaway account, opens every main
page as that person, and deletes the account again. Only standard-library Python is needed.

  SMOKE_ADMIN_PASSWORD   also sign in as admin and open the Admin Console
  SMOKE_EXPECT_VERSION   a commit id: wait until /healthz reports this version (a deploy finishing)
  SMOKE_WAIT_SECONDS     how long to wait for the site to come up (default 120)
"""
import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PUBLIC_PAGES = ["/", "/login", "/signup", "/forgot", "/hiring", "/privacy", "/terms", "/legal",
                "/manifest.webmanifest", "/sw.js", "/offline"]
SIGNED_IN_PAGES = ["/", "/find", "/progress", "/emails", "/career-switch", "/learn", "/calls"]


class Site:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def request(self, path, data=None, json_body=None, timeout=60):
        headers = {"User-Agent": "sunny-smoke-test"}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
        if body is not None and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                status, text, url = r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            status, text, url = e.code, e.read().decode("utf-8", "replace"), e.geturl()
        m = re.search(r'name="csrf-token" content="([^"]+)"', text) or re.search(r'name="csrf_token" value="([^"]+)"', text)
        if m:
            self.csrf = m.group(1)
        return status, text, url


failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  ({detail})"), flush=True)
    if not ok:
        failures.append(label)


def wait_until_up(site, seconds):
    deadline = time.time() + seconds
    while True:
        try:
            status, text, _ = site.request("/healthz", timeout=15)
            want = os.getenv("SMOKE_EXPECT_VERSION", "")[:7]
            if status == 200 and (not want or json.loads(text).get("version") == want):
                return status, text
        except Exception as e:  # not listening yet, or a free instance waking up
            status, text = 0, str(e)
        if time.time() > deadline:
            return status, text
        time.sleep(3)


def page_ok(status, text):
    return status == 200 and "Something went wrong on our side" not in text and "Traceback" not in text


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    site = Site(sys.argv[1])

    status, text = wait_until_up(site, int(os.getenv("SMOKE_WAIT_SECONDS", "120")))
    try:
        health = json.loads(text or "{}")
    except ValueError:
        health = {}
    want = os.getenv("SMOKE_EXPECT_VERSION", "")[:7]
    check("/healthz answers ok", status == 200 and health.get("ok") is True and (not want or health.get("version") == want),
          f"{status} {text[:200]}" + (f", wanted version {want}" if want else ""))
    if failures:
        return finish()

    for path in PUBLIC_PAGES:
        status, text, _ = site.request(path)
        check(f"public page {path}", page_ok(status, text), f"HTTP {status}")

    username = "smoke_" + secrets.token_hex(4)
    password = secrets.token_urlsafe(14)
    site.request("/signup")
    status, text, url = site.request("/signup", data={"username": username, "password": password,
                                                       "name": "Smoke Test", "designation": "Product Manager",
                                                       "csrf_token": site.csrf})
    signed_up = status == 200 and not url.rstrip("/").endswith("/signup")
    check("sign up a throwaway account", signed_up, f"HTTP {status}, ended on {url}")
    if signed_up:
        for path in SIGNED_IN_PAGES:
            status, text, url = site.request(path)
            check(f"signed-in page {path}", page_ok(status, text) and "/login" not in url, f"HTTP {status}, ended on {url}")
        status, text, _ = site.request("/api/stats")
        check("api /api/stats", status == 200, f"HTTP {status}")
        site.request("/")  # fresh CSRF token for the delete
        status, text, _ = site.request("/api/account/delete", json_body={"confirm": "DELETE"})
        check("delete the throwaway account", status == 200, f"HTTP {status} {text[:200]}")

    admin_pw = os.getenv("SMOKE_ADMIN_PASSWORD", "")
    if admin_pw:
        admin = Site(sys.argv[1])
        admin.request("/login")
        status, text, url = admin.request("/login", data={"username": "admin", "password": admin_pw, "csrf_token": admin.csrf})
        check("admin signs in", status == 200 and "/login" not in url, f"HTTP {status}, ended on {url}")
        status, text, _ = admin.request("/users")
        check("Admin Console /users", page_ok(status, text) and "feature-switches" in text, f"HTTP {status}")
    return finish()


def finish():
    if failures:
        print(f"\n{len(failures)} check(s) failed: " + ", ".join(failures))
        return 1
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
