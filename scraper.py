"""
scraper.py — Scrapes PM job listings with zero API keys required.

Sources (all free, no auth):
  - LinkedIn Jobs public pages
  - Company job boards, read from their public Greenhouse / Lever feeds
  - Remote-job boards with open APIs (Remotive, Jobicy, Arbeitnow), for search only

Naukri and Google search were removed: both block scripted requests and never
returned a single job.
"""

import requests
from bs4 import BeautifulSoup
import re
import threading
import time
from datetime import datetime
from db import init_db, job_exists, DB_PATH

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

# ── Company job boards ────────────────────────────────────────────────────
# Most career pages load their openings with JavaScript, so scraping the HTML
# finds nothing. These companies publish the same openings as JSON through
# their applicant-tracking system, which is what we read instead.
#   ats "greenhouse": board token from job-boards.greenhouse.io/<board>
#   ats "lever":      company slug from jobs.lever.co/<board>
COMPANY_BOARDS = [
    {"company": "Razorpay", "ats": "greenhouse", "board": "razorpaysoftwareprivatelimited"},
    {"company": "Groww",    "ats": "greenhouse", "board": "groww", "region": "eu"},
    {"company": "InMobi",   "ats": "greenhouse", "board": "inmobi"},
    {"company": "CRED",     "ats": "lever",      "board": "cred"},
    {"company": "Meesho",   "ats": "lever",      "board": "meesho"},
    {"company": "Paytm",    "ats": "lever",      "board": "paytm"},
]

