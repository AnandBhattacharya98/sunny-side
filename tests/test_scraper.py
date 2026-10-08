"""Tests for the company job-board scraper. Run with: python -m pytest tests/

Network calls are replaced with canned Greenhouse / Lever responses shaped like the real APIs."""
import os
import sys
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "scraper_test.db")
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402
from db import init_db  # noqa: E402

GREENHOUSE = {
    "jobs": [
        {"id": 7001, "title": "Product Manager II - Ads", "absolute_url": "https://job-boards.greenhouse.io/x/jobs/7001",
         "location": {"name": "Bangalore"}, "updated_at": "2026-10-01T10:00:00-04:00",
         "content": "&lt;p&gt;Own the &lt;strong&gt;ads&lt;/strong&gt; roadmap.&lt;/p&gt;"},
        {"id": 7002, "title": "Engineering Manager", "absolute_url": "https://job-boards.greenhouse.io/x/jobs/7002",
         "location": {"name": "Bangalore"}, "updated_at": "", "content": ""},
        {"id": 7003, "title": "Senior Product Manager", "absolute_url": "https://job-boards.greenhouse.io/x/jobs/7003",
         "location": {"name": "San Mateo, CA"}, "updated_at": "", "content": ""},
    ]
}
LEVER = [
    {"id": "abc-123", "text": "Associate Product Manager", "hostedUrl": "https://jobs.lever.co/x/abc-123",
     "categories": {"location": "Noida, Uttar Pradesh"}, "descriptionPlain": "Build UPI features.",
     "createdAt": 1759300000000},
    {"id": "def-456", "text": "Product Designer", "hostedUrl": "https://jobs.lever.co/x/def-456",
     "categories": {"location": "Bangalore, Karnataka"}, "descriptionPlain": "", "createdAt": None},
]


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "jobs.db"))
    yield c
    c.close()


@pytest.fixture
def fake_boards(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "greenhouse.io" in url:
            return FakeResp(GREENHOUSE)
        if "lever.co" in url:
            return FakeResp(LEVER)
        return FakeResp({}, 404)

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper, "COMPANY_BOARDS", [
        {"company": "Razorpay", "ats": "greenhouse", "board": "razorpay"},
        {"company": "Groww", "ats": "greenhouse", "board": "groww", "region": "eu"},
        {"company": "Paytm", "ats": "lever", "board": "paytm"},
    ])
    return calls


def test_company_boards_keep_matching_india_roles(conn, fake_boards):
    jobs = scraper.scrape_company_pages(conn, user_id=1, keywords="Product Manager")
    got = {(j["company"], j["title"]) for j in jobs}
    assert got == {
        ("Razorpay", "Product Manager II - Ads"),
        ("Groww", "Product Manager II - Ads"),
        ("Paytm", "Associate Product Manager"),
    }
    gh = next(j for j in jobs if j["company"] == "Razorpay")
    assert gh["description"] == "Own the\nads\nroadmap."
    assert gh["url"] == "https://job-boards.greenhouse.io/x/jobs/7001"
    assert gh["source"] == "direct" and gh["job_id"].endswith("_u1")
    assert any("boards-api.eu.greenhouse.io/v1/boards/groww" in u for u in fake_boards)


def test_company_boards_do_not_duplicate(conn, fake_boards):
    assert len(scraper.scrape_company_pages(conn, user_id=1)) == 3
    assert scraper.scrape_company_pages(conn, user_id=1) == []
    # Another user gets their own copies.
    assert len(scraper.scrape_company_pages(conn, user_id=2)) == 3


def test_one_failing_board_does_not_stop_others(conn, monkeypatch):
    def fake_get(url, **kwargs):
        if "lever.co" in url:
            return FakeResp(LEVER)
        return FakeResp({}, 404)

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper, "COMPANY_BOARDS", [
        {"company": "Gone", "ats": "greenhouse", "board": "gone"},
        {"company": "Paytm", "ats": "lever", "board": "paytm"},
    ])
    jobs = scraper.scrape_company_pages(conn, user_id=1)
    assert [j["company"] for j in jobs] == ["Paytm"]


@pytest.mark.parametrize("title,keywords,ok", [
    ("Senior Product Manager", "Product Manager", True),
    ("Engineering Manager", "Product Manager", False),
    ("Director - Product Management", "Product Manager", False),
    ("Data Analyst", "Data Analyst", True),
])
def test_title_matches(title, keywords, ok):
    assert scraper._title_matches(title, keywords) is ok


def test_run_all_scrapers_only_uses_live_sources(monkeypatch):
    called = []
    monkeypatch.setattr(scraper, "scrape_linkedin_jobs", lambda c, *a: called.append("linkedin") or [])
    monkeypatch.setattr(scraper, "scrape_company_pages", lambda c, *a: called.append("boards") or [])
    scraper.run_all_scrapers(os.environ["DB_PATH"], user_id=1)
    assert sorted(called) == ["boards", "linkedin"]
    assert not hasattr(scraper, "scrape_naukri")
    assert not hasattr(scraper, "scrape_google_search_jobs")
