"""
linkedin_finder.py — Finds contacts at companies.

WITHOUT Proxycurl key: generates realistic placeholder contacts + LinkedIn search URLs.
WITH Proxycurl key:    live person lookup via Proxycurl API (~$0.03/company).

Set PROXYCURL_API_KEY in .env to enable live contact data.
"""

import os
import re
import time
import sqlite3
import requests
from datetime import datetime
from db import get_conn, DB_PATH

PROXYCURL_API_KEY = os.getenv("PROXYCURL_API_KEY", "")

# Known hiring contacts by company — crowd-sourced / public info
# These are placeholder titles to generate useful LinkedIn search URLs.
COMPANY_CONTACT_HINTS = {
    "Razorpay":      [("Talent Acquisition", "PM Hiring"), ("Head of Product", "Leadership")],
    "CRED":          [("Recruiter", "Tech Hiring"), ("VP Product", "Leadership")],
    "Zepto":         [("Talent Partner", "Product Hiring"), ("Product Lead", "Consumer")],
    "Groww":         [("HR Business Partner", "Tech"), ("Director Product", "Investments")],
    "PhonePe":       [("Senior Recruiter", "Product"), ("VP Product", "UPI")],
    "Meesho":        [("Talent Acquisition", "Product"), ("Senior PM", "Seller Platform")],
    "Swiggy":        [("Tech Recruiter", "Product"), ("Director Product", "Consumer")],
    "BrowserStack":  [("People & Culture", "Tech"), ("Head of Product", "Platform")],
    "default":       [("Talent Acquisition", "Tech Hiring"), ("Head of Product", "")],
}


def _linkedin_search_url(company: str, role: str) -> str:
    """Returns a pre-filled LinkedIn people search URL — click to find real contacts."""
    q = f'site:linkedin.com/in "{company}" "{role}"'
    return f"https://www.linkedin.com/search/results/people/?keywords={requests.utils.quote(company + ' ' + role)}&origin=GLOBAL_SEARCH_HEADER"


def _local_contacts(company: str) -> list[dict]:
    """
    Returns placeholder contacts with LinkedIn search URLs.
    No API key required — user can click to open the real LinkedIn search.
    """
    hints = COMPANY_CONTACT_HINTS.get(company, COMPANY_CONTACT_HINTS["default"])
    contacts = []
    for title, dept in hints:
        linkedin_search = _linkedin_search_url(company, title)
        contacts.append({
            "name": f"{title} @ {company}",
            "title": f"{title} ({dept})" if dept else title,
            "linkedin_url": linkedin_search,
            "email": "",
            "is_placeholder": True,
        })
    return contacts


def _proxycurl_contacts(company: str) -> list[dict]:
    """Live contact lookup via Proxycurl — requires PROXYCURL_API_KEY."""
    base = "https://nubela.co/proxycurl/api/v2"
    contacts = []

    # Step 1: find company LinkedIn URL
    company_url = ""
    try:
        r = requests.get(
            f"{base}/linkedin/company/search/",
            params={"company_name": company, "enrich_profiles": "skip"},
            headers={"Authorization": f"Bearer {PROXYCURL_API_KEY}"},
            timeout=10,
        )
        results = r.json().get("results", [])
        if results:
            company_url = results[0].get("linkedin_profile_url", "")
    except Exception as e:
        print(f"  [Proxycurl company] {e}")
        return _local_contacts(company)

    if not company_url:
        return _local_contacts(company)

    # Step 2: search employees
    for kw in ["product manager", "recruiter"]:
        try:
            r = requests.get(
                f"{base}/linkedin/company/employees/",
                params={
                    "linkedin_company_profile_url": company_url,
                    "keyword_regex": kw,
                    "page_size": 3,
                    "employment_status": "current",
                },
                headers={"Authorization": f"Bearer {PROXYCURL_API_KEY}"},
                timeout=12,
            )
            for p in r.json().get("employees", []):
                contacts.append({
                    "name": p.get("name", ""),
                    "title": p.get("title", ""),
                    "linkedin_url": p.get("profile_url", ""),
                    "email": "",
                    "is_placeholder": False,
                })
        except Exception as e:
            print(f"  [Proxycurl employees] {e}")

    return contacts if contacts else _local_contacts(company)


def find_contacts(company: str) -> list[dict]:
    if PROXYCURL_API_KEY:
        return _proxycurl_contacts(company)
    return _local_contacts(company)


def _store_contacts(conn, job_id: str, contacts: list[dict]) -> None:
    # Fetch user_id for this job
    row = conn.execute("SELECT user_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    uid = row[0] if row else 1
    
    # Clear old contacts for this job first
    conn.execute("DELETE FROM contacts WHERE job_id=? AND user_id=?", (job_id, uid))
    for c in contacts:
        conn.execute(
            """INSERT INTO contacts (job_id, name, title, linkedin_url, email, found_at, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job_id, c["name"], c["title"], c["linkedin_url"],
             c.get("email", ""), datetime.now().isoformat(), uid),
        )
    conn.commit()


def enrich_jobs_with_contacts(db_path: str = DB_PATH, user_id: int = None) -> None:
    conn = get_conn(db_path)
    if user_id is not None:
        jobs = conn.execute(
            "SELECT job_id, company FROM jobs WHERE status='new' AND user_id = ? LIMIT 30", (user_id,)
        ).fetchall()
    else:
        jobs = conn.execute(
            "SELECT job_id, company FROM jobs WHERE status='new' LIMIT 30"
        ).fetchall()

    mode = "Proxycurl (live)" if PROXYCURL_API_KEY else "local placeholders + LinkedIn search URLs"
    print(f"\n[Finding contacts — mode: {mode}]")

    seen = {}
    for job_id, company in jobs:
        if company in seen:
            # Reuse contacts found earlier for same company
            _store_contacts(conn, job_id, seen[company])
            continue
        contacts = find_contacts(company)
        seen[company] = contacts
        _store_contacts(conn, job_id, contacts)
        print(f"  {company}: {len(contacts)} contact(s)")
        if PROXYCURL_API_KEY:
            time.sleep(1)

    conn.close()


if __name__ == "__main__":
    enrich_jobs_with_contacts()
