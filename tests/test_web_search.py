"""Tests for the board search bar's web search. Run with: python -m pytest tests/

Network calls are replaced with canned LinkedIn HTML and Greenhouse/Lever JSON."""
import os
import sys
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["GEMINI_API_KEY"] = ""
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402
import dashboard  # noqa: E402
import voice_engine as ve  # noqa: E402
from db import get_conn, DB_PATH  # noqa: E402

LINKEDIN_HTML = """
<div class="base-card">
  <h3 class="base-search-card__title">Product Manager, Growth</h3>
  <h4 class="base-search-card__subtitle">Zepto</h4>
  <span class="job-search-card__location">Bengaluru, Karnataka, India</span>
  <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/product-manager-growth-4100000001?refId=x"></a>
  <time datetime="2026-10-01"></time>
</div>
<div class="base-card">
  <h3 class="base-search-card__title">Senior Product Manager</h3>
  <h4 class="base-search-card__subtitle">Swiggy</h4>
  <span class="job-search-card__location">Bengaluru</span>
  <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/senior-pm-4100000002"></a>
</div>
"""

GREENHOUSE = {"jobs": [
    {"id": 11, "title": "Product Manager - Payments", "absolute_url": "https://job-boards.greenhouse.io/razorpay/jobs/11",
     "location": {"name": "Bengaluru"}, "content": "&lt;p&gt;Own payments.&lt;/p&gt;", "updated_at": "2026-10-05T10:00:00"},
    {"id": 12, "title": "Engineering Manager", "absolute_url": "https://job-boards.greenhouse.io/razorpay/jobs/12",
     "location": {"name": "Bengaluru"}, "content": "", "updated_at": "2026-10-05T10:00:00"},
    {"id": 13, "title": "Product Manager - Lending", "absolute_url": "https://job-boards.greenhouse.io/razorpay/jobs/13",
     "location": {"name": "London"}, "content": "", "updated_at": "2026-10-05T10:00:00"},
]}

LEVER = [
    {"id": "abc", "text": "Product Manager, Rewards", "hostedUrl": "https://jobs.lever.co/cred/abc",
     "categories": {"location": "Bangalore"}, "descriptionPlain": "Rewards.", "createdAt": 1759600000000},
    {"id": "def", "text": "Designer", "hostedUrl": "https://jobs.lever.co/cred/def",
     "categories": {"location": "Bangalore"}, "descriptionPlain": "Design.", "createdAt": 1759600000000},
]


class FakeResp:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def fake_get(url, **kwargs):
    if "linkedin.com/jobs/search" in url:
        return FakeResp(text=LINKEDIN_HTML)
    if "linkedin.com/jobs/view" in url:
        return FakeResp(text='<div class="description__text">A great PM role.</div>')
    if "razorpaysoftwareprivatelimited" in url:
        return FakeResp(data=GREENHOUSE)
    if "api.lever.co/v0/postings/cred" in url:
        return FakeResp(data=LEVER)
    if "greenhouse" in url:
        return FakeResp(data={"jobs": []})
    return FakeResp(data=[])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(scraper.requests, "get", fake_get)
    scraper._BOARD_CACHE.clear()


@pytest.fixture
def client(monkeypatch):
    ve.rate_limiter.reset()
    # Score synchronously with a stub so tests stay offline and deterministic.
    monkeypatch.setattr(dashboard, "run_in_background", lambda key, fn, *a: fn(*a) or True)
    monkeypatch.setattr(dashboard, "score_job", lambda *a, **k: {"score": 8.2, "fit_summary": "Good fit.", "key_requirements": []})
    conn = get_conn(DB_PATH)
    for uid in (201, 202):
        conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name) VALUES (?, ?, 'x', ?)",
                     (uid, f"user{uid}", f"User {uid}"))
    conn.execute("DELETE FROM jobs WHERE user_id IN (201, 202)")
    conn.execute("INSERT INTO jobs (job_id, title, company, url, status, user_id) VALUES "
                 "('mine1', 'Senior Product Manager', 'Swiggy', 'https://in.linkedin.com/jobs/view/senior-pm-4100000002', 'new', 201)")
    conn.commit()
    conn.close()
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 201
        yield c


# ── Scraper search helpers ───────────────────────────────────────────────

def test_company_boards_match_title_and_location():
    results = scraper.search_company_boards("Product Manager", "Bengaluru")
    titles = {r["title"] for r in results}
    # Bangalore counts as Bengaluru; London and non-PM roles are left out.
    assert titles == {"Product Manager - Payments", "Product Manager, Rewards"}
    assert all(r["source"] == "direct" for r in results)


