"""
email_scraper.py — Scans email inbox (via IMAP) for job application updates.
Matches emails to companies in the SQLite database and updates their status.
"""

import os
import re
import imaplib
import email
import sqlite3
from datetime import datetime, date, timedelta
from email.header import decode_header
from db import get_conn, add_timeline, DB_PATH, get_user_secrets

from dotenv import load_dotenv
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(base_dir, ".env"))

# Configuration
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_EMAIL = os.getenv("IMAP_EMAIL", os.getenv("SENDER_EMAIL", ""))
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", os.getenv("SENDER_PASSWORD", ""))

# Keywords for status determination
REJECTED_KEYWORDS = [
    r"\bunfortunately\b",
    r"not moving forward",
    r"thank you for your interest",
    r"pursue other candidates",
    r"decided to go with",
    r"not selected",
    r"position has been filled"
]

INTERVIEW_KEYWORDS = [
    r"\binterview\b",
    r"\bschedule\b",
    r"\bdiscussion\b",
    r"\bchat\b",
    r"\bmeeting\b",
    r"\bcall\b",
    r"availability for"
]

APPLIED_KEYWORDS = [
    r"application received",
    r"thank you for applying",
    r"successfully submitted",
    r"confirm your application",
    r"application submitted",
    r"application.*was sent",
    r"we've sent your application",
    r"you applied to"
]

def clean_text(text: str) -> str:
    """Helper to clean string encoding issues."""
    return re.sub(r'\s+', ' ', text).strip()

def decode_mime_words(s: str) -> str:
    """Decode email header strings."""
    if not s:
        return ""
    try:
        parts = decode_header(s)
        decoded = []
        for word, encoding in parts:
            if isinstance(word, bytes):
                decoded.append(word.decode(encoding or "utf-8", errors="ignore"))
            else:
                decoded.append(str(word))
        return "".join(decoded)
    except Exception:
        return str(s)

