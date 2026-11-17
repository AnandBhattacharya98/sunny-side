"""The refreshed homepage: live numbers, who's hiring and the jobs-near-you map (flag home_refresh)."""
import json
import re
from datetime import datetime, timedelta

import pytest

import dashboard
import flags
import home_stats
from db import get_conn, DB_PATH, init_db

PREFIX = "hr_test_"


@pytest.fixture(autouse=True)
def conn():
    init_db(DB_PATH)
    c = get_conn(DB_PATH)
    c.execute("DELETE FROM jobs WHERE job_id LIKE ?", (PREFIX + "%",))
    c.execute("DELETE FROM feature_flags")
    c.commit()
    flags.clear_cache()
    home_stats.clear_cache()
    yield c
    c.execute("DELETE FROM jobs WHERE job_id LIKE ?", (PREFIX + "%",))
    c.execute("DELETE FROM feature_flags")
    c.commit()
    c.close()
    flags.clear_cache()
    home_stats.clear_cache()


def add(c, n, title, company, location, source="linkedin", days_ago=1, user_id=1):
    when = (datetime.now() - timedelta(days=days_ago)).isoformat()
    c.execute("INSERT INTO jobs (job_id, title, company, location, source, scraped_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (f"{PREFIX}{n}", title, company, location, source, when, user_id))


def seed(c):
    add(c, 1, "Product Manager", "Flipkart", "Bellandur, Bengaluru")
    add(c, 2, "Product Manager", "Flipkart", "Bengaluru, Karnataka, India", days_ago=2, user_id=2)  # same role on another board
    add(c, 3, "SDE Intern", "Flipkart", "Bengaluru, Karnataka, India")
    add(c, 4, "Data Analyst", "Swiggy", "Koramangala, Bengaluru", source="direct")
    add(c, 5, "Product Designer", "Zomato", "Gurugram, Haryana, India")
    add(c, 6, "Secret Role", "Inbox Corp", "Mumbai", source="email")       # from someone's inbox: never public
    add(c, 7, "Recruiter post", "Hirer Co", "Pune", source="recruiter")    # recruiter post: never public
    add(c, 8, "Old Role", "Old Co", "Chennai", days_ago=90)                # too old
    add(c, 9, "Remote Engineer", "Far Co", "India")                       # no place to pin
    c.commit()


def test_numbers_count_public_listings_once(conn):
    seed(conn)
    d = home_stats.compute(conn)
    assert d["roles"] == 5          # 1/2 are one role; 6, 7 and 8 are left out
    assert d["companies"] == 4
    assert d["fresher"] == 1
    names = [p["company"] for p in d["pins"]]
    assert "Inbox Corp" not in names and "Hirer Co" not in names and "Old Co" not in names and "Far Co" not in names
    assert {p["area"] for p in d["pins"] if p["company"] == "Flipkart"} == {"Bellandur", "Bengaluru"}
    assert d["top_companies"][0] == {"company": "Flipkart", "roles": 2}


def test_places_and_families():
    assert home_stats.place_of("Whitefield, Bengaluru")[0] == "Whitefield"
    assert home_stats.place_of("Bangalore Urban, Karnataka, India")[1] == "Bengaluru"
    assert home_stats.place_of("Gurgaon, Haryana")[1] == "Gurugram"
    assert home_stats.place_of("India") is None
    assert home_stats.family_of("Senior Product Designer") == "Design"
    assert home_stats.family_of("Backend Engineer") == "Engineering"


def test_pins_stay_near_their_area():
    lat, lng = home_stats._spread(12.97, 77.59, "acme|Bengaluru")
    assert abs(lat - 12.97) < 0.07 and abs(lng - 77.59) < 0.07
    assert home_stats._spread(12.97, 77.59, "acme|Bengaluru") == (lat, lng)  # same spot every time


def test_off_for_visitors_until_switched_on(conn):
    seed(conn)
    page = dashboard.app.test_client().get("/").get_data(as_text=True)
    assert 'id="jobs-map"' not in page and 'id="hello"' not in page
    flags.set_state(conn, "home_refresh", "on")
    page = dashboard.app.test_client().get("/").get_data(as_text=True)
    assert 'id="jobs-map"' in page and 'id="hello"' in page and "step-mock" in page


def test_map_data_has_no_links_or_private_jobs(conn):
    seed(conn)
    flags.set_state(conn, "home_refresh", "on")
    page = dashboard.app.test_client().get("/").get_data(as_text=True)
    raw = re.search(r'<script type="application/json" id="pins-data">(.*?)</script>', page, re.S).group(1)
    pins = json.loads(raw)
    assert pins and all("url" not in p and "job_id" not in p for p in pins)
    assert "Secret Role" not in raw and "Inbox Corp" not in page


def test_admin_can_preview_it(conn):
    seed(conn)
    admin = dashboard.app.test_client()
    with admin.session_transaction() as s:
        s["user_id"] = 1
        s["username"] = "admin"
    page = admin.get("/?preview=home").get_data(as_text=True)
    assert 'id="jobs-map"' in page
    other = dashboard.app.test_client()
    with other.session_transaction() as s:
        s["user_id"] = 99999
    assert 'id="jobs-map"' not in other.get("/?preview=home").get_data(as_text=True)


def test_hello_overlay_can_actually_be_hidden(conn):
    """Sunny's hello starts hidden and only the first-visit script shows it. Its display:grid
    used to override the hidden attribute, so a repeat visitor was stuck behind it for good."""
    flags.set_state(conn, "home_refresh", "on")
    page = dashboard.app.test_client().get("/").get_data(as_text=True)
    assert re.search(r'<div class="hello" id="hello" hidden', page)
    assert ".hello[hidden] { display: none; }" in page


# ── The signed-in Map view (flag job_map) ──

def signed_in(uid):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
        s["username"] = "admin" if uid == 1 else f"user{uid}"
    return c


def test_board_pins_are_your_own_jobs(conn):
    add(conn, 20, "Product Manager", "Flipkart", "Bellandur, Bengaluru", user_id=1)
    add(conn, 21, "Designer", "Flipkart", "Bellandur, Bengaluru", user_id=1)
    add(conn, 22, "Analyst", "Elsewhere", "India", user_id=1)                 # no city: counted, not pinned
    add(conn, 23, "Someone else's job", "Other Co", "Pune", user_id=2)
    conn.execute("UPDATE jobs SET status = 'applied' WHERE job_id = ?", (PREFIX + "21",))
    conn.commit()
    pins, unplaced = home_stats.board_pins(conn, 1)
    mine = [p for p in pins if p["company"] == "Flipkart"]
    assert len(mine) == 1 and {j["group"] for j in mine[0]["jobs"]} >= {"Applied"}
    assert not any(p["company"] == "Other Co" for p in pins)
    assert unplaced >= 1


def test_map_view_needs_the_switch(conn):
    add(conn, 30, "Product Manager", "Flipkart", "Bellandur, Bengaluru", user_id=1)
    conn.commit()
    page = signed_in(1).get("/map").get_data(as_text=True)
    assert 'id="mv-map"' in page and PREFIX + "30" in page   # admin sees it while it's admin-only
    conn.execute("DELETE FROM users WHERE id = 7301")
    conn.execute("INSERT INTO users (id, username, password_hash) VALUES (7301, 'map_user', 'x')")
    conn.commit()
    r = signed_in(7301).get("/map")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")   # others wait for the switch
    assert 'href="/map"' not in signed_in(7301).get("/progress").get_data(as_text=True)
    flags.set_state(conn, "job_map", "on")
    assert 'href="/map"' in signed_in(7301).get("/progress").get_data(as_text=True)
    conn.execute("DELETE FROM users WHERE id = 7301")
    conn.commit()
