"""
scraper.py — Scrapes PM job listings with zero API keys required.

Sources (all free, no auth):
  - LinkedIn Jobs public pages
  - Company job boards, read from their public Greenhouse / Lever feeds

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
    for cfg in COMPANY_BOARDS:
        try:
            postings = _fetch_board_postings(cfg)
        except Exception as e:
            print(f"  [{cfg['company']}] {e}")
            continue
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


def search_linkedin(query: str, location: str = "", limit: int = 15) -> list[dict]:
    import urllib.parse
    url = ("https://www.linkedin.com/jobs/search?"
           f"keywords={urllib.parse.quote_plus(query)}"
           f"&location={urllib.parse.quote_plus(location or 'India')}&f_TPR=r2592000")
    resp = requests.get(url, headers=HEADERS, timeout=12)
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select("div.base-card"):
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


def search_company_boards(query: str, location: str = "", limit: int = 15) -> list[dict]:
    q = query.lower().strip()

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
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(COMPANY_BOARDS)) as ex:
        groups = list(ex.map(one_board, COMPANY_BOARDS))
    # Newest first, without letting one company crowd out the rest.
    for g in groups:
        g.sort(key=lambda r: r["posted_at"] or "", reverse=True)
    results = []
    while any(groups) and len(results) < limit:
        for g in groups:
            if g and len(results) < limit:
                results.append(g.pop(0))
    return results


def search_web_jobs(query: str, location: str = "") -> dict:
    """Searches LinkedIn and the company feeds at once. Returns results plus the sources that failed."""
    import concurrent.futures
    sources = {"linkedin": search_linkedin, "direct": search_company_boards}
    results, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futures = {name: ex.submit(fn, query, location) for name, fn in sources.items()}
        for name, fut in futures.items():
            try:
                results += fut.result()
            except Exception as e:
                print(f"  [search {name}] {e}")
                failed.append(name)
    seen, unique = set(), []
    for r in results:
        k = (r["title"].lower(), r["company"].lower())
        if k not in seen:
            seen.add(k)
            unique.append(r)
    return {"results": unique, "failed": failed}


def find_board_posting(ref: str) -> dict | None:
    """Looks a company-feed result up again by its ref, so what gets saved comes from the feed, not the browser."""
    board, _, posting_id = ref.partition(":")
    cfg = next((c for c in COMPANY_BOARDS if c["board"] == board), None)
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

    print(f"  Total new jobs this run: {len(all_new)}\n")
    return all_new


if __name__ == "__main__":
    run_all_scrapers()