def parse_email_body(msg) -> str:
    """Extract plain text body from email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disp = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disp:
                try:
                    body += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            pass
    return body

def determine_status_from_content(subject: str, body: str) -> str | None:
    """Analyze subject and body to determine updated job status."""
    content = f"{subject} {body}".lower()
    
    # Check for rejection first (highest priority)
    for kw in REJECTED_KEYWORDS:
        if re.search(kw, content):
            return "rejected"
            
    # Check for interviews
    for kw in INTERVIEW_KEYWORDS:
        if re.search(kw, content):
            return "interviewing"
            
    # Check for application confirmation
    for kw in APPLIED_KEYWORDS:
        if re.search(kw, content):
            return "applied"
            
    return None

def extract_job_info_from_email(subject: str, body: str, sender: str) -> tuple[str | None, str]:
    import re
    subject_clean = subject.replace('\n', ' ').strip()
    sender_clean = sender.lower()
    
    company = None
    title = "Product Manager"
    
    # Heuristics for subject:
    # 1. "Your application to [Company] was sent" or "your application to [Company]"
    m1 = re.search(r"your application to\s+([A-Za-z0-9\s\.\,\-\&\'\"]+?)(?:\s+was\s+sent|\s+for|\s*$|\.|\,)", subject_clean, re.IGNORECASE)
    if m1:
        company = m1.group(1).strip()
        
    # 2. "your application for [Title] at/to [Company] was sent" or "application for [Title] at/to [Company]"
    m2 = re.search(r"application for\s+([A-Za-z0-9\s\.\,\-\&\'\"]+?)\s+(?:at|to)\s+([A-Za-z0-9\s\.\,\-\&\'\"]+?)(?:\s+was\s+sent|\s*$|\.|\,)", subject_clean, re.IGNORECASE)
    if m2:
        title = m2.group(1).strip()
        company = m2.group(2).strip()
        
    # 3. "We've sent your application for [Title] to [Company]"
    m3 = re.search(r"we've sent your application for\s+([A-Za-z0-9\s\.\,\-\&\'\"]+?)\s+to\s+([A-Za-z0-9\s\.\,\-\&\'\"]+?)(?:\s*$|\.|\,)", subject_clean, re.IGNORECASE)
    if m3:
        title = m3.group(1).strip()
        company = m3.group(2).strip()
        
    # 4. "You applied to [Company]"
    m4 = re.search(r"you applied to\s+([A-Za-z0-9\s\.\,\-\&\'\"]+?)(?:\s*$|\.|\,)", subject_clean, re.IGNORECASE)
    if m4 and not company:
        company = m4.group(1).strip()
        
    # 5. "Thank you for applying to [Company]"
    m5 = re.search(r"thank you for applying to\s+([A-Za-z0-9\s\.\,\-\&\'\"]+?)(?:\s+for|\s*$|\.|\,)", subject_clean, re.IGNORECASE)
    if m5 and not company:
        company = m5.group(1).strip()

    # Extract company from sender domain if possible (e.g. recruitment@company.com)
    if not company and "@" in sender_clean:
        domain = sender_clean.split("@")[1].split(".")[0]
        if domain not in ("gmail", "yahoo", "outlook", "linkedin", "indeed", "greenhouse", "lever", "workday", "instahyre"):
            company = domain.capitalize()
            
    # Clean suffix like Pvt/Inc/Ltd
    if company:
        if " for " in company.lower():
            company = re.split(r"\s+for\s+", company, flags=re.IGNORECASE)[0]
        company = re.sub(r"\s+(inc|ltd|pvt|gmbh|co|corporation|llc)\b.*", "", company, flags=re.IGNORECASE).strip()
        company = company.strip('\'"., ')
        
    return company, title

def sync_job_statuses_from_email(db_path: str = DB_PATH, user_id: int | None = None) -> int:
    """Connect to IMAP and synchronize statuses in the database."""
    # An unscoped run is the operator's own (CLI) run: only touch the admin's jobs,
    # never match the operator's inbox against other users' boards.
    if user_id is None:
        user_id = 1
    imap_server = IMAP_SERVER
    # The IMAP_* / SENDER_* env credentials belong to the server operator, so only the
    # admin account (or an unscoped CLI run) may fall back to them. Every other user
    # syncs only the inbox they connected themselves.
    if user_id is None or user_id == 1:
        imap_email = IMAP_EMAIL
        imap_password = IMAP_PASSWORD
    else:
        imap_email = ""
        imap_password = ""

    if user_id is not None:
        conn = get_conn(db_path)
        creds = get_user_secrets(conn, user_id)
        conn.close()
        if creds["imap_email"] and creds["imap_password"]:
            imap_email = creds["imap_email"]
            imap_password = creds["imap_password"]

    if not imap_email or not imap_password:
        prefix = f"[Email Sync (User {user_id})]" if user_id else "[Email Sync]"
        print(f"{prefix} Skipping: credentials not configured.")
        return 0
        
    print(f"\n[Email Sync] Connecting to {imap_server} as {imap_email}...")
    try:
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(imap_email, imap_password)
        # Select All Mail to scan all folders/categories (Promotions, Updates, etc.) in Gmail
        try:
            status, _ = mail.select('"[Gmail]/All Mail"', readonly=True)
            if status != "OK":
                mail.select("inbox")
        except Exception:
            mail.select("inbox")
    except Exception as e:
        print(f"[Email Sync] Connection failed: {e}")
        return 0

    # Search for emails from the last 7 days
    date_since = (date.today() - timedelta(days=7)).strftime("%d-%b-%Y")
    status, data = mail.search(None, f'(SINCE "{date_since}")')
    
    if status != "OK" or not data[0]:
        print("[Email Sync] No recent emails found.")
        mail.logout()
        return 0
        
    email_ids = data[0].split()
    print(f"[Email Sync] Scanning {len(email_ids)} recent emails...")

    # Load tracked companies from our SQLite DB
    conn = get_conn(db_path)
    if user_id is not None:
        tracked_jobs = conn.execute(
            "SELECT job_id, company, title, status, user_id FROM jobs WHERE user_id = ? AND status NOT IN ('archived', 'rejected', 'offer')",
            (user_id,)
        ).fetchall()
    else:
        tracked_jobs = conn.execute(
            "SELECT job_id, company, title, status, user_id FROM jobs WHERE status NOT IN ('archived', 'rejected', 'offer')"
        ).fetchall()
    
    if not tracked_jobs:
        print("[Email Sync] No active jobs in database to match.")
        conn.close()
        mail.logout()
        return 0

    updates_count = 0
    
    # Process email IDs from newest to oldest
    for e_id in reversed(email_ids):
        res, msg_data = mail.fetch(e_id, "(RFC822)")
        if res != "OK":
            continue
            
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        
        subject = decode_mime_words(msg.get("Subject", ""))
        sender = decode_mime_words(msg.get("From", ""))
        body = parse_email_body(msg)
        
        combined_text = f"{sender} {subject} {body}".lower()
        sender_lower = sender.lower()
        
        # Allowed recruitment platforms/ATS domains
        ATS_DOMAINS = [
            "linkedin.com", "indeed.com", "greenhouse.io", "lever.co", 
            "workday", "instahyre.com", "myworkdayjobs.com", "workdayjobs.com"
        ]
        is_recruitment_source = any(platform in sender_lower for platform in ATS_DOMAINS)
        
        # Match against our tracked jobs/companies
        matched = False
        for job_id, company, title, current_status, job_user_id in tracked_jobs:
            company_clean = company.lower().strip()
            
            # Check if email is from the company's direct domain
            is_company_sender = (f"@{company_clean}." in sender_lower or 
                                 f"@{company_clean}jobs." in sender_lower or 
                                 company_clean in sender_lower)
                                 
            # Process only if from a recruiting platform referencing the company, or directly from the company
            if (is_recruitment_source and company_clean in combined_text) or is_company_sender:
                new_status = determine_status_from_content(subject, body)
                
                # Update status if we found a match and it is a progression or rejection
                if new_status and new_status != current_status:
                    # Let's verify we don't accidentally downgrade status (e.g. applied -> interviewing is fine, but interviewing -> applied is not)
                    status_hierarchy = {"new": 0, "scored": 0, "shortlisted": 1, "applied": 2, "interviewing": 3, "rejected": 4, "offer": 5}
                    
                    if status_hierarchy.get(new_status, 0) > status_hierarchy.get(current_status, 0) or new_status == "rejected":
                        print(f"  [Match!] {company} ({title}): status '{current_status}' → '{new_status}'")
                        
                        conn.execute(
                            "UPDATE jobs SET status = ? WHERE job_id = ? AND user_id = ?",
                            (new_status, job_id, job_user_id)
                        )
                        conn.execute(
                            """INSERT INTO received_emails (job_id, sender, subject, body, received_at, user_id)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (job_id, sender, subject, body, datetime.now().isoformat(), job_user_id)
                        )
                        add_timeline(
                            conn, 
                            job_id, 
                            f"Email update: {sender} | Subject: {subject[:40]}... → Status: {new_status}"
                        )
                        conn.commit()
                        updates_count += 1
                        matched = True
                        break
                        
        # Auto-discover fallback if email indicates application confirmation but no matched tracked job
        if not matched and is_recruitment_source:
            new_status = determine_status_from_content(subject, body)
            if new_status == "applied":
                extracted_company, extracted_title = extract_job_info_from_email(subject, body, sender)
                if extracted_company:
                    # Check if already tracked in the database to prevent duplicate creation
                    if user_id is not None:
                        existing_job = conn.execute(
                            "SELECT job_id FROM jobs WHERE user_id = ? AND LOWER(company) = ? AND LOWER(title) = ?",
                            (user_id, extracted_company.lower(), extracted_title.lower())
                        ).fetchone()
                    else:
                        existing_job = conn.execute(
                            "SELECT job_id FROM jobs WHERE LOWER(company) = ? AND LOWER(title) = ?",
                            (extracted_company.lower(), extracted_title.lower())
                        ).fetchone()
                    
                    if not existing_job:
                        import uuid
                        import random
                        job_id = f"auto_{extracted_company.lower().replace(' ', '_')}_{str(uuid.uuid4())[:8]}_u{user_id}"
                        auto_score = None  # not scored: there's no job description to score against
                        
                        print(f"  [Auto-Discover!] Creating new applied job: {extracted_company} - {extracted_title}")
                        conn.execute(
                            """INSERT INTO jobs (job_id, company, title, status, url, location, description, scraped_at, ai_score, user_id)
                               VALUES (?, ?, ?, 'applied', '', 'Remote', 'Automatically discovered via email application confirmation.', ?, ?, ?)""",
                            (job_id, extracted_company, extracted_title, datetime.now().isoformat(), auto_score, user_id or 1)
                        )
                        conn.execute(
                            """INSERT INTO received_emails (job_id, sender, subject, body, received_at, user_id)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (job_id, sender, subject, body, datetime.now().isoformat(), user_id or 1)
                        )
                        add_timeline(
                            conn,
                            job_id,
                            f"Discovered application confirmation email from {sender}"
                        )
                        conn.commit()
                        updates_count += 1
                        
                        # Update our local tracked list reference to prevent duplicate triggers
                        tracked_jobs.append((job_id, extracted_company, extracted_title, 'applied', user_id or 1))

    conn.close()
    try:
        mail.close()
        mail.logout()
    except Exception:
        pass
        
    print(f"[Email Sync] Done. Updated {updates_count} job status(es).\n")
    return updates_count

if __name__ == "__main__":
    sync_job_statuses_from_email()