def test_company_name_lists_all_its_openings():
    titles = {r["title"] for r in scraper.search_company_boards("cred")}
    assert titles == {"Product Manager, Rewards", "Designer"}


def test_linkedin_results_parse():
    results = scraper.search_linkedin("Product Manager", "Bengaluru")
    assert [r["company"] for r in results] == ["Zepto", "Swiggy"]
    assert results[0]["url"] == "https://in.linkedin.com/jobs/view/product-manager-growth-4100000001"
    assert results[0]["posted_at"] == "2026-10-01"


def test_one_failing_source_still_returns_the_other(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("blocked")
    monkeypatch.setattr(scraper, "search_linkedin", broken)
    found = scraper.search_web_jobs("Product Manager")
    assert found["failed"] == ["linkedin"]
    assert found["results"]


# ── API ──────────────────────────────────────────────────────────────────

def test_search_requires_login():
    with dashboard.app.test_client() as c:
        assert c.get("/api/jobs/search?q=pm").status_code == 401


def test_search_needs_a_query(client):
    assert client.get("/api/jobs/search?q=a").status_code == 400


def test_search_marks_jobs_already_on_board(client):
    data = client.get("/api/jobs/search?q=Product+Manager&location=Bengaluru").get_json()
    assert data["ok"]
    by_company = {r["company"]: r for r in data["results"]}
    assert by_company["Swiggy"]["on_board"] is True
    assert by_company["Zepto"]["on_board"] is False
    assert by_company["Razorpay"]["on_board"] is False


def test_add_board_result_uses_feed_data_and_scores_it(client):
    res = client.post("/api/jobs/search/add", json={"source": "direct", "ref": "razorpaysoftwareprivatelimited:11",
                                                    "title": "Something the browser made up"}).get_json()
    assert res["ok"] and not res.get("already")
    conn = get_conn(DB_PATH)
    row = conn.execute("SELECT title, company, description, ai_score, user_id FROM jobs WHERE job_id = ?", (res["job_id"],)).fetchone()
    conn.close()
    assert row["title"] == "Product Manager - Payments"
    assert row["company"] == "Razorpay"
    assert "Own payments" in row["description"]
    assert row["ai_score"] == 8.2
    assert row["user_id"] == 201


def test_add_linkedin_result_fetches_description(client):
    url = "https://in.linkedin.com/jobs/view/product-manager-growth-4100000001"
    res = client.post("/api/jobs/search/add", json={"source": "linkedin", "ref": url, "title": "Product Manager, Growth",
                                                    "company": "Zepto", "location": "Bengaluru"}).get_json()
    assert res["ok"]
    conn = get_conn(DB_PATH)
    row = conn.execute("SELECT description, source FROM jobs WHERE job_id = ?", (res["job_id"],)).fetchone()
    conn.close()
    assert row["description"] == "A great PM role."
    assert row["source"] == "linkedin"


def test_add_rejects_non_linkedin_urls(client):
    for url in ("https://evil.example/jobs/view/1", "http://169.254.169.254/latest", "javascript:alert(1)"):
        res = client.post("/api/jobs/search/add", json={"source": "linkedin", "ref": url, "title": "PM", "company": "X"})
        assert res.status_code == 400


def test_add_twice_reports_already_on_board(client):
    body = {"source": "direct", "ref": "cred:abc"}
    assert not client.post("/api/jobs/search/add", json=body).get_json().get("already")
    assert client.post("/api/jobs/search/add", json=body).get_json()["already"] is True


def test_adds_stay_with_the_user_who_added_them(client):
    client.post("/api/jobs/search/add", json={"source": "direct", "ref": "cred:abc"})
    with dashboard.app.test_client() as other:
        with other.session_transaction() as s:
            s["user_id"] = 202
        data = other.get("/api/jobs/search?q=Product+Manager").get_json()
        assert not any(r["on_board"] for r in data["results"])
        res = other.post("/api/jobs/search/add", json={"source": "direct", "ref": "cred:abc"}).get_json()
        assert res["ok"] and not res.get("already")
    conn = get_conn(DB_PATH)
    owners = {r[0] for r in conn.execute("SELECT user_id FROM jobs WHERE title = 'Product Manager, Rewards'").fetchall()}
    conn.close()
    assert owners == {201, 202}


def test_unknown_board_posting_is_404(client):
    assert client.post("/api/jobs/search/add", json={"source": "direct", "ref": "cred:nope"}).status_code == 404
