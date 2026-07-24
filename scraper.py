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

# ── Demo seed data — shown when all scraping fails ─────────────────────────
DEMO_JOBS = [
    {
        "job_id": "demo_razorpay_spm_001",
        "title": "Senior Product Manager — Checkout",
        "company": "Razorpay",
        "location": "Bengaluru",
        "url": "https://razorpay.com/jobs/",
        "description": (
            "Lead the checkout product for India's largest payment gateway. "
            "You will own the end-to-end checkout experience for 10M+ merchants. "
            "Requirements: 4+ years PM experience, strong analytical skills, "
            "experience with payments or fintech preferred. "
            "You will work closely with engineering, design, and business teams "
            "to define the product vision and roadmap. "
            "Responsibilities include defining success metrics, running A/B tests, "
            "and shipping features that improve conversion rates."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_cred_pm_002",
        "title": "Product Manager — Rewards & Loyalty",
        "company": "CRED",
        "location": "Bengaluru",
        "url": "https://careers.cred.club/",
        "description": (
            "Own the rewards and loyalty platform at CRED, serving 12M+ premium users. "
            "3-5 years experience required. Strong design sense and data-driven mindset. "
            "You will partner with brand, marketing, and engineering to build engaging "
            "reward experiences. Experience in consumer apps is a strong plus. "
            "Responsibilities: product strategy, OKR setting, stakeholder management, "
            "and cross-functional execution."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_zepto_pm_003",
        "title": "Product Manager — Supply Chain",
        "company": "Zepto",
        "location": "Mumbai",
        "url": "https://www.zepto.team/careers",
        "description": (
            "Drive the supply chain and dark store operations product at Zepto. "
            "2-4 years PM experience, ideally in logistics, ops-tech, or marketplace. "
            "You will work on inventory management, demand forecasting, and last-mile tools. "
            "Strong SQL skills required. Experience with operations-heavy products preferred. "
            "Collaborate with city ops, category, and tech teams to build India's "
            "fastest grocery delivery platform."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_groww_pm_004",
        "title": "Associate Product Manager — Mutual Funds",
        "company": "Groww",
        "location": "Bengaluru",
        "url": "https://groww.in/careers",
        "description": (
            "Build investment products for first-time investors on Groww. "
            "0-2 years experience, APM or fresh MBA welcome. Strong problem-solving "
            "and communication skills essential. You will work on the mutual funds "
            "discovery and investment flow. Responsibilities include user research, "
            "wireframing with design, and working with engineering on delivery. "
            "Familiarity with financial products is a strong plus."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_phonepe_gpm_005",
        "title": "Group Product Manager — UPI Payments",
        "company": "PhonePe",
        "location": "Bengaluru",
        "url": "https://www.phonepe.com/careers/",
        "description": (
            "Lead a team of PMs on PhonePe's core UPI payments product, "
            "used by 500M+ Indians. 7+ years experience required with at least "
            "2 years managing a PM team. Deep understanding of payments ecosystem "
            "and regulatory environment (RBI, NPCI) preferred. "
            "You will set product vision, manage a roadmap across multiple squads, "
            "and represent product in leadership reviews. Strong execution track record essential."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_meesho_pm_006",
        "title": "Product Manager — Seller Experience",
        "company": "Meesho",
        "location": "Bengaluru",
        "url": "https://meesho.io/careers",
        "description": (
            "Own the seller onboarding and catalogue management experience at Meesho. "
            "3-5 years PM experience, ideally in marketplace or e-commerce. "
            "You will work on tools that help 1.5M+ sellers list, price, and sell products. "
            "Strong analytical skills and experience with large-scale consumer products. "
            "Comfort with SQL, Mixpanel or similar analytics tools required."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_swiggy_pm_007",
        "title": "Senior PM — Consumer App (Food)",
        "company": "Swiggy",
        "location": "Bengaluru",
        "url": "https://careers.swiggy.com/",
        "description": (
            "Drive discovery and personalisation on Swiggy's consumer app. "
            "4-6 years experience in consumer product management. "
            "Experience with recommendation systems or personalisation is a big plus. "
            "You will own the home feed, search, and restaurant discovery experience. "
            "Strong data instincts, ability to run rapid experiments, and comfort "
            "working with ML teams required."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_browserstack_pm_008",
        "title": "Product Manager — Developer Tools",
        "company": "BrowserStack",
        "location": "Mumbai / Remote",
        "url": "https://www.browserstack.com/careers",
        "description": (
            "Build developer-first testing tools used by 50,000+ companies globally. "
            "3-5 years PM experience, ideally in B2B SaaS or developer tools. "
            "Strong technical background preferred — you will work closely with engineers "
            "on API design, SDKs, and CI/CD integrations. "
            "Excellent written communication for external product documentation. "
            "Experience with agile delivery and working with distributed global teams."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_stripe_mle_009",
        "title": "Machine Learning Engineer — Risk & Fraud",
        "company": "Stripe",
        "location": "San Francisco, CA / Remote",
        "url": "https://stripe.com/jobs",
        "description": (
            "Build and deploy real-time fraud detection and risk models. "
            "Scale Stripe's transaction scoring pipeline processing billions of dollars daily. "
            "Requirements: 3+ years experience with PyTorch/TensorFlow, Python, Spark, and MLOps platforms. "
            "Experience with streaming architectures using Kafka/Flink is a strong plus. "
            "You will optimize ML model inference latency to run under 50ms at consumer-scale throughput."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_pinterest_mle_010",
        "title": "Machine Learning Engineer — Recommendation Systems",
        "company": "Pinterest",
        "location": "San Francisco, CA",
        "url": "https://careers.pinterest.com",
        "description": (
            "Develop candidate-generation and ranking models for home feed recommendations. "
            "Work on large-scale recommendation systems, NLP text classifiers (using BERT/Transformers), "
            "and A/B experimentation frameworks. "
            "Skills required: Python, PyTorch, SQL, Docker, and TensorFlow Serving. "
            "You will deploy deep learning models serving 100M+ active users."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
    {
        "job_id": "demo_adobe_ds_011",
        "title": "Data Scientist / ML Developer",
        "company": "Adobe",
        "location": "San Jose, CA",
        "url": "https://adobe.com/careers",
        "description": (
            "Build subscription churn prediction and marketing analytics models. "
            "Work on ML pipelines utilizing gradient-boosted trees, Python, SQL, and pandas. "
            "Automate data workflows, design KPI dashboards, and present findings to leadership. "
            "Experience in statistics, experimental design, and predictive modeling required."
        ),
        "source": "demo",
        "posted_at": datetime.now().strftime("%Y-%m-%d"),
    },
]


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


def scrape_naukri(conn, max_pages: int = 2, user_id: int = 1) -> list[dict]:
    new_jobs = []
    for page in range(1, max_pages + 1):
        url = (
            f"https://www.naukri.com/product-manager-jobs-in-india-{page}"
            if page > 1 else
            "https://www.naukri.com/product-manager-jobs-in-india"
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
                if not any(k in title.lower() for k in ["product manager", "product management"]):
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


def scrape_linkedin_jobs(conn, user_id: int = 1) -> list[dict]:
    url = (
        "https://www.linkedin.com/jobs/search?"
        "keywords=Product+Manager&location=India&f_TPR=r86400&position=1&pageNum=0"
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
            if "product manager" not in title.lower():
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
    """Load demo jobs so the app works with no internet / no keys."""
    new_jobs = []
    for job in DEMO_JOBS:
        if _insert_job(conn, job, user_id=user_id):
            new_jobs.append(job)
    print(f"  Demo seed: {len(new_jobs)} jobs loaded")
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

    # Always seed demo data first so app has something to show
    demo = seed_demo_jobs(conn, user_id=user_id)
    all_new += demo

    # Then try live sources — failures are silent, demo data is the fallback
    all_new += scrape_naukri(conn, user_id=user_id)
    all_new += scrape_linkedin_jobs(conn, user_id=user_id)
    all_new += scrape_company_pages(conn, user_id=user_id)

    conn.close()
    print(f"  Total new jobs this run: {len(all_new)}\n")
    return all_new


if __name__ == "__main__":
    run_all_scrapers()
