"""
scraper.py — Scrapes PM job listings with zero API keys required.

Sources (all free, no auth):
  - Naukri.com public pages
  - LinkedIn Jobs public pages
  - Direct company career pages
  - Instahyre public search

Falls back to demo seed data if all requests fail (e.g. no internet).
"""

import requests
from bs4 import BeautifulSoup
import re
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

# ── Add any company you want crawled ──────────────────────────────────────
COMPANY_CAREER_PAGES = [
    {
        "company": "Razorpay",
        "url": "https://razorpay.com/jobs/",
        "selector": "a",
        "filter_keywords": ["product manager", "product management"],
    },
    {
        "company": "CRED",
        "url": "https://careers.cred.club/",
        "selector": "a",
        "filter_keywords": ["product"],
    },
    {
        "company": "Zepto",
        "url": "https://www.zepto.team/careers",
        "selector": "a",
        "filter_keywords": ["product manager"],
    },
    {
        "company": "Groww",
        "url": "https://groww.in/careers",
        "selector": "a",
        "filter_keywords": ["product manager"],
    },
    {
        "company": "PhonePe",
        "url": "https://www.phonepe.com/careers/",
        "selector": "a",
        "filter_keywords": ["product"],
    },
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


def scrape_naukri(conn, max_pages: int = 2, user_id: int = 1, keywords: str = "Product Manager") -> list[dict]:
    new_jobs = []
    kw_hyphenated = keywords.lower().replace(" ", "-").replace("/", "-")
    for page in range(1, max_pages + 1):
        url = (
            f"https://www.naukri.com/{kw_hyphenated}-jobs-in-india-{page}"
            if page > 1 else
            f"https://www.naukri.com/{kw_hyphenated}-jobs-in-india"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select("article.jobTuple") or soup.select("div.srp-jobtuple-wrapper")
            for card in cards:
                title_el = card.select_one("a.title") or card.select_one("a.jobTitle")
                company_el = card.select_one("a.subTitle") or card.select_one("a.companyInfo")
                loc_el = card.select_one("li.location span") or card.select_one("span.locWdth")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                
                # Loose overlap match: job title should contain at least one term of the keyword phrase
                terms = [t.strip().lower() for t in keywords.replace("-", " ").split() if len(t.strip()) > 2]
                title_lower = title.lower()
                if terms and not any(t in title_lower for t in terms):
                    continue
                
                job_url = title_el.get("href", url)
                job_id = f"naukri_{re.sub(r'[^a-z0-9]', '_', job_url[-40:].lower())}"
                job = {
                    "job_id": job_id, "title": title,
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": loc_el.get_text(strip=True) if loc_el else "India",
                    "url": job_url, "source": "naukri",
                    "description": _fetch_description(job_url),
                    "posted_at": "",
                }
                if _insert_job(conn, job, user_id=user_id):
                    new_jobs.append(job)
                time.sleep(0.8)
        except Exception as e:
            print(f"  [Naukri p{page}] {e}")
        time.sleep(1.5)
    print(f"  Naukri: {len(new_jobs)} new jobs")
    return new_jobs


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
        for card in soup.select("div.base-card"):
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
            job_id = f"li_{re.sub(r'[^0-9]', '', job_url[-20:])}"
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
            time.sleep(1)
    except Exception as e:
        print(f"  [LinkedIn] {e}")
    print(f"  LinkedIn: {len(new_jobs)} new jobs")
    return new_jobs


def scrape_company_pages(conn, user_id: int = 1) -> list[dict]:
    new_jobs = []
    for cfg in COMPANY_CAREER_PAGES:
        try:
            resp = requests.get(cfg["url"], headers=HEADERS, timeout=12)
            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.select(cfg["selector"]):
                text = link.get_text(strip=True).lower()
                if not any(k in text for k in cfg["filter_keywords"]):
                    continue
                href = link.get("href", "")
                if not href or href == "#":
                    continue
                if href.startswith("/"):
                    base = "/".join(cfg["url"].split("/")[:3])
                    href = base + href
                elif not href.startswith("http"):
                    continue
                job_id = f"direct_{cfg['company'].lower()}_{re.sub(r'[^a-z0-9]','_',href[-30:])}"
                job = {
                    "job_id": job_id,
                    "title": link.get_text(strip=True) or "Product Manager",
                    "company": cfg["company"], "location": "India",
                    "url": href, "source": "direct", "description": "",
                    "posted_at": "",
                }
                if _insert_job(conn, job, user_id=user_id):
                    new_jobs.append(job)
            time.sleep(1.5)
        except Exception as e:
            print(f"  [{cfg['company']}] {e}")
    print(f"  Company pages: {len(new_jobs)} new jobs")
    return new_jobs


def seed_demo_jobs(conn, user_id: int = 1) -> list[dict]:
    return []


def scrape_google_search_jobs(conn, user_id: int = 1, keywords: str = "Product Manager") -> list[dict]:
    import urllib.parse
    q = f"{keywords} jobs India"
    url = f"https://www.google.com/search?q={urllib.parse.quote_plus(q)}&num=30"
    new_jobs = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Google search results container: a links inside h3 elements
        for a in soup.select("a"):
            href = a.get("href", "")
            # Google links in simple HTML search look like: /url?q=https://company.com/job...
            if href.startswith("/url?q="):
                real_url = href.split("/url?q=")[1].split("&")[0]
                real_url = urllib.parse.unquote(real_url)
                
                # Exclude internal google domains or support pages
                if "google.com" in real_url or "youtube.com" in real_url:
                    continue
                
                # Extract title from child h3 or the link text
                title_el = a.select_one("h3")
                title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
                
                if not title or len(title) < 10:
                    continue
                    
                # Clean up title
                for suffix in [" | ", " - "]:
                    if suffix in title:
                        title = title.split(suffix)[0].strip()
                
                # Generate a unique job_id
                job_id = f"google_{re.sub(r'[^a-z0-9]', '_', real_url[-40:].lower())}"
                
                # Fetch snippet from search description
                parent = a.find_parent("div")
                description = ""
                if parent:
                    # Look for child span or div with snippet text
                    for sibling in parent.find_next_siblings():
                        sib_text = sibling.get_text(strip=True)
                        if len(sib_text) > 40:
                            description = sib_text
                            break
                            
                job = {
                    "job_id": job_id,
                    "title": title,
                    "company": "Google Search Match",
                    "location": "India",
                    "url": real_url,
                    "source": "google",
                    "description": description or f"Job listing found on Google Search for: {keywords}",
                    "posted_at": "",
                }
                
                if _insert_job(conn, job, user_id=user_id):
                    new_jobs.append(job)
                    
    except Exception as e:
        print(f"  [Google Search] {e}")
    print(f"  Google Search: {len(new_jobs)} new jobs")
    return new_jobs


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
    all_new = []
    print(f"\n[Scraping job listings for user {user_id}...]")

    # Query user's designation to use as query keywords
    row = conn.execute("SELECT designation FROM users WHERE id = ?", (user_id,)).fetchone()
    keywords = row[0] if row and row[0] else "Product Manager"

    all_new += scrape_naukri(conn, user_id=user_id, keywords=keywords)
    all_new += scrape_linkedin_jobs(conn, user_id=user_id, keywords=keywords)
    all_new += scrape_google_search_jobs(conn, user_id=user_id, keywords=keywords)
    all_new += scrape_company_pages(conn, user_id=user_id)

    conn.close()
    print(f"  Total new jobs this run: {len(all_new)}\n")
    return all_new


if __name__ == "__main__":
    run_all_scrapers()
