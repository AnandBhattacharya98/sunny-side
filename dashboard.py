import os, json, sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from db import get_conn, add_timeline, DB_PATH, init_db
from ai_engine import generate_cover_letter, generate_linkedin_note, score_job
from notifier import send_email_digest
from auth import signup_user, login_user

app = Flask(__name__, template_folder='.')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "pm_job_hunter_super_secret_key_123")

# Set session cookies lifetime to be long so login stays active
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=30)

def get_user_id() -> int:
    return session.get("user_id", 1)

def get_user_settings(conn, user_id):
    row = conn.execute("SELECT resume_text, imap_email, imap_password, gemini_api_key, linkedin_profile, name, designation, share_profile FROM users WHERE id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    return {}

@app.before_request
def require_login():
    allowed_endpoints = ["login", "signup", "static", "index"]
    if not session.get("user_id"):
        if request.endpoint and request.endpoint not in allowed_endpoints:
            return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        conn = get_conn(DB_PATH)
        user = login_user(conn, username, password)
        conn.close()
        if user:
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        return render_template("dashboard.html", view_mode="login", error="Invalid username or password")
    return render_template("dashboard.html", view_mode="login")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        name = request.form.get("name", "").strip() or username.capitalize()
        designation = request.form.get("designation", "").strip() or "Product Seeker"
        share_profile = 1 if request.form.get("share_profile") else 0
        linkedin_profile = request.form.get("linkedin_profile", "").strip()
        imap_email = request.form.get("imap_email", "").strip()
        imap_password = request.form.get("imap_password", "").strip()
        gemini_api_key = request.form.get("gemini_api_key", "").strip()
        
        # Check for uploaded resume file
        resume_text = ""
        file = request.files.get("resume_file")
        if file and file.filename:
            filename = file.filename.lower()
            if filename.endswith(".txt"):
                try:
                    resume_text = file.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
            elif filename.endswith(".pdf"):
                import pypdf
                try:
                    reader = pypdf.PdfReader(file)
                    resume_text = "\n".join([page.extract_text() or "" for page in reader.pages])
                except Exception as e:
                    print(f"Error parsing PDF: {e}")
                    resume_text = ""
        
        if not resume_text:
            resume_text = request.form.get("resume_text", "").strip()

        conn = get_conn(DB_PATH)
        try:
            uid = signup_user(conn, username, password)
            conn.execute(
                """UPDATE users SET resume_text = ?, imap_email = ?, imap_password = ?, gemini_api_key = ?, linkedin_profile = ?,
                   name = ?, designation = ?, share_profile = ?
                   WHERE id = ?""",
                (resume_text, imap_email, imap_password, gemini_api_key, linkedin_profile, name, designation, share_profile, uid)
            )
            conn.commit()
            session.permanent = True
            session["user_id"] = uid
            session["username"] = username
            conn.close()
            return redirect(url_for("index"))
        except Exception as e:
            conn.close()
            return render_template("dashboard.html", view_mode="signup", error=str(e))
    return render_template("dashboard.html", view_mode="signup")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/api/profile", methods=["POST"])
def update_profile():
    d = request.json
    uid = get_user_id()
    conn = get_conn(DB_PATH)
    conn.execute(
        """UPDATE users SET resume_text = ?, imap_email = ?, imap_password = ?, gemini_api_key = ?, linkedin_profile = ?,
           name = ?, designation = ?, share_profile = ?
           WHERE id = ?""",
        (d.get("resume_text", ""), d.get("imap_email", ""), d.get("imap_password", ""), d.get("gemini_api_key", ""), d.get("linkedin_profile", ""),
         d.get("name", ""), d.get("designation", ""), d.get("share_profile", 0), uid)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/profile/upload", methods=["POST"])
def upload_profile_resume():
    uid = get_user_id()
    file = request.files.get("resume_file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
        
    filename = file.filename.lower()
    resume_text = ""
    if filename.endswith(".txt"):
        resume_text = file.read().decode("utf-8", errors="ignore")
    elif filename.endswith(".pdf"):
        import pypdf
        try:
            reader = pypdf.PdfReader(file)
            resume_text = "\n".join([page.extract_text() or "" for page in reader.pages])
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error parsing PDF: {str(e)}"}), 400
    else:
        return jsonify({"ok": False, "error": "Unsupported file format. Please upload PDF or TXT."}), 400
        
    conn = get_conn(DB_PATH)
    conn.execute("UPDATE users SET resume_text = ? WHERE id = ?", (resume_text, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "resume_text": resume_text})


# ── Helpers ────────────────────────────────────────────────────────────────

def _stats(conn):
    uid = get_user_id()
    total = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (uid,)).fetchone()[0]
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM jobs WHERE user_id = ? GROUP BY status", (uid,)).fetchall())
    avg = conn.execute("SELECT AVG(ai_score) FROM jobs WHERE user_id = ? AND ai_score IS NOT NULL", (uid,)).fetchone()[0]
    email_count = conn.execute("SELECT COUNT(*) FROM received_emails WHERE user_id = ?", (uid,)).fetchone()[0]
    
    last_updated_row = conn.execute("SELECT MAX(scraped_at) FROM jobs WHERE user_id = ?", (uid,)).fetchone()
    last_updated = last_updated_row[0] if last_updated_row and last_updated_row[0] else None
    if last_updated:
        try:
            dt = datetime.fromisoformat(last_updated.split('.')[0])
            last_updated_str = dt.strftime("%b %d, %I:%M %p")
        except Exception:
            last_updated_str = last_updated
    else:
        last_updated_str = "Never"
        
    return {"total": total, "by_status": by_status,
            "avg_score": round(avg, 1) if avg else 0,
            "last_updated": last_updated_str,
            "email_count": email_count}


def _full_job(conn, job_id):
    uid = get_user_id()
    j = conn.execute("SELECT * FROM jobs WHERE job_id=? AND user_id=?", (job_id, uid)).fetchone()
    if not j:
        return None
    j = dict(j)
    j["key_reqs_list"] = json.loads(j.get("key_reqs") or "[]")
    cl = conn.execute("SELECT * FROM cover_letters WHERE job_id=? AND user_id=?", (job_id, uid)).fetchone()
    j["cover_letter"] = dict(cl) if cl else None
    contacts = conn.execute("SELECT * FROM contacts WHERE job_id=? AND user_id=?", (job_id, uid)).fetchall()
    j["contacts"] = [dict(c) for c in contacts]
    notes = conn.execute("SELECT * FROM application_notes WHERE job_id=? AND user_id=?", (job_id, uid)).fetchone()
    j["notes"] = dict(notes) if notes else {"note": "", "linkedin_note": ""}
    timeline = conn.execute(
        "SELECT event, created_at FROM application_timeline WHERE job_id=? AND user_id=? ORDER BY created_at",
        (job_id, uid)
    ).fetchall()
    j["timeline"] = [dict(t) for t in timeline]
    
    emails = conn.execute(
        "SELECT * FROM received_emails WHERE job_id=? AND user_id=? ORDER BY received_at DESC",
        (job_id, uid)
    ).fetchall()
    j["emails"] = [dict(e) for e in emails]
    return j


# ── Pages ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = get_conn(DB_PATH)
    uid = session.get("user_id")
    if not uid:
        # Fetch consenting seekers for landing page wall (excluding admin)
        users = conn.execute(
            "SELECT name, designation, linkedin_profile FROM users WHERE share_profile = 1 AND username != 'admin' ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return render_template("landing.html", seekers=[dict(u) for u in users])
        
    # Fetch all active jobs (ignore archived)
    raw_jobs = conn.execute("SELECT * FROM jobs WHERE user_id = ? AND status != 'archived' ORDER BY COALESCE(ai_score,0) DESC", (uid,)).fetchall()
    
    board = {
        "whatsapp": [],
        "new": [],
        "shortlisted": [],
        "applied": [],
        "offer": [],
        "rejected": []
    }
    
    for row in raw_jobs:
        j = _full_job(conn, row["job_id"])
        if j:
            status = j["status"]
            if status == "whatsapp":
                board["whatsapp"].append(j)
            elif status in ("new", "scored", "ready"):
                board["new"].append(j)
            elif status in ("shortlisted", "interviewing"):
                board["shortlisted"].append(j)
            elif status == "applied":
                board["applied"].append(j)
            elif status == "offer":
                board["offer"].append(j)
            elif status == "rejected":
                board["rejected"].append(j)
            
    stats = _stats(conn)
    companies = [r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE user_id = ? ORDER BY company", (uid,)).fetchall()]
    
    # Get user settings to pass to frontend profile form
    settings = get_user_settings(conn, uid)
    needs_onboarding = not settings.get("resume_text")
    conn.close()
    
    cols = ["whatsapp", "new", "shortlisted", "applied", "offer", "rejected"]
    return render_template("dashboard.html", board=board, cols=cols, stats=stats,
                           companies=companies, settings=settings, view_mode="board",
                           needs_onboarding=needs_onboarding)


@app.route("/emails")
def view_emails():
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    raw_emails = conn.execute(
        """SELECT r.*, j.company, j.title 
           FROM received_emails r 
           LEFT JOIN jobs j ON r.job_id = j.job_id 
           WHERE r.user_id = ?
           ORDER BY r.received_at DESC""", (uid,)
    ).fetchall()
    emails = [dict(e) for e in raw_emails]
    stats = _stats(conn)
    conn.close()
    
    return render_template("dashboard.html", view_mode="emails", emails=emails, stats=stats)


@app.route("/pipeline")
def pipeline():
    return redirect("/")


@app.route("/users", methods=["GET", "POST"])
def admin_users():
    if session.get("username") != "admin":
        return redirect("/")
    
    conn = get_conn(DB_PATH)
    
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            error = "Both fields are required."
        else:
            existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                error = "Username already exists."
            else:
                from datetime import datetime
                from auth import hash_password
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, hash_password(password), datetime.now().isoformat())
                )
                conn.commit()
                
    # Fetch all users and calculate their job counts
    raw_users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    users = []
    for u in raw_users:
        u_dict = dict(u)
        job_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (u_dict["id"],)).fetchone()[0]
        u_dict["job_count"] = job_count
        users.append(u_dict)
        
    stats = _stats(conn)
    conn.close()
    return render_template("dashboard.html", view_mode="users", users=users, stats=stats, error=error)


@app.route("/api/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    if session.get("username") != "admin":
        return jsonify({"error": "unauthorized"}), 403
    if user_id == 1:
        return jsonify({"error": "cannot delete admin"}), 400
        
    conn = get_conn(DB_PATH)
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.execute("DELETE FROM jobs WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM contacts WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM cover_letters WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM application_notes WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM application_timeline WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM received_emails WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM tailored_resumes WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── API ────────────────────────────────────────────────────────────────────

@app.route("/api/job/<job_id>/status", methods=["POST"])
def set_status(job_id):
    status = request.json.get("status")
    allowed = {"whatsapp", "new", "scored", "shortlisted", "ready", "applied", "interviewing", "offer", "rejected", "archived"}
    if status not in allowed:
        return jsonify({"error": "invalid"}), 400
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    conn.execute("UPDATE jobs SET status=? WHERE job_id=? AND user_id=?", (status, job_id, uid))
    add_timeline(conn, job_id, f"Status → {status}")
    conn.commit(); conn.close()
    return jsonify({"ok": True, "status": status})


@app.route("/api/job/<job_id>/cover-letter", methods=["PUT"])
def save_cover_letter(job_id):
    d = request.json
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    conn.execute("UPDATE cover_letters SET subject=?, body=? WHERE job_id=? AND user_id=?",
                 (d["subject"], d["body"], job_id, uid))
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/note", methods=["POST"])
def save_note(job_id):
    d = request.json
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    conn.execute(
        "INSERT OR REPLACE INTO application_notes (job_id, note, linkedin_note, user_id) VALUES (?,?,?,?)",
        (job_id, d.get("note",""), d.get("linkedin_note",""), uid)
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/regenerate", methods=["POST"])
def regenerate(job_id):
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    job = conn.execute("SELECT * FROM jobs WHERE job_id=? AND user_id=?", (job_id, uid)).fetchone()
    if not job:
        conn.close(); return jsonify({"error": "not found"}), 404
    j = dict(job)
    contact = conn.execute(
        "SELECT name, title FROM contacts WHERE job_id=? AND user_id=? LIMIT 1", (job_id, uid)
    ).fetchone()
    cn = contact[0] if contact else "Hiring Team"
    ct = contact[1] if contact else "Recruiter"

    settings = get_user_settings(conn, uid)
    score_data = score_job(j["title"], j["company"], j.get("description",""), resume_text=settings.get("resume_text"), api_key=settings.get("gemini_api_key"))
    letter     = generate_cover_letter(j["title"], j["company"], j.get("description",""), cn, ct, resume_text=settings.get("resume_text"), api_key=settings.get("gemini_api_key"))
    li_note    = generate_linkedin_note(cn, ct, j["company"], j["title"], api_key=settings.get("gemini_api_key"))

    conn.execute("UPDATE jobs SET ai_score=?, ai_summary=?, key_reqs=? WHERE job_id=? AND user_id=?",
                 (score_data["score"], score_data["fit_summary"],
                  json.dumps(score_data.get("key_requirements",[])), job_id, uid))
    conn.execute(
        "INSERT OR REPLACE INTO cover_letters (job_id, subject, body, linkedin_note, created_at, user_id) VALUES (?,?,?,?,?,?)",
        (job_id, letter["subject"], letter["body"], li_note, datetime.now().isoformat(), uid)
    )
    add_timeline(conn, job_id, "Regenerated cover letter")
    conn.commit(); conn.close()
    return jsonify({"ok": True, "score": score_data["score"],
                    "fit_summary": score_data["fit_summary"],
                    "cover_letter": letter, "linkedin_note": li_note,
                    "key_requirements": score_data.get("key_requirements",[])})


@app.route("/api/job/<job_id>/send-email", methods=["POST"])
def send_email(job_id):
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    j_row = conn.execute("SELECT * FROM jobs WHERE job_id=? AND user_id=?", (job_id, uid)).fetchone()
    if not j_row:
        conn.close(); return jsonify({"error": "not found"}), 404
    j  = dict(j_row)
    cl = conn.execute("SELECT * FROM cover_letters WHERE job_id=? AND user_id=?", (job_id, uid)).fetchone()
    c  = conn.execute("SELECT * FROM contacts WHERE job_id=? AND user_id=? LIMIT 1", (job_id, uid)).fetchone()
    conn.close()
    if not cl:
        return jsonify({"error": "no cover letter"}), 400
    item = {**j, "score": j.get("ai_score",0),
            "fit_summary": j.get("ai_summary",""),
            "key_requirements": json.loads(j.get("key_reqs") or "[]"),
            "cover_letter_subject": cl["subject"],
            "cover_letter_body": cl["body"],
            "contact_name": c["name"] if c else "Hiring Team",
            "contact_title": c["title"] if c else "",
            "linkedin_note": cl.get("linkedin_note","") if cl else ""}
    ok = send_email_digest([item])
    if ok:
        conn2 = get_conn(DB_PATH)
        conn2.execute("UPDATE jobs SET status='applied' WHERE job_id=? AND user_id=?", (job_id, uid))
        add_timeline(conn2, job_id, "Email sent → applied")
        conn2.commit(); conn2.close()
    return jsonify({"ok": ok, "message": "Email sent!" if ok else "Add SENDER_EMAIL + SENDER_PASSWORD to .env"})


@app.route("/api/bulk", methods=["POST"])
def bulk_action():
    d      = request.json
    action = d.get("action")
    ids    = d.get("job_ids", [])
    conn   = get_conn(DB_PATH)
    uid    = get_user_id()

    if action in ("shortlist", "archive", "applied"):
        status_map = {"shortlist": "shortlisted", "archive": "archived", "applied": "applied"}
        for jid in ids:
            conn.execute("UPDATE jobs SET status=? WHERE job_id=? AND user_id=?", (status_map[action], jid, uid))
            add_timeline(conn, jid, f"Bulk → {status_map[action]}")

    elif action == "send-digest":
        items = []
        for jid in ids:
            j  = conn.execute("SELECT * FROM jobs WHERE job_id=? AND user_id=?", (jid, uid)).fetchone()
            cl = conn.execute("SELECT * FROM cover_letters WHERE job_id=? AND user_id=?", (jid, uid)).fetchone()
            c  = conn.execute("SELECT * FROM contacts WHERE job_id=? AND user_id=? LIMIT 1", (jid, uid)).fetchone()
            if j and cl:
                jd = dict(j)
                items.append({
                    **jd, "score": jd.get("ai_score",0),
                    "fit_summary": jd.get("ai_summary",""),
                    "key_requirements": json.loads(jd.get("key_reqs") or "[]"),
                    "cover_letter_subject": cl["subject"],
                    "cover_letter_body": cl["body"],
                    "contact_name": c["name"] if c else "Hiring Team",
                    "contact_title": c["title"] if c else "",
                    "linkedin_note": cl.get("linkedin_note","") if cl else "",
                })
        send_email_digest(items)

    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/pipeline/move", methods=["POST"])
def pipeline_move():
    d = request.json
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    conn.execute("UPDATE jobs SET status=? WHERE job_id=? AND user_id=?", (d["status"], d["job_id"], uid))
    add_timeline(conn, d["job_id"], f"Pipeline → {d['status']}")
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/stats")
def api_stats():
    conn = get_conn(DB_PATH)
    s = _stats(conn)
    conn.close()
    return jsonify(s)


@app.route("/api/refresh", methods=["POST"])
def refresh_listings():
    from scraper import run_all_scrapers
    from linkedin_finder import enrich_jobs_with_contacts
    from ai_engine import process_new_jobs
    
    try:
        uid = get_user_id()
        run_all_scrapers(DB_PATH, user_id=uid)
        
        conn = get_conn(DB_PATH)
        conn.execute("UPDATE jobs SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
        conn.commit()
        
        enrich_jobs_with_contacts(DB_PATH)
        conn.execute("UPDATE contacts SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
        conn.commit()
        
        # Sync application statuses from user's email
        from email_scraper import sync_job_statuses_from_email
        sync_job_statuses_from_email(DB_PATH, user_id=uid)
        conn.execute("UPDATE received_emails SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
        conn.commit()
        
        # Using default MIN_SCORE or float(os.getenv("MIN_SCORE", "6.0"))
        min_score = float(os.getenv("MIN_SCORE", "0.0")) # Score everything so we don't miss jobs in dashboard
        process_new_jobs(DB_PATH, min_score=min_score, user_id=uid)
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def scrape_job_url(url: str) -> dict:
    """Fetches a job URL and extracts company, title, description, and location."""
    import requests
    from bs4 import BeautifulSoup
    import re
    import json
    from scraper import HEADERS, _fetch_description
    from ai_engine import GEMINI_API_KEY, _call_gemini
    
    domain = url.split("://")[-1].split("/")[0].replace("www.", "")
    company = domain.split(".")[0].capitalize()
    title = "Product Manager"
    desc = "Forwarded via WhatsApp."
    loc = "India"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            fetched_desc = _fetch_description(url)
            if fetched_desc:
                desc = fetched_desc
            else:
                desc = soup.get_text(separator="\n", strip=True)[:2500]
                
            if GEMINI_API_KEY:
                page_text = soup.get_text(separator="\n", strip=True)[:4000]
                prompt = f"""
Analyze the following text content fetched from a job posting webpage ({url}).
Extract the:
- Company name (e.g. "Adobe", "Razorpay")
- Job Title (e.g. "Senior Product Manager", "Product Manager - Payments")
- Job Location (e.g. "Bengaluru", "Remote", "India")
- Brief job description (max 1000 characters)

Page Text:
{page_text}

Respond ONLY with a JSON object containing keys: "company", "title", "location", "description". No markdown wrapping or extra comments.
"""
                try:
                    res = _call_gemini(prompt)
                    clean_res = res.replace("```json", "").replace("```", "").strip()
                    parsed_data = json.loads(clean_res)
                    if parsed_data.get("company"):
                        company = parsed_data["company"].strip()
                    if parsed_data.get("title"):
                        title = parsed_data["title"].strip()
                    if parsed_data.get("location"):
                        loc = parsed_data["location"].strip()
                    if parsed_data.get("description"):
                        desc = parsed_data["description"].strip()
                except Exception as e:
                    print(f"[scrape_job_url] Gemini extraction failed: {e}")
            
            if not GEMINI_API_KEY or company == domain.split(".")[0].capitalize():
                # Fallback meta extraction
                og_title = soup.find("meta", property="og:title")
                title_text = og_title["content"] if og_title and og_title.get("content") else (soup.title.string if soup.title else "")
                if title_text:
                    title_text = title_text.strip()
                    if " hiring " in title_text:
                        parts = title_text.split(" hiring ")
                        company = parts[0].strip()
                        title = parts[1].split(" in ")[0].split("|")[0].split("-")[0].strip()
                    elif " at " in title_text:
                        parts = title_text.split(" at ")
                        title = parts[0].strip()
                        company = parts[1].split("|")[0].split("-")[0].strip()
                    elif " - " in title_text:
                        parts = title_text.split(" - ")
                        company = parts[0].strip()
                        title = parts[1].split("|")[0].strip()
                    else:
                        title = title_text.split("|")[0].split("-")[0].strip()
    except Exception as e:
        print(f"[scrape_job_url] Error: {e}")
        
    return {
        "company": company,
        "title": title,
        "url": url,
        "location": loc,
        "description": desc
    }


@app.route("/api/whatsapp/import", methods=["POST"])
def import_whatsapp():
    text = request.json.get("text", "")
    if not text:
        return jsonify({"ok": False, "error": "Empty text"}), 400
        
    prompt = f"""
Given the following raw chat message(s) shared on WhatsApp, extract all job listings mentioned.
For each job listing, extract:
- Company name (clean, standard name, e.g. "Swiggy" instead of "Swiggy link")
- Job title (clean, standard title, e.g. "Product Manager", "Software Engineer")
- Job URL / link (if present, else empty string)
- Brief description / context from the message (if present, else empty string)

Raw chat text:
{text}

Respond ONLY with a JSON array of objects with the keys: "company", "title", "url", "description". Do not include markdown wraps or any explanation.
"""
    import json
    parsed = []
    try:
        from ai_engine import GEMINI_API_KEY, _call_gemini
        if GEMINI_API_KEY:
            raw_response = _call_gemini(prompt)
            clean_response = raw_response.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean_response)
    except Exception as e:
        print(f"[WhatsApp Import] Gemini failed, falling back to regex: {e}")
        
    # Enrich by scraping URLs if present
    enriched_parsed = []
    for item in parsed:
        url = item.get("url", "").strip()
        if url:
            print(f"[WhatsApp Import] Enrichment scraping URL: {url}")
            scraped = scrape_job_url(url)
            item["company"] = scraped["company"]
            item["title"] = scraped["title"]
            item["description"] = scraped["description"]
            item["location"] = scraped.get("location", "Remote")
        enriched_parsed.append(item)
    parsed = enriched_parsed

    # Regex fallback if Gemini failed or is not active
    if not parsed:
        import re
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', text)
        for url in urls:
            print(f"[WhatsApp Import] Scraping fallback URL: {url}")
            scraped = scrape_job_url(url)
            parsed.append(scraped)
            
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    import uuid
    import random
    from ai_engine import score_job
    
    added_count = 0
    for item in parsed:
        company = item.get("company", "Unknown Company").strip()
        title = item.get("title", "Product Manager").strip()
        url = item.get("url", "").strip()
        desc = item.get("description", "Imported from WhatsApp.").strip()
        loc = item.get("location", "Remote").strip()
        
        if url:
            existing = conn.execute(
                "SELECT title, company, status FROM jobs WHERE url = ? AND user_id = ?", (url, uid)
            ).fetchone()
        if not existing:
            existing = conn.execute(
                "SELECT title, company, status FROM jobs WHERE LOWER(company) = ? AND LOWER(title) = ? AND user_id = ?",
                (company.lower(), title.lower(), uid)
            ).fetchone()
            
        if existing:
            status_map = {
                "whatsapp": "From WhatsApp",
                "new": "Inbox / New", "scored": "Inbox / New", "ready": "Inbox / New",
                "shortlisted": "Shortlisted / Active", "interviewing": "Shortlisted / Active",
                "applied": "Applied", "offer": "Offer", "rejected": "Rejected", "archived": "Archived"
            }
            lane = status_map.get(existing["status"], "Unknown")
            conn.close()
            return jsonify({
                "ok": False,
                "error": f"Duplicate found: '{existing['title']}' at {existing['company']} is already in the '{lane}' lane!"
            }), 409

        job_id = f"wa_{company.lower().replace(' ', '_')}_{str(uuid.uuid4())[:8]}"
        
        # Calculate real score if description and title are present
        try:
            settings = get_user_settings(conn, uid)
            score_data = score_job(title, company, desc, resume_text=settings.get("resume_text"), api_key=settings.get("gemini_api_key"))
            auto_score = score_data.get("score", 7.0)
            ai_summary = score_data.get("summary", "Imported from WhatsApp.")
            key_reqs = json.dumps(score_data.get("key_requirements", []))
        except Exception as e:
            print(f"[WhatsApp Import] Scoring failed: {e}")
            auto_score = round(random.uniform(7.5, 9.5), 1)
            ai_summary = desc
            key_reqs = "[]"
            
        conn.execute(
            """INSERT INTO jobs (job_id, company, title, status, url, location, description, scraped_at, ai_score, ai_summary, key_reqs, user_id)
               VALUES (?, ?, ?, 'whatsapp', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, company, title, url, loc, desc, datetime.now().isoformat(), auto_score, ai_summary, key_reqs, uid)
        )
        add_timeline(conn, job_id, "Imported from WhatsApp forward")
        added_count += 1
            
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "count": added_count})


@app.route("/api/job/<job_id>/resume", methods=["GET"])
def get_resume(job_id):
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    row = conn.execute("SELECT resume_content FROM tailored_resumes WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
    
    if row:
        resume = row["resume_content"]
    else:
        # Fetch job details to generate
        job = conn.execute("SELECT title, company, description FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if not job:
            conn.close()
            return jsonify({"ok": False, "error": "Job not found"}), 404
            
        from ai_engine import generate_tailored_resume
        settings = get_user_settings(conn, uid)
        resume = generate_tailored_resume(job["description"] or "", job["title"] or "", job["company"] or "", resume_text=settings.get("resume_text"))
        
        # Save to DB cache
        conn.execute(
            "INSERT OR REPLACE INTO tailored_resumes (job_id, resume_content, created_at, user_id) VALUES (?, ?, ?, ?)",
            (job_id, resume, datetime.now().isoformat(), uid)
        )
        conn.commit()
        
    conn.close()
    return jsonify({"ok": True, "resume": resume})


@app.route("/resume/<job_id>/print")
def print_resume(job_id):
    conn = get_conn(DB_PATH)
    uid = get_user_id()
    row = conn.execute("SELECT resume_content FROM tailored_resumes WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
    
    if not row:
        job = conn.execute("SELECT title, company, description FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if not job:
            conn.close()
            return "Job not found", 404
        from ai_engine import generate_tailored_resume
        settings = get_user_settings(conn, uid)
        resume = generate_tailored_resume(job["description"] or "", job["title"] or "", job["company"] or "", resume_text=settings.get("resume_text"))
        conn.execute(
            "INSERT OR REPLACE INTO tailored_resumes (job_id, resume_content, created_at, user_id) VALUES (?, ?, ?, ?)",
            (job_id, resume, datetime.now().isoformat(), uid)
        )
        conn.commit()
    else:
        resume = row["resume_content"]
    conn.close()
    
    # Preprocess markdown to ensure valid list blocks and header spacing
    lines = resume.splitlines()
    new_lines = []
    for i, line in enumerate(lines):
        clean = line.strip()
        if clean.startswith(("-", "*")):
            if i > 0 and lines[i-1].strip() and not lines[i-1].strip().startswith(("-", "*")):
                new_lines.append("")
        elif clean.startswith("**") and "float: right" in line:
            if i > 0 and lines[i-1].strip():
                new_lines.append("")
        new_lines.append(line)
    processed_resume = "\n".join(new_lines)
    
    import markdown
    html_content = markdown.markdown(processed_resume)
    
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Tailored Resume - {job_id}</title>
  <style>
    @page {{
      size: letter;
      margin: 0.6in 0.6in 0.5in 0.6in;
    }}
    body {{
      font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
      color: #000;
      line-height: 1.45;
      max-width: 800px;
      margin: 0 auto;
      background: #fff;
      font-size: 11px;
    }}
    h1 {{
      font-size: 22px;
      text-align: center;
      margin-top: 0;
      margin-bottom: 2px;
      font-weight: bold;
      color: #000;
      text-transform: none;
      letter-spacing: -0.2px;
    }}
    /* Style the contact information paragraph under the name */
    .resume-body > p:first-of-type {{
      text-align: center;
      font-size: 10px;
      margin-top: 0;
      margin-bottom: 20px;
      color: #333;
      word-spacing: 1px;
    }}
    h2 {{
      border-bottom: 1px solid #111;
      padding-bottom: 2px;
      margin-top: 20px;
      margin-bottom: 10px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      font-weight: bold;
      color: #000;
    }}
    p {{
      margin-top: 12px;
      margin-bottom: 4px;
      font-size: 11px;
    }}
    /* Tighten gap between title paragraph and company/context paragraph */
    .resume-body p + p {{
      margin-top: 2px;
      margin-bottom: 4px;
    }}
    ul {{
      padding-left: 18px;
      margin-top: 4px;
      margin-bottom: 12px;
    }}
    li {{
      margin-top: 2px;
      margin-bottom: 3px;
      font-size: 11px;
      line-height: 1.4;
    }}
    .print-btn-container {{
      text-align: center;
      margin-bottom: 20px;
      margin-top: 10px;
    }}
    .btn {{
      padding: 8px 16px;
      background: #5c3bcf;
      color: white;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      font-size: 12px;
      font-family: sans-serif;
      font-weight: 500;
      box-shadow: 0 1px 3px rgba(0,0,0,0.15);
    }}
    @media print {{
      body {{
        padding: 0;
        margin: 0;
      }}
      .print-btn-container {{
        display: none !important;
      }}
    }}
  </style>
</head>
<body>
  <div class="print-btn-container">
    <button onclick="window.print()" class="btn">🖨️ Save / Download as PDF</button>
  </div>
  <div class="resume-body">
    {html_content}
  </div>
</body>
</html>"""


def run_dashboard(port=5050):
    init_db(DB_PATH)
    print(f"\nDashboard → http://localhost:{port}")
    print("Press Ctrl+C to stop.\n")
    app.run(debug=False, port=port, use_reloader=False)


if __name__ == "__main__":
    run_dashboard()