_BOARD_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,79}$")
_BOARD_URLS = [
    (re.compile(r"(?:job-boards|boards)\.eu\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I), "greenhouse", "eu"),
    (re.compile(r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I), "greenhouse", ""),
    (re.compile(r"boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([\w-]+)", re.I), "greenhouse", ""),
    (re.compile(r"jobs\.lever\.co/([\w-]+)", re.I), "lever", ""),
]


def parse_board_url(text: str) -> dict | None:
    """Reads a Greenhouse or Lever careers link (or "greenhouse:slug" / "lever:slug") into a board config."""
    text = (text or "").strip()
    m = re.match(r"^(greenhouse|lever)\s*[:/]\s*([\w-]+)$", text, re.I)
    if m:
        return {"ats": m.group(1).lower(), "board": m.group(2), "region": ""}
    for pat, ats, region in _BOARD_URLS:
        m = pat.search(text)
        if m and _BOARD_SLUG.match(m.group(1)):
            region = "eu" if ".eu." in text.lower() else region
            return {"ats": ats, "board": m.group(1), "region": region}
    return None


def user_boards(conn, user_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, company, ats, board FROM user_company_boards WHERE user_id = ? ORDER BY id",
                        (user_id,)).fetchall()
    out = []
    for r in rows:
        ats, _, region = (r[2] or "").partition(":")
        out.append({"id": r[0], "company": r[1], "ats": ats, "board": r[3], "region": region, "custom": True})
    return out


def boards_for_user(conn, user_id: int | None) -> list[dict]:
    """The built-in company feeds plus any the user added in Settings."""
    boards = list(COMPANY_BOARDS)
    if user_id is None or conn is None:
        return boards
    seen = {(b["ats"], b["board"].lower()) for b in boards}
    for b in user_boards(conn, user_id):
        if (b["ats"], b["board"].lower()) not in seen:
            seen.add((b["ats"], b["board"].lower()))
            boards.append(b)
    return boards


def boards_for_user_id(user_id: int | None) -> list[dict]:
    if user_id is None:
        return list(COMPANY_BOARDS)
    conn = init_db(DB_PATH)
    try:
        return boards_for_user(conn, user_id)
    finally:
        conn.close()


# A board posting is kept only if its location looks like India (or remote).
INDIA_LOCATION_HINTS = [
    "india", "bengaluru", "bangalore", "mumbai", "delhi", "gurgaon", "gurugram",
    "noida", "pune", "hyderabad", "chennai", "kolkata", "ahmedabad", "jaipur",
    "remote",
]

# -- DEMO_JOBS removed --


def _insert_job(conn, job: dict, user_id: int = 1) -> bool:
    """Returns True if inserted (new), False if already existed."""
    # Ensure job_id is unique per user
    jid = job["job_id"]
    suffix = f"_u{user_id}"
    if not jid.endswith(suffix):
        jid = f"{jid}{suffix}"
    
    # Mutate the dictionary so callers get the updated job_id
    job["job_id"] = jid
    
    if job_exists(conn, jid):
        return False
    # The same opening often turns up from LinkedIn, a company feed and web search.
    if find_duplicate(conn, user_id, job["title"], job["company"]):
        return False
    conn.execute(
        """INSERT OR IGNORE INTO jobs
           (job_id, title, company, location, url, description, source, posted_at, scraped_at, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            jid, job["title"], job["company"],
            job.get("location", "India"), job["url"],
            job.get("description", ""), job["source"],
            job.get("posted_at", ""), datetime.now().isoformat(),
            user_id
        ),
    )
    conn.commit()
    return True


def scrape_linkedin_jobs(conn, user_id: int = 1, keywords: str = "Product Manager") -> list[dict]:
    import urllib.parse
    q_keywords = urllib.parse.quote_plus(keywords)
    url = (
        f"https://www.linkedin.com/jobs/search?"
        f"keywords={q_keywords}&location=India&f_TPR=r86400&position=1&pageNum=0"
    )
    new_jobs = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        count = 0
        for card in soup.select("div.base-card"):
            if count >= 5:
                break
            title_el = card.select_one("h3.base-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle")
            loc_el = card.select_one("span.job-search-card__location")
            link_el = card.select_one("a.base-card__full-link")
            if not title_el or not link_el:
                continue
            title = title_el.get_text(strip=True)
            
            # Loose overlap match: job title should contain at least one term of the keyword phrase
            terms = [t.strip().lower() for t in keywords.replace("-", " ").split() if len(t.strip()) > 2]
            title_lower = title.lower()
            if terms and not any(t in title_lower for t in terms):
                continue
                
            job_url = link_el.get("href", "").split("?")[0]
            job_id = linkedin_job_id(job_url)
            job = {
                "job_id": job_id, "title": title,
                "company": company_el.get_text(strip=True) if company_el else "Unknown",
                "location": loc_el.get_text(strip=True) if loc_el else "India",
                "url": job_url, "source": "linkedin",
                "description": _fetch_description(job_url),
                "posted_at": "",
            }
            if _insert_job(conn, job, user_id=user_id):
                new_jobs.append(job)
                count += 1
            time.sleep(0.4)
    except Exception as e:
        print(f"  [LinkedIn] {e}")
    print(f"  LinkedIn: {len(new_jobs)} new jobs")
    return new_jobs


# ── Duplicates ────────────────────────────────────────────────────────────
_COMPANY_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|inc|llc|llp|corp|corporation|co|technologies|technology|tech|"
    r"software|solutions|services|labs|india|global)\b\.?")
_TITLE_NOISE = re.compile(r"\(.*?\)|\[.*?\]|\b(remote|hybrid|on-?site|urgent|hiring)\b")


def normalize_company(company: str) -> str:
    c = _COMPANY_SUFFIXES.sub(" ", (company or "").lower())
    c = re.sub(r"[^a-z0-9]+", " ", c).strip()
    return c or (company or "").lower().strip()


def normalize_title(title: str) -> str:
    t = _TITLE_NOISE.sub(" ", (title or "").lower())
    t = t.replace("sr.", "senior").replace("sr ", "senior ").replace("jr.", "junior").replace("jr ", "junior ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def job_key(title: str, company: str) -> tuple[str, str]:
    return normalize_title(title), normalize_company(company)


def find_duplicate(conn, user_id: int, title: str, company: str) -> str | None:
    """job_id of a job on this user's board that is the same opening, if any."""
    key = job_key(title, company)
    if not key[0] or not key[1]:
        return None
    rows = conn.execute("SELECT job_id, title, company FROM jobs WHERE user_id = ? AND status != 'archived'",
                        (user_id,)).fetchall()
    for r in rows:
        if job_key(r[1], r[2]) == key:
            return r[0]
    return None


# Columns a job moves through; a job further along is the copy worth keeping.
_STATUS_RANK = {"offer": 7, "interviewing": 6, "applied": 5, "shortlisted": 4, "rejected": 3,
                "ready": 2, "scored": 2, "whatsapp": 1, "new": 1}
_UNTOUCHED = {"new", "scored", "ready"}


def fold_duplicates(conn, user_id: int) -> int:
    """Archives extra copies of the same opening. Only untouched Inbox copies are archived."""
    from db import add_timeline
    rows = conn.execute("SELECT job_id, title, company, status, scraped_at FROM jobs WHERE user_id = ? AND status != 'archived'",
                        (user_id,)).fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        key = job_key(r[1], r[2])
        if key[0] and key[1]:
            groups.setdefault(key, []).append(r)
    folded = 0
    for copies in groups.values():
        if len(copies) < 2:
            continue
        copies.sort(key=lambda r: (-_STATUS_RANK.get(r[3], 0), r[4] or ""))
        keep = copies[0]
        for r in copies[1:]:
            if r[3] not in _UNTOUCHED:
                continue
            conn.execute("UPDATE jobs SET status = 'archived' WHERE job_id = ? AND user_id = ?", (r[0], user_id))
            add_timeline(conn, r[0], f"Archived as a duplicate of {keep[1]} at {keep[2]}")
            folded += 1
    if folded:
        conn.commit()
    return folded


def _title_matches(title: str, keywords: str) -> bool:
    """Every word of the user's designation (3+ letters) must appear in the title.

    Company boards list every role, so a loose "any word" match would let
    "Engineering Manager" through for "Product Manager"."""
    terms = [t.lower() for t in re.split(r"[\s/-]+", keywords) if len(t) > 2]
    title_lower = title.lower()
    return all(t in title_lower for t in terms)


def _is_india_location(location: str) -> bool:
    if not location:
        return True
    loc = location.lower()
    return any(h in loc for h in INDIA_LOCATION_HINTS)


def _html_to_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n", strip=True)


def _fetch_board_postings(cfg: dict) -> list[dict]:
    """Returns a company's open roles as dicts with title, url, location, description."""
    if cfg["ats"] == "greenhouse":
        host = "boards-api.eu.greenhouse.io" if cfg.get("region") == "eu" else "boards-api.greenhouse.io"
        resp = requests.get(f"https://{host}/v1/boards/{cfg['board']}/jobs",
                            params={"content": "true"}, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        postings = []
        for j in resp.json().get("jobs", []):
            # Greenhouse returns the description as HTML-escaped HTML.
            content = BeautifulSoup(j.get("content") or "", "html.parser").get_text()
            postings.append({
                "id": str(j.get("id", "")),
                "title": j.get("title", ""),
                "url": j.get("absolute_url", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "description": _html_to_text(content),
                "posted_at": j.get("updated_at", ""),
            })
        return postings

    if cfg["ats"] == "lever":
        resp = requests.get(f"https://api.lever.co/v0/postings/{cfg['board']}",
                            params={"mode": "json"}, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        postings = []
        for j in resp.json():
            created = j.get("createdAt")
            postings.append({
                "id": str(j.get("id", "")),
                "title": j.get("text", ""),
                "url": j.get("hostedUrl", ""),
                "location": (j.get("categories") or {}).get("location", ""),
                "description": j.get("descriptionPlain") or _html_to_text(j.get("description", "")),
                "posted_at": datetime.fromtimestamp(created / 1000).isoformat() if created else "",
            })
        return postings

    raise ValueError(f"Unknown ATS {cfg['ats']!r}")


def scrape_company_pages(conn, user_id: int = 1, keywords: str = "Product Manager") -> list[dict]:
    new_jobs = []
    for cfg in boards_for_user(conn, user_id):
        try:
            postings = _fetch_board_postings(cfg)
        except Exception as e:
            print(f"  [{cfg['company']}] {e}")
            continue
        mark_closed_board_postings(conn, user_id, cfg, postings)
        count = 0
        for p in postings:
            if count >= 5:
                break
            if not p["title"] or not p["url"]:
                continue
            if not _title_matches(p["title"], keywords) or not _is_india_location(p["location"]):
                continue
            job = {
                "job_id": board_job_id(cfg["company"], p["id"]),
                "title": p["title"],
                "company": cfg["company"],
                "location": p["location"] or "India",
                "url": p["url"],
                "source": "direct",
                "description": p["description"][:3000],
                "posted_at": p["posted_at"],
            }
            if _insert_job(conn, job, user_id=user_id):
                new_jobs.append(job)
                count += 1
    print(f"  Company boards: {len(new_jobs)} new jobs")
    return new_jobs


# ── Closed listings ───────────────────────────────────────────────────────

def mark_closed_board_postings(conn, user_id: int, cfg: dict, postings: list[dict]) -> int:
    """A company feed lists every open role, so a saved role missing from it has closed."""
    prefix = board_job_id(cfg["company"], "")
    suffix = f"_u{user_id}"
    open_ids = {board_job_id(cfg["company"], p["id"]) + suffix for p in postings if p.get("id")}
    rows = conn.execute("SELECT job_id, closed_at FROM jobs WHERE user_id = ? AND source = 'direct' AND status != 'archived'",
                        (user_id,)).fetchall()
    now = datetime.now().isoformat()
    changed = 0
    for job_id, closed_at in rows:
        if not job_id.startswith(prefix) or not job_id.endswith(suffix):
            continue
        is_open = job_id in open_ids
        if is_open and closed_at:
            conn.execute("UPDATE jobs SET closed_at = NULL WHERE job_id = ?", (job_id,))
            changed += 1
        elif not is_open and not closed_at:
            conn.execute("UPDATE jobs SET closed_at = ? WHERE job_id = ?", (now, job_id))
            changed += 1
    if changed:
        conn.commit()
    return changed


_LINKEDIN_CLOSED = re.compile(r"no longer accepting applications", re.I)


def check_linkedin_closures(conn, user_id: int, limit: int = 10, recheck_days: int = 3) -> int:
    """Opens a few saved LinkedIn postings per refresh and marks the ones that stopped taking applications."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=recheck_days)).isoformat()
    rows = conn.execute(
        "SELECT job_id, url FROM jobs WHERE user_id = ? AND url LIKE ? AND closed_at IS NULL "
        "AND status IN ('new', 'scored', 'ready', 'shortlisted', 'whatsapp') "
        "AND (closed_checked_at IS NULL OR closed_checked_at < ?) ORDER BY closed_checked_at IS NOT NULL, closed_checked_at LIMIT ?",
        (user_id, "%linkedin.com/jobs/view/%", cutoff, limit)).fetchall()
    closed = 0
    for job_id, url in rows:
        now = datetime.now().isoformat()
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8)
        except Exception:
            continue
        if resp.status_code == 200 and _LINKEDIN_CLOSED.search(resp.text or ""):
            conn.execute("UPDATE jobs SET closed_at = ?, closed_checked_at = ? WHERE job_id = ?", (now, now, job_id))
            closed += 1
        elif resp.status_code in (200, 404, 410):
            if resp.status_code != 200:
                conn.execute("UPDATE jobs SET closed_at = ?, closed_checked_at = ? WHERE job_id = ?", (now, now, job_id))
                closed += 1
            else:
                conn.execute("UPDATE jobs SET closed_checked_at = ? WHERE job_id = ?", (now, job_id))
        time.sleep(0.3)
    conn.commit()
    return closed


# ── On-demand search (the board's search bar) ─────────────────────────────
# Unlike the scrapers above, these only return postings; nothing is saved
# until the user adds a result to their board.

_BOARD_CACHE: dict[str, tuple[float, list[dict]]] = {}
_BOARD_CACHE_TTL = 600
_board_cache_lock = threading.Lock()


def _board_postings_cached(cfg: dict) -> list[dict]:
    """Company feeds list every opening, so a search reuses them for a few minutes."""
    key = f"{cfg['ats']}:{cfg['board']}"
    now = time.monotonic()
    with _board_cache_lock:
        hit = _BOARD_CACHE.get(key)
        if hit and now - hit[0] < _BOARD_CACHE_TTL:
            return hit[1]
    postings = _fetch_board_postings(cfg)
    with _board_cache_lock:
        _BOARD_CACHE[key] = (now, postings)
    return postings


def _location_matches(location: str, wanted: str) -> bool:
    if not wanted:
        return _is_india_location(location)
    loc = (location or "").lower()
    wanted = wanted.lower().strip()
    # Bengaluru and Bangalore, Gurugram and Gurgaon are the same place.
    aliases = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai"}
    for a, b in aliases.items():
        loc = loc.replace(a, b)
        wanted = wanted.replace(a, b)
    return wanted in loc or "remote" in loc


def board_job_id(company: str, posting_id: str) -> str:
    return f"direct_{company.lower()}_{re.sub(r'[^a-z0-9]', '_', posting_id.lower())}"


def linkedin_job_id(url: str) -> str:
    return f"li_{re.sub(r'[^0-9]', '', url[-20:])}"


def search_linkedin(query: str, location: str = "", limit: int = 15, remote: bool = False) -> list[dict]:
    import urllib.parse
    params = (f"keywords={urllib.parse.quote_plus(query)}"
              f"&location={urllib.parse.quote_plus(location or 'India')}&f_TPR=r2592000"
              + ("&f_WT=2" if remote else ""))
    resp = requests.get(f"https://www.linkedin.com/jobs/search?{params}", headers=HEADERS, timeout=12)
    cards = BeautifulSoup(resp.text, "html.parser").select("div.base-card")
    if not cards:
        # The full page is sometimes served without listings; the guest feed has the same cards.
        resp = requests.get(f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{params}&start=0",
                            headers=HEADERS, timeout=12)
        cards = BeautifulSoup(resp.text, "html.parser").select("div.base-card")
    results = []
    for card in cards:
        title_el = card.select_one("h3.base-search-card__title")
        company_el = card.select_one("h4.base-search-card__subtitle")
        loc_el = card.select_one("span.job-search-card__location")
        link_el = card.select_one("a.base-card__full-link")
        time_el = card.select_one("time")
        if not title_el or not link_el:
            continue
        job_url = link_el.get("href", "").split("?")[0]
        if "/jobs/view/" not in job_url:
            continue
        results.append({
            "source": "linkedin",
            "ref": job_url,
            "title": title_el.get_text(strip=True),
            "company": company_el.get_text(strip=True) if company_el else "Unknown",
            "location": loc_el.get_text(strip=True) if loc_el else (location or "India"),
            "url": job_url,
            "posted_at": time_el.get("datetime", "") if time_el else "",
        })
        if len(results) >= limit:
            break
    return results


def search_company_boards(query: str, location: str = "", limit: int = 15, boards: list[dict] | None = None) -> list[dict]:
    q = query.lower().strip()
    boards = boards or COMPANY_BOARDS

    def one_board(cfg):
        try:
            postings = _board_postings_cached(cfg)
        except Exception as e:
            print(f"  [search {cfg['company']}] {e}")
            return []
        # Typing a company's name lists all of that company's openings.
        whole_company = q and q in cfg["company"].lower()
        out = []
        for p in postings:
            if not p["title"] or not p["url"]:
                continue
            if not whole_company and not _title_matches(p["title"], query):
                continue
            if not _location_matches(p["location"], location):
                continue
            out.append({
                "source": "direct",
                "ref": f"{cfg['board']}:{p['id']}",
                "title": p["title"],
                "company": cfg["company"],
                "location": p["location"] or "India",
                "url": p["url"],
                "posted_at": p["posted_at"],
            })
        return out

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(boards), 12)) as ex:
        groups = list(ex.map(one_board, boards))
    # Newest first, without letting one company crowd out the rest.
    for g in groups:
        g.sort(key=lambda r: r["posted_at"] or "", reverse=True)
    results = []
    while any(groups) and len(results) < limit:
        for g in groups:
            if g and len(results) < limit:
                results.append(g.pop(0))
    return results


# Remote-job boards with open JSON APIs. They cover roles worldwide, so a
# result is kept only if people in the searched place (India by default) can apply.
OPEN_GEO_HINTS = ["worldwide", "anywhere", "global", "asia", "apac", "emea", "international"]


def _remote_geo_ok(geo: str, location: str) -> bool:
    g = (geo or "").lower()
    if not g:
        return True
    if any(h in g for h in OPEN_GEO_HINTS):
        return True
    wanted = (location or "").lower().strip()
    if wanted and wanted != "remote":
        return _location_matches(g, wanted)
    return any(h in g for h in INDIA_LOCATION_HINTS if h != "remote")


def _snippet(text: str, n: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _remote_result(source, ref, title, company, geo, url, posted_at, description, salary=""):
    return {
        "source": source, "ref": ref, "title": title.strip(), "company": (company or "Unknown").strip(),
        "location": f"Remote · {geo}" if geo else "Remote", "url": url, "posted_at": posted_at or "",
        "remote": True, "salary": salary or "", "description": (description or "")[:3000],
    }


def search_remotive(query: str, location: str = "", limit: int = 15) -> list[dict]:
    resp = requests.get("https://remotive.com/api/remote-jobs", params={"search": query, "limit": 50},
                        headers=HEADERS, timeout=12)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        geo = j.get("candidate_required_location", "")
        if not j.get("title") or not j.get("url") or not _remote_geo_ok(geo, location):
            continue
        out.append(_remote_result("remotive", f"remotive:{j.get('id')}", j["title"], j.get("company_name"), geo,
                                  j["url"], j.get("publication_date", ""), _html_to_text(j.get("description", "")),
                                  j.get("salary", "")))
        if len(out) >= limit:
            break
    return out


def search_jobicy(query: str, location: str = "", limit: int = 15) -> list[dict]:
    resp = requests.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": query},
                        headers=HEADERS, timeout=12)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []) or []:
        geo = j.get("jobGeo", "")
        if not j.get("jobTitle") or not j.get("url") or not _remote_geo_ok(geo, location):
            continue
        salary = ""
        if j.get("annualSalaryMin") and j.get("annualSalaryMax"):
            salary = f"{j.get('salaryCurrency', '')} {j['annualSalaryMin']:,}–{j['annualSalaryMax']:,}".strip()
        out.append(_remote_result("jobicy", f"jobicy:{j.get('id')}", j["jobTitle"], j.get("companyName"), geo,
                                  j["url"], j.get("pubDate", ""),
                                  _html_to_text(j.get("jobDescription") or j.get("jobExcerpt", "")), salary))
        if len(out) >= limit:
            break
    return out


def search_arbeitnow(query: str, location: str = "", limit: int = 15) -> list[dict]:
    """Arbeitnow has no search parameter, so its latest listings are matched here."""
    resp = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=12)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("data", []):
        title = j.get("title", "")
        if not title or not j.get("url") or not j.get("remote"):
            continue
        if not _title_matches(title, query) and query.lower() not in (j.get("company_name") or "").lower():
            continue
        geo = j.get("location", "")
        if location and location.lower() != "remote" and not _location_matches(geo, location):
            continue
        created = j.get("created_at")
        posted = datetime.utcfromtimestamp(created).isoformat() if isinstance(created, (int, float)) else ""
        out.append(_remote_result("arbeitnow", f"arbeitnow:{j.get('slug')}", title, j.get("company_name"), geo,
                                  j["url"], posted, _html_to_text(j.get("description", ""))))
        if len(out) >= limit:
            break
    return out


SEARCH_SOURCES = ["linkedin", "direct", "remotive", "jobicy", "arbeitnow"]
REMOTE_SOURCES = {"remotive", "jobicy", "arbeitnow"}
# Where a saved result's link may point, per source.
SOURCE_HOSTS = {"remotive": ("remotive.com",), "jobicy": ("jobicy.com",), "arbeitnow": ("arbeitnow.com",)}


def search_web_jobs(query: str, location: str = "", remote_only: bool = False, boards: list[dict] | None = None) -> dict:
    """Searches LinkedIn, company feeds and remote-job boards at once.

    Returns results plus the sources that failed. Remote boards only list remote
    roles, so they're skipped when someone asks for a specific city."""
    import concurrent.futures
    loc = (location or "").strip()
    city_only = loc and loc.lower() != "remote" and not remote_only
    names = [n for n in SEARCH_SOURCES if not (city_only and n in REMOTE_SOURCES)]
    results, failed = [], []
    where = "" if loc.lower() == "remote" else loc
    remote = remote_only or loc.lower() == "remote"
    fns = {"linkedin": lambda: search_linkedin(query, where, remote=remote),
           "direct": lambda: search_company_boards(query, where, boards=boards),
           "remotive": lambda: search_remotive(query, where),
           "jobicy": lambda: search_jobicy(query, where),
           "arbeitnow": lambda: search_arbeitnow(query, where)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as ex:
        futures = {n: ex.submit(fns[n]) for n in names}
        for name, fut in futures.items():
            try:
                results += fut.result()
            except Exception as e:
                print(f"  [search {name}] {e}")
                failed.append(name)
    if remote:
        # LinkedIn's remote filter is trusted; company feeds must say "remote" themselves.
        results = [r for r in results if r["source"] == "linkedin" or r.get("remote")
                   or "remote" in (r.get("location") or "").lower()]
    seen, unique = set(), []
    for r in results:
        k = (r["title"].lower(), r["company"].lower())
        if k not in seen:
            seen.add(k)
            unique.append(r)
    # Take turns between sources so one busy source doesn't bury the others.
    by_source: dict[str, list] = {}
    for r in unique:
        by_source.setdefault(r["source"], []).append(r)
    mixed = []
    while any(by_source.values()):
        for group in by_source.values():
            if group:
                mixed.append(group.pop(0))
    return {"results": mixed, "failed": failed}


def find_board_posting(ref: str, boards: list[dict] | None = None) -> dict | None:
    """Looks a company-feed result up again by its ref, so what gets saved comes from the feed, not the browser."""
    board, _, posting_id = ref.partition(":")
    cfg = next((c for c in (boards or COMPANY_BOARDS) if c["board"] == board), None)
    if not cfg or not posting_id:
        return None
    for p in _board_postings_cached(cfg):
        if p["id"] == posting_id:
            return {
                "job_id": board_job_id(cfg["company"], p["id"]),
                "title": p["title"], "company": cfg["company"],
                "location": p["location"] or "India", "url": p["url"],
                "source": "direct", "description": p["description"][:3000],
                "posted_at": p["posted_at"],
            }
    return None


def _fetch_description(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in ["div.job-desc", "div#job_description", "div.description__text",
                    "section.description", "div.job-description", "div[class*='description']"]:
            el = soup.select_one(sel)
            if el:
                return el.get_text(separator="\n", strip=True)[:3000]
    except Exception:
        pass
    return ""


def run_all_scrapers(db_path: str = DB_PATH, user_id: int = 1) -> list[dict]:
    conn = init_db(db_path)
    # Query user's designation to use as query keywords
    row = conn.execute("SELECT designation FROM users WHERE id = ?", (user_id,)).fetchone()
    keywords = row[0] if row and row[0] else "Product Manager"
    conn.close()

    print(f"\n[Scraping job listings in parallel for user {user_id}...]")
    
    import concurrent.futures
    
    def run_scraper(scraper_func, *args, **kwargs):
        thread_conn = init_db(db_path)
        try:
            res = scraper_func(thread_conn, *args, **kwargs)
            thread_conn.commit()
            return res
        except Exception as e:
            print(f"Error in thread scraper: {e}")
            return []
        finally:
            thread_conn.close()

    all_new = []
    scrapers = [
        (scrape_linkedin_jobs, (user_id, keywords)),
        (scrape_company_pages, (user_id, keywords)),
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(scrapers)) as executor:
        futures = [executor.submit(run_scraper, func, *args) for func, args in scrapers]
        for future in concurrent.futures.as_completed(futures):
            all_new += future.result()

    conn = init_db(db_path)
    try:
        closed = check_linkedin_closures(conn, user_id)
        folded = fold_duplicates(conn, user_id)
        if closed or folded:
            print(f"  Marked {closed} LinkedIn postings closed, archived {folded} duplicates")
    except Exception as e:
        print(f"  [closures/duplicates] {e}")
    finally:
        conn.close()

    print(f"  Total new jobs this run: {len(all_new)}\n")
    return all_new


if __name__ == "__main__":
    run_all_scrapers()
