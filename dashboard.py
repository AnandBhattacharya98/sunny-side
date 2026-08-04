import os, json, sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, session, url_for, send_file
from db import get_conn, add_timeline, DB_PATH, init_db
from ai_engine import generate_cover_letter, generate_linkedin_note, score_job
from notifier import send_email_digest
from auth import signup_user, login_user

app = Flask(__name__, template_folder='.')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "pm_job_hunter_super_secret_key_123")

# Initialize database on startup (crucial for Gunicorn/Render deployments)
init_db(DB_PATH)

# Set session cookies lifetime to be long so login stays active
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=30)

def get_user_id() -> int:
    return session.get("user_id", 1)

def get_user_settings(conn, user_id):
    row = conn.execute("SELECT resume_text, imap_email, imap_password, gemini_api_key, linkedin_profile, name, designation, share_profile, resume_filename, weight_thumbs_up, weight_applied, weight_thumbs_down, weight_rejected FROM users WHERE id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    return {}

@app.before_request
def require_login():
    allowed_endpoints = ["login", "signup", "static", "index", "auth_google", "auth_google_callback", "auth_linkedin", "auth_linkedin_callback", "auth_mock_callback", "serve_logo", "serve_favicon"]
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
        resume_filename = ""
        file = request.files.get("resume_file")
        if file and file.filename:
            resume_filename = file.filename
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
            elif filename.endswith(".docx"):
                import docx
                try:
                    doc = docx.Document(file)
                    resume_text = "\n".join([p.text for p in doc.paragraphs])
                except Exception as e:
                    print(f"Error parsing DOCX: {e}")
                    resume_text = ""
        
        if not resume_text:
            resume_text = request.form.get("resume_text", "").strip()

        from resume_parser import parse_resume
        profile_json = ""
        if resume_text:
            try:
                profile_json = json.dumps(parse_resume(resume_text, gemini_api_key))
            except Exception as e:
                print(f"Error parsing resume: {e}")

        conn = get_conn(DB_PATH)
        try:
            uid = signup_user(conn, username, password)
            conn.execute(
                """UPDATE users SET resume_text = ?, imap_email = ?, imap_password = ?, gemini_api_key = ?, linkedin_profile = ?,
                   name = ?, designation = ?, share_profile = ?, resume_filename = ?, resume_profile_json = ?
                   WHERE id = ?""",
                (resume_text, imap_email, imap_password, gemini_api_key, linkedin_profile, name, designation, share_profile, resume_filename, profile_json, uid)
            )
            conn.commit()
            
            # If resume is provided on signup, scrape live jobs matching user designation and score them
            if resume_text:
                from scraper import run_all_scrapers
                conn.commit()
                run_all_scrapers(DB_PATH, user_id=uid)
                from ai_engine import process_new_jobs
                process_new_jobs(DB_PATH, min_score=0, user_id=uid)

            session.permanent = True
            session["user_id"] = uid
            session["username"] = username
            session["is_new_user"] = True
            conn.close()
            return redirect(url_for("index"))
        except Exception as e:
            conn.close()
            return render_template("dashboard.html", view_mode="signup", error=str(e))
    return render_template("dashboard.html", view_mode="signup")

def handle_social_login(provider, email, name):
    username = email.split("@")[0] + "_" + provider
    conn = get_conn(DB_PATH)
    try:
        row = conn.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            uid = row[0]
            is_new = False
        else:
            import uuid
            password = str(uuid.uuid4())
            uid = signup_user(conn, username, password)
            conn.execute("UPDATE users SET name = ?, share_profile = 1 WHERE id = ?", (name, uid))
            conn.commit()
            is_new = True
            
        session.permanent = True
        session["user_id"] = uid
        session["username"] = username
        session["is_new_user"] = is_new
        conn.close()
        return redirect(url_for("index"))
    except Exception as e:
        conn.close()
        return f"Social authentication error: {str(e)}", 500


@app.route("/auth/google")
def auth_google():
    from dotenv import load_dotenv
    base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(dotenv_path=os.path.join(base_dir, ".env"), override=True)
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        return render_template("dashboard.html", view_mode="mock_auth", provider="google")
    redirect_uri = url_for("auth_google_callback", _external=True)
    google_auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={client_id}&"
        f"redirect_uri={redirect_uri}&"
        f"response_type=code&"
        f"scope=openid%20email%20profile"
    )
    return redirect(google_auth_url)


@app.route("/auth/google/callback")
def auth_google_callback():
    from dotenv import load_dotenv
    base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(dotenv_path=os.path.join(base_dir, ".env"), override=True)
    code = request.args.get("code")
    if not code:
        return "Authorization code missing", 400
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = url_for("auth_google_callback", _external=True)
    
    import requests
    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        }
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return f"Failed to retrieve access token: {token_data}", 400
        
    user_resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_info = user_resp.json()
    email = user_info.get("email")
    name = user_info.get("name", email.split("@")[0])
    
    if not email:
        return "Failed to retrieve email from Google profile", 400
        
    return handle_social_login("google", email, name)


@app.route("/auth/linkedin")
def auth_linkedin():
    from dotenv import load_dotenv
    base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(dotenv_path=os.path.join(base_dir, ".env"), override=True)
    client_id = os.getenv("LINKEDIN_CLIENT_ID")
    if not client_id:
        return render_template("dashboard.html", view_mode="mock_auth", provider="linkedin")
    redirect_uri = url_for("auth_linkedin_callback", _external=True)
    linkedin_auth_url = (
        f"https://www.linkedin.com/oauth/v2/authorization?"
        f"client_id={client_id}&"
        f"redirect_uri={redirect_uri}&"
        f"response_type=code&"
        f"scope=openid%20profile%20email"
    )
    return redirect(linkedin_auth_url)


@app.route("/auth/linkedin/callback")
def auth_linkedin_callback():
    from dotenv import load_dotenv
    base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(dotenv_path=os.path.join(base_dir, ".env"), override=True)
    code = request.args.get("code")
    if not code:
        return "Authorization code missing", 400
    client_id = os.getenv("LINKEDIN_CLIENT_ID")
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET")
    redirect_uri = url_for("auth_linkedin_callback", _external=True)
    
    import requests
    token_resp = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        }
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return f"Failed to retrieve access token: {token_data}", 400
        
    user_resp = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_info = user_resp.json()
    email = user_info.get("email")
    name = user_info.get("name") or (user_info.get("given_name", "") + " " + user_info.get("family_name", "")).strip() or email.split("@")[0]
    
    if not email:
        return "Failed to retrieve email from LinkedIn profile", 400
        
    return handle_social_login("linkedin", email, name)


@app.route("/auth/mock/callback", methods=["POST"])
def auth_mock_callback():
    provider = request.form.get("provider")
    email = request.form.get("email")
    name = request.form.get("name")
    
    if not email or not name:
        return "Missing email or name", 400
        
    return handle_social_login(provider, email, name)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/api/profile", methods=["POST"])
def update_profile():
    d = request.json
    uid = get_user_id()
    conn = get_conn(DB_PATH)
    
    # Fetch old values to check for changes
    old = conn.execute("SELECT resume_text, gemini_api_key, designation FROM users WHERE id = ?", (uid,)).fetchone()
    old_resume = old[0] if old else ""
    old_key = old[1] if old else ""
    old_designation = old[2] if old else ""
    
    new_resume = d.get("resume_text", "")
    new_key = d.get("gemini_api_key", "")
    new_designation = d.get("designation", "")
    
    should_reparse = (new_resume != old_resume) or (new_key != old_key)
    designation_changed = (new_designation != old_designation)
    
    profile_json = None
    if should_reparse:
        from resume_parser import parse_resume
        try:
            profile_json = json.dumps(parse_resume(new_resume, new_key))
        except Exception as e:
            print(f"Error parsing resume: {e}")
    else:
        profile_row = conn.execute("SELECT resume_profile_json FROM users WHERE id = ?", (uid,)).fetchone()
        profile_json = profile_row[0] if profile_row else None
        
    conn.execute(
        """UPDATE users SET resume_text = ?, imap_email = ?, imap_password = ?, gemini_api_key = ?, linkedin_profile = ?,
           name = ?, designation = ?, share_profile = ?, resume_filename = ?, resume_profile_json = ?,
           weight_thumbs_up = ?, weight_applied = ?, weight_thumbs_down = ?, weight_rejected = ?,
           daily_recs_enabled = ?, daily_recs_min_score = ?, daily_recs_time = ?
           WHERE id = ?""",
        (new_resume, d.get("imap_email", ""), d.get("imap_password", ""), new_key, d.get("linkedin_profile", ""),
         d.get("name", ""), d.get("designation", ""), d.get("share_profile", 0), d.get("resume_filename", ""), profile_json,
         float(d.get("weight_thumbs_up", 1.0)), float(d.get("weight_applied", 1.0)), float(d.get("weight_thumbs_down", -1.0)), float(d.get("weight_rejected", -1.5)),
         int(d.get("daily_recs_enabled", 1)), float(d.get("daily_recs_min_score", 7.5)), d.get("daily_recs_time", "07:30"), uid)
    )
    conn.commit()
    
    if designation_changed:
        conn.execute("DELETE FROM jobs WHERE user_id = ? AND status = 'new'", (uid,))
        conn.commit()
        
    if should_reparse:
        conn.execute(
            "UPDATE jobs SET status = 'new' WHERE user_id = ? AND status IN ('new', 'scored', 'ready')",
            (uid,)
        )
        conn.commit()
        from ai_engine import process_new_jobs
        process_new_jobs(DB_PATH, min_score=0, user_id=uid)
        
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/jobs/feedback", methods=["POST"])
def update_job_feedback():
    uid = get_user_id()
    d = request.json or {}
    job_id = d.get("job_id")
    feedback = d.get("feedback") # 1, -1, or 0
    
    if not job_id or feedback is None:
        return jsonify({"ok": False, "error": "Missing parameters"}), 400
        
    conn = get_conn(DB_PATH)
    conn.execute(
        "UPDATE jobs SET feedback = ? WHERE job_id = ? AND user_id = ?",
        (feedback, job_id, uid)
    )
    conn.commit()
    
    # Reset all inbox jobs (status 'new', 'scored', 'ready') to 'new' for re-evaluation
    conn.execute(
        "UPDATE jobs SET status = 'new' WHERE user_id = ? AND status IN ('new', 'scored', 'ready')",
        (uid,)
    )
    conn.commit()
    
    # Run re-scoring
    from ai_engine import process_new_jobs
    process_new_jobs(DB_PATH, min_score=0, user_id=uid)
    
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
    elif filename.endswith(".docx"):
        import docx
        try:
            doc = docx.Document(file)
            resume_text = "\n".join([p.text for p in doc.paragraphs])
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error parsing DOCX: {str(e)}"}), 400
    else:
        return jsonify({"ok": False, "error": "Unsupported file format. Please upload PDF, TXT, or DOCX."}), 400
        
    conn = get_conn(DB_PATH)
    # Get gemini api key to use for parsing
    p_row = conn.execute("SELECT gemini_api_key FROM users WHERE id = ?", (uid,)).fetchone()
    api_key = p_row[0] if p_row else None
    
    from resume_parser import parse_resume
    profile_json = ""
    try:
        profile_json = json.dumps(parse_resume(resume_text, api_key))
    except Exception as e:
        print(f"Error parsing resume: {e}")
        
    conn.execute("UPDATE users SET resume_text = ?, resume_filename = ?, resume_profile_json = ? WHERE id = ?", (resume_text, file.filename, profile_json, uid))
    conn.commit()
    
    # Reset all inbox jobs (status 'new', 'scored', 'ready') to 'new' for re-evaluation
    conn.execute(
        "UPDATE jobs SET status = 'new' WHERE user_id = ? AND status IN ('new', 'scored', 'ready')",
        (uid,)
    )
    conn.commit()
    
    # Run re-scoring in a background thread to prevent gateway timeouts on large pipelines
    import threading
    from ai_engine import process_new_jobs
    threading.Thread(target=process_new_jobs, args=(DB_PATH, 0, uid), daemon=True).start()
    
    conn.close()
    return jsonify({"ok": True, "resume_text": resume_text, "resume_filename": file.filename})


@app.route("/api/onboard_skip", methods=["POST"])
def onboard_skip():
    session["skip_onboarding"] = True
    return jsonify({"ok": True})


@app.route("/api/onboard_resume", methods=["POST"])
def onboard_resume():
    uid = get_user_id()
    file = request.files.get("resume_file")
    
    resume_text = ""
    resume_filename = "Pasted_Resume.txt"
    if file and file.filename:
        resume_filename = file.filename
        filename = file.filename.lower()
        if filename.endswith(".txt"):
            resume_text = file.read().decode("utf-8", errors="ignore")
        elif filename.endswith(".pdf"):
            import pypdf
            try:
                reader = pypdf.PdfReader(file)
                resume_text = "\n".join([page.extract_text() or "" for page in reader.pages])
            except Exception as e:
                return jsonify({"ok": False, "error": f"Error parsing PDF: {str(e)}"}), 400
        elif filename.endswith(".docx"):
            import docx
            try:
                doc = docx.Document(file)
                resume_text = "\n".join([p.text for p in doc.paragraphs])
            except Exception as e:
                return jsonify({"ok": False, "error": f"Error parsing DOCX: {str(e)}"}), 400
        else:
            return jsonify({"ok": False, "error": "Unsupported file format. Please upload PDF, TXT, or DOCX."}), 400
    else:
        # Fallback to form field or JSON body
        resume_text = request.form.get("resume_text", "").strip()
        if not resume_text:
            try:
                d = request.json or {}
                resume_text = d.get("resume_text", "").strip()
            except Exception:
                pass

    if not resume_text:
        return jsonify({"ok": False, "error": "Resume text is empty"}), 400

    conn = get_conn(DB_PATH)
    # Get gemini api key to use for parsing
    p_row = conn.execute("SELECT gemini_api_key FROM users WHERE id = ?", (uid,)).fetchone()
    api_key = p_row[0] if p_row else None
    
    from resume_parser import parse_resume
    profile_json = ""
    try:
        profile_json = json.dumps(parse_resume(resume_text, api_key))
    except Exception as e:
        print(f"Error parsing resume: {e}")

    conn.execute("UPDATE users SET resume_text = ?, resume_filename = ?, resume_profile_json = ? WHERE id = ?", (resume_text, resume_filename, profile_json, uid))
    conn.commit()

    # Scrape live jobs matching user designation
    from scraper import run_all_scrapers
    conn.close()
    run_all_scrapers(DB_PATH, user_id=uid)
    conn = get_conn(DB_PATH)

    # Re-score all jobs for this user
    from ai_engine import process_new_jobs
    process_new_jobs(DB_PATH, min_score=0, user_id=uid)
    
    suggestions = conn.execute(
        """SELECT title, company, location, ai_score, ai_summary 
           FROM jobs WHERE user_id = ? AND ai_score IS NOT NULL 
           ORDER BY ai_score DESC LIMIT 3""", (uid,)
    ).fetchall()
    
    conn.close()
    return jsonify({
        "ok": True,
        "suggestions": [dict(s) for s in suggestions]
    })


@app.route("/api/resume/parse", methods=["POST"])
def api_resume_parse():
    file = request.files.get("resume_file")
    resume_text = ""
    if file and file.filename:
        filename = file.filename.lower()
        if filename.endswith(".txt"):
            resume_text = file.read().decode("utf-8", errors="ignore")
        elif filename.endswith(".pdf"):
            import pypdf
            try:
                reader = pypdf.PdfReader(file)
                resume_text = "\n".join([page.extract_text() or "" for page in reader.pages])
            except Exception as e:
                return jsonify({"ok": False, "error": f"Error parsing PDF: {str(e)}"}), 400
        elif filename.endswith(".docx"):
            import docx
            try:
                doc = docx.Document(file)
                resume_text = "\n".join([p.text for p in doc.paragraphs])
            except Exception as e:
                return jsonify({"ok": False, "error": f"Error parsing DOCX: {str(e)}"}), 400
        else:
            return jsonify({"ok": False, "error": "Unsupported file format"}), 400
    else:
        try:
            d = request.json or {}
            resume_text = d.get("resume_text", "").strip()
        except Exception:
            resume_text = request.form.get("resume_text", "").strip()
            
    if not resume_text:
        return jsonify({"ok": False, "error": "No resume content provided"}), 400
        
    from resume_parser import parse_resume
    try:
        uid = get_user_id()
        conn = get_conn(DB_PATH)
        row = conn.execute("SELECT gemini_api_key FROM users WHERE id = ?", (uid,)).fetchone()
        conn.close()
        api_key = row[0] if row else None
        
        profile = parse_resume(resume_text, api_key)
        return jsonify({"ok": True, "profile": profile})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Helpers ────────────────────────────────────────────────────────────────

def _stats(conn):
    uid = get_user_id()
    total = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (uid,)).fetchone()[0]
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM jobs WHERE user_id = ? GROUP BY status", (uid,)).fetchall())
    avg = conn.execute("SELECT AVG(ai_score) FROM jobs WHERE user_id = ? AND ai_score IS NOT NULL", (uid,)).fetchone()[0]
    email_count = conn.execute("SELECT COUNT(*) FROM received_emails WHERE user_id = ?", (uid,)).fetchone()[0]
    
    last_scraped_row = conn.execute("SELECT last_scraped_at FROM users WHERE id = ?", (uid,)).fetchone()
    last_updated = last_scraped_row[0] if last_scraped_row and last_scraped_row[0] else None
    
    if not last_updated:
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


def _batch_full_jobs(conn, raw_jobs, uid):
    if not raw_jobs:
        return []

    # 1. Cover letters
    cl_rows = conn.execute("SELECT * FROM cover_letters WHERE user_id = ?", (uid,)).fetchall()
    cls = {row["job_id"]: dict(row) for row in cl_rows}

    # 2. Contacts
    contact_rows = conn.execute("SELECT * FROM contacts WHERE user_id = ?", (uid,)).fetchall()
    contacts = {}
    for row in contact_rows:
        jid = row["job_id"]
        if jid not in contacts:
            contacts[jid] = []
        contacts[jid].append(dict(row))

    # 3. Notes
    notes_rows = conn.execute("SELECT * FROM application_notes WHERE user_id = ?", (uid,)).fetchall()
    notes = {row["job_id"]: dict(row) for row in notes_rows}

    # 4. Timeline
    timeline_rows = conn.execute("SELECT job_id, event, created_at FROM application_timeline WHERE user_id = ? ORDER BY created_at", (uid,)).fetchall()
    timelines = {}
    for row in timeline_rows:
        jid = row["job_id"]
        if jid not in timelines:
            timelines[jid] = []
        timelines[jid].append({"event": row["event"], "created_at": row["created_at"]})

    # 5. Emails
    email_rows = conn.execute("SELECT * FROM received_emails WHERE user_id = ? ORDER BY received_at DESC", (uid,)).fetchall()
    emails = {}
    for row in email_rows:
        jid = row["job_id"]
        if jid not in emails:
            emails[jid] = []
        emails[jid].append(dict(row))

    # Construct the full job objects
    full_jobs = []
    for row in raw_jobs:
        j = dict(row)
        jid = j["job_id"]
        
        j["key_reqs_list"] = json.loads(j.get("key_reqs") or "[]")
        j["matched_skills_list"] = json.loads(j.get("matched_skills") or "[]")
        j["missing_skills_list"] = json.loads(j.get("missing_skills") or "[]")
        
        j["cover_letter"] = cls.get(jid)
        j["contacts"] = contacts.get(jid, [])
        j["notes"] = notes.get(jid, {"note": "", "linkedin_note": ""})
        j["timeline"] = timelines.get(jid, [])
        j["emails"] = emails.get(jid, [])
        
        full_jobs.append(j)
        
    return full_jobs



# ── Pages ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = get_conn(DB_PATH)
    uid = session.get("user_id")
    print(f"DEBUG: index route called. session user_id = {uid}")
    if not uid:
        # Fetch consenting seekers for landing page wall (excluding admin)
        users = conn.execute(
            "SELECT name, designation, linkedin_profile FROM users WHERE share_profile = 1 AND username != 'admin' ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return render_template("landing.html", seekers=[dict(u) for u in users])
        
    # Get user settings to pass to frontend profile form
    settings = get_user_settings(conn, uid)
    needs_onboarding = not settings.get("resume_text") and not session.get("skip_onboarding")
    if needs_onboarding:
        stats = _stats(conn)
        conn.close()
        return render_template("dashboard.html", settings=settings, view_mode="onboarding", stats=stats)
        
    # Fetch all active jobs (ignore archived)
    raw_jobs = conn.execute("SELECT * FROM jobs WHERE user_id = ? AND status != 'archived' ORDER BY COALESCE(ai_score,0) DESC", (uid,)).fetchall()
    
    board = {
        "whatsapp": [],
        "new": [],
        "shortlisted": [],
        "interviewing": [],
        "applied": [],
        "offer": [],
        "rejected": []
    }
    
    active_jobs = _batch_full_jobs(conn, raw_jobs, uid)
    for j in active_jobs:
        status = j["status"]
        if status == "whatsapp":
            board["whatsapp"].append(j)
        elif status in ("new", "scored", "ready"):
            board["new"].append(j)
        elif status == "shortlisted":
            board["shortlisted"].append(j)
        elif status == "interviewing":
            board["interviewing"].append(j)
        elif status == "applied":
            board["applied"].append(j)
        elif status == "offer":
            board["offer"].append(j)
        elif status == "rejected":
            board["rejected"].append(j)
            
    stats = _stats(conn)
    companies = [r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE user_id = ? ORDER BY company", (uid,)).fetchall()]
    conn.close()
    
    is_new_user = session.get("is_new_user", False)
    if is_new_user:
        session.pop("is_new_user", None)
        
    cols = ["whatsapp", "new", "shortlisted", "interviewing", "applied", "offer", "rejected"]
    return render_template("dashboard.html", board=board, cols=cols, stats=stats,
                           companies=companies, settings=settings, view_mode="board", is_new_user=is_new_user)


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
    conn.commit()
    
    # Reset all inbox jobs (status 'new', 'scored', 'ready') to 'new' for re-evaluation
    conn.execute(
        "UPDATE jobs SET status = 'new' WHERE user_id = ? AND status IN ('new', 'scored', 'ready')",
        (uid,)
    )
    conn.commit()
    
    # Run re-scoring
    from ai_engine import process_new_jobs
    process_new_jobs(DB_PATH, min_score=0, user_id=uid)
    
    conn.close()
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


@app.route("/api/cron/daily-recommendations", methods=["POST"])
def cron_daily_recommendations():
    auth_header = request.headers.get("X-Cron-Token")
    expected_token = os.environ.get("CRON_SECRET", "default_cron_secret")
    if auth_header != expected_token:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    conn = get_conn(DB_PATH)
    users = conn.execute("SELECT id, daily_recs_min_score FROM users WHERE daily_recs_enabled = 1").fetchall()
    
    from scraper import run_all_scrapers
    from linkedin_finder import enrich_jobs_with_contacts
    from email_scraper import sync_job_statuses_from_email
    from ai_engine import process_new_jobs
    from datetime import datetime

    for user in users:
        uid = user[0]
        min_score = user[1] if user[1] is not None else 7.5

        # 1. Clear old daily picks
        conn.execute("UPDATE jobs SET is_daily_pick = 0 WHERE user_id = ?", (uid,))
        conn.commit()

        # 2. Run scrapers
        run_all_scrapers(DB_PATH, user_id=uid)

        # 3. Find contacts
        enrich_jobs_with_contacts(DB_PATH)

        # 4. Sync emails
        try:
            sync_job_statuses_from_email(DB_PATH, user_id=uid)
        except Exception as e:
            print(f"Email sync failed: {e}")

        # 5. Get new jobs before scoring
        new_jobs = conn.execute("SELECT job_id FROM jobs WHERE status = 'new' AND user_id = ?", (uid,)).fetchall()
        new_job_ids = [r[0] for r in new_jobs]

        # 6. Score jobs
        process_new_jobs(DB_PATH, min_score=0, user_id=uid)

        # 7. Set daily picks
        if new_job_ids:
            placeholders = ",".join(["?"] * len(new_job_ids))
            query = f"""
                UPDATE jobs 
                SET is_daily_pick = 1, picked_at = ? 
                WHERE user_id = ? AND job_id IN ({placeholders}) AND ai_score >= ?
            """
            conn.execute(query, (datetime.now().isoformat(), uid, *new_job_ids, min_score))
            conn.commit()

        # 8. Fire email digest
        try:
            from notifier import send_email_digest
            send_email_digest(uid)
        except Exception as e:
            print(f"Failed to send email digest: {e}")

        # 9. Update last scraped time
        conn.execute("UPDATE users SET last_scraped_at = ? WHERE id = ?", (datetime.now().isoformat(), uid))
        conn.commit()

    conn.close()
    return jsonify({"ok": True})


@app.route("/api/recommendations/today", methods=["GET"])
def get_daily_recommendations():
    uid = get_user_id()
    conn = get_conn(DB_PATH)
    picks = conn.execute(
        """SELECT job_id, title, company, location, url, ai_score, ai_summary, picked_at 
           FROM jobs 
           WHERE user_id = ? AND is_daily_pick = 1 
           ORDER BY ai_score DESC""", (uid,)
    ).fetchall()
    conn.close()
    return jsonify({
        "ok": True,
        "picks": [dict(p) for p in picks]
    })


@app.route("/api/job/<job_id>/interview-round", methods=["POST"])
def update_interview_round(job_id):
    uid = get_user_id()
    d = request.json or {}
    round_name = d.get("round", "").strip()
    
    conn = get_conn(DB_PATH)
    row = conn.execute("SELECT 1 FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Job not found"}), 404
        
    from datetime import datetime
    
    conn.execute(
        "UPDATE jobs SET interview_round = ?, interview_round_updated_at = ? WHERE job_id = ? AND user_id = ?",
        (round_name, datetime.now().isoformat(), job_id, uid)
    )
    conn.commit()
    
    add_timeline(conn, job_id, f"Interview round -> {round_name}")
    
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/interview-prep", methods=["GET"])
def get_job_interview_prep(job_id):
    uid = get_user_id()
    conn = get_conn(DB_PATH)
    
    prep = conn.execute(
        "SELECT quick_questions, deep_questions FROM interview_prep WHERE job_id = ? AND user_id = ?",
        (job_id, uid)
    ).fetchone()
    
    if prep:
        conn.close()
        return jsonify({
            "ok": True,
            "quick_questions": json.loads(prep[0] or "[]"),
            "deep_questions": json.loads(prep[1] or "[]")
        })
        
    job = conn.execute(
        "SELECT title, company, description FROM jobs WHERE job_id = ? AND user_id = ?",
        (job_id, uid)
    ).fetchone()
    
    if not job:
        conn.close()
        return jsonify({"ok": False, "error": "Job not found"}), 404
        
    row = conn.execute("SELECT resume_text, gemini_api_key FROM users WHERE id = ?", (uid,)).fetchone()
    resume_text = row[0] if row else None
    api_key = row[1] if row else None
    
    from ai_engine import generate_interview_prep
    from datetime import datetime
    
    try:
        prep_data = generate_interview_prep(job[0], job[1], job[2] or "", resume_text, api_key)
        quick_json = json.dumps(prep_data.get("quick_questions", []))
        deep_json = json.dumps(prep_data.get("deep_questions", []))
        
        conn.execute(
            """INSERT OR REPLACE INTO interview_prep (job_id, quick_questions, deep_questions, created_at, user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (job_id, quick_json, deep_json, datetime.now().isoformat(), uid)
        )
        conn.commit()
        conn.close()
        
        return jsonify({
            "ok": True,
            "quick_questions": prep_data.get("quick_questions", []),
            "deep_questions": prep_data.get("deep_questions", [])
        })
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "error": f"Failed to generate prep questions: {e}"}), 500


@app.route("/api/job/<job_id>/interview-prep/regenerate", methods=["POST"])
def regenerate_job_interview_prep(job_id):
    uid = get_user_id()
    conn = get_conn(DB_PATH)
    
    job = conn.execute(
        "SELECT title, company, description FROM jobs WHERE job_id = ? AND user_id = ?",
        (job_id, uid)
    ).fetchone()
    
    if not job:
        conn.close()
        return jsonify({"ok": False, "error": "Job not found"}), 404
        
    row = conn.execute("SELECT resume_text, gemini_api_key FROM users WHERE id = ?", (uid,)).fetchone()
    resume_text = row[0] if row else None
    api_key = row[1] if row else None
    
    from ai_engine import generate_interview_prep
    from datetime import datetime
    
    try:
        prep_data = generate_interview_prep(job[0], job[1], job[2] or "", resume_text, api_key)
        quick_json = json.dumps(prep_data.get("quick_questions", []))
        deep_json = json.dumps(prep_data.get("deep_questions", []))
        
        conn.execute(
            """INSERT OR REPLACE INTO interview_prep (job_id, quick_questions, deep_questions, created_at, user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (job_id, quick_json, deep_json, datetime.now().isoformat(), uid)
        )
        conn.commit()
        conn.close()
        
        return jsonify({
            "ok": True,
            "quick_questions": prep_data.get("quick_questions", []),
            "deep_questions": prep_data.get("deep_questions", [])
        })
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "error": f"Failed to regenerate prep questions: {e}"}), 500


@app.route("/api/stats")
def api_stats():
    conn = get_conn(DB_PATH)
    s = _stats(conn)
    conn.close()
    return jsonify(s)


@app.route("/api/refresh", methods=["POST"])
def refresh_listings():
    import threading
    from datetime import datetime
    from scraper import run_all_scrapers
    from linkedin_finder import enrich_jobs_with_contacts
    from ai_engine import process_new_jobs
    
    uid = get_user_id()
    
    def run_sync():
        try:
            print(f"Background sync started for user {uid}")
            run_all_scrapers(DB_PATH, user_id=uid)
            
            conn = get_conn(DB_PATH)
            conn.execute("UPDATE jobs SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
            conn.commit()
            
            enrich_jobs_with_contacts(DB_PATH)
            conn.execute("UPDATE contacts SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
            conn.commit()
            
            from email_scraper import sync_job_statuses_from_email
            sync_job_statuses_from_email(DB_PATH, user_id=uid)
            conn.execute("UPDATE received_emails SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
            conn.commit()
            
            min_score = float(os.getenv("MIN_SCORE", "0.0"))
            process_new_jobs(DB_PATH, min_score=min_score, user_id=uid)
            
            conn.execute("UPDATE users SET last_scraped_at = ? WHERE id = ?", (datetime.now().isoformat(), uid))
            conn.commit()
            conn.close()
            print(f"Background sync successfully completed for user {uid}")
        except Exception as err:
            print(f"Background sync failed for user {uid}: {err}")
            
    threading.Thread(target=run_sync).start()
    return jsonify({"ok": True, "message": "Sync started in background"})


@app.route("/api/voice/transcribe", methods=["POST"])
def voice_transcribe():
    import base64
    import requests
    from ai_engine import get_fallback_gemini_key
    uid = get_user_id()
    
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    audio_file = request.files['file']
    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"error": "Empty audio file"}), 400
        
    base64_audio = base64.b64encode(audio_bytes).decode('utf-8')
    
    conn = get_conn(DB_PATH)
    settings = get_user_settings(conn, uid)
    gemini_key = settings.get("gemini_api_key") or get_fallback_gemini_key()
    conn.close()
    
    if not gemini_key:
        return jsonify({"error": "No Gemini key configured", "provider_fallback": True}), 400
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
    payload = {
        "contents": [{
            "parts": [
                {
                    "inlineData": {
                        "mimeType": "audio/webm",
                        "data": base64_audio
                    }
                },
                {
                    "text": "Transcribe this audio clip into plain English text. Respond only with the exact transcription, without adding any introductory or concluding comments, quotes, or conversational padding. If the audio is silent or unintelligible, respond with an empty string."
                }
            ]
        }]
    }
    
    try:
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=8)
        res.raise_for_status()
        res_data = res.json()
        transcript = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return jsonify({"transcript": transcript})
    except Exception as e:
        print(f"Gemini STT proxy failed: {e}")
        return jsonify({"error": str(e), "provider_fallback": True}), 500


@app.route("/api/voice/synthesize", methods=["POST"])
def voice_synthesize():
    import base64
    import requests
    from ai_engine import get_fallback_gemini_key
    from voice_engine import pcm_to_wav
    uid = get_user_id()
    d = request.json or {}
    text = d.get("text", "").strip()
    
    if not text:
        return jsonify({"error": "No text provided"}), 400
        
    conn = get_conn(DB_PATH)
    settings = get_user_settings(conn, uid)
    gemini_key = settings.get("gemini_api_key") or get_fallback_gemini_key()
    conn.close()
    
    if not gemini_key:
        return jsonify({"error": "No Gemini key configured", "provider_fallback": True}), 400
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": "Read the following response text out loud with a clear, helpful, professional voice: " + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": "Puck"
                    }
                }
            }
        }
    }
    
    try:
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=8)
        res.raise_for_status()
        res_data = res.json()
        inline_data = res_data["candidates"][0]["content"]["parts"][0]["inlineData"]
        pcm_base64 = inline_data["data"]
        pcm_bytes = base64.b64decode(pcm_base64)
        
        # Package raw PCM into a playable WAV bytes format
        wav_bytes = pcm_to_wav(pcm_bytes)
        wav_base64 = base64.b64encode(wav_bytes).decode('utf-8')
        return jsonify({"audio_base64": wav_base64})
    except Exception as e:
        print(f"Gemini TTS proxy failed: {e}")
        return jsonify({"error": str(e), "provider_fallback": True}), 500


@app.route("/api/voice/query", methods=["POST"])
def voice_query():
    from voice_engine import classify_intent_and_slot
    uid = get_user_id()
    d = request.json or {}
    transcript = d.get("transcript", "").strip()
    session_id = d.get("session_id", "default")
    
    if not transcript:
        return jsonify({"intent": None, "reply_text": "I didn't hear anything. Please try again.", "requires_confirmation": False, "reply_cards": []})
        
    conn = get_conn(DB_PATH)
    settings = get_user_settings(conn, uid)
    gemini_key = settings.get("gemini_api_key")
    
    # Query current jobs snapshot
    rows = conn.execute(
        "SELECT job_id, title, company, status, ai_score FROM jobs WHERE user_id = ? AND status != 'archived' ORDER BY scraped_at DESC LIMIT 50",
        (uid,)
    ).fetchall()
    jobs_snapshot = [{"job_id": r[0], "title": r[1], "company": r[2], "status": r[3], "score": r[4]} for r in rows]
    
    # Classify intent
    res = classify_intent_and_slot(transcript, jobs_snapshot, api_key=gemini_key)
    intent = res.get("intent")
    slots = res.get("slots", {})
    job_id = slots.get("job_id")
    
    mutating_intents = {"move_job", "trigger_refresh", "regenerate_cover_letter", "send_email", "archive_job"}
    reply_cards = []
    
    if intent in mutating_intents:
        if intent == "move_job":
            target_status = slots.get("status")
            if not target_status:
                conn.close()
                return jsonify({"intent": intent, "reply_text": "Which column would you like to move it to?", "requires_confirmation": False, "reply_cards": []})
            job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not job:
                conn.close()
                return jsonify({"intent": intent, "reply_text": "Sorry, I couldn't find that job listing on your board.", "requires_confirmation": False, "reply_cards": []})
            reply_text = f"Move '{job[1]}' at {job[0]} to {target_status}?"
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        elif intent == "trigger_refresh":
            reply_text = "Would you like me to refresh your listings and check for new jobs?"
        elif intent == "regenerate_cover_letter":
            job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not job:
                conn.close()
                return jsonify({"intent": intent, "reply_text": "Sorry, I couldn't find that job listing.", "requires_confirmation": False, "reply_cards": []})
            reply_text = f"Regenerate the cover letter for '{job[1]}' at {job[0]}?"
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        elif intent == "send_email":
            job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not job:
                conn.close()
                return jsonify({"intent": intent, "reply_text": "Sorry, I couldn't find that job.", "requires_confirmation": False, "reply_cards": []})
            reply_text = f"Send the application email to {job[0]}?"
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        elif intent == "archive_job":
            job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not job:
                conn.close()
                return jsonify({"intent": intent, "reply_text": "Sorry, I couldn't find that job.", "requires_confirmation": False, "reply_cards": []})
            reply_text = f"Archive the job '{job[1]}' at {job[0]}?"
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
            
        session['voice_pending_action'] = {"intent": intent, "slots": slots}
        conn.close()
        return jsonify({
            "intent": intent,
            "reply_text": reply_text,
            "requires_confirmation": True,
            "action": {"intent": intent, "slots": slots},
            "reply_cards": reply_cards
        })
        
    reply_text = "I'm not sure how to help with that yet. You can ask about your pipeline, email matches, or say things like 'move X to applied'."
    
    if intent == "pipeline_stats":
        stats = _stats(conn)
        total = stats.get("total", 0)
        avg = stats.get("avg_score", 0)
        applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'applied'", (uid,)).fetchone()[0]
        shortlisted = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'shortlist'", (uid,)).fetchone()[0]
        offers = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'offer'", (uid,)).fetchone()[0]
        reply_text = f"You have {total} total jobs in your pipeline with an average score of {avg:.1f}/10."
        reply_cards = [{
            "type": "stats",
            "total": total,
            "avg_score": avg,
            "applied": applied,
            "shortlisted": shortlisted,
            "offers": offers
        }]
        
    elif intent == "column_count":
        column = slots.get("column")
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = ?", (uid, column)).fetchone()[0]
        reply_text = f"You have {count} jobs in your {column} column."
        
    elif intent == "job_lookup":
        job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if job:
            reply_text = f"Found job '{job[1]}' at {job[0]}. Location is {job[5]} and its AI fit score is {job[3]:.1f}/10."
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        else:
            reply_text = "Sorry, I couldn't find details for that job."
            
    elif intent == "job_fit":
        job = conn.execute("SELECT job_id, title, company, ai_score, status, location, ai_summary FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if job:
            summary = job[6] or "No AI summary available."
            reply_text = f"The job '{job[1]}' at {job[2]} scored {job[3]:.1f}/10 because: {summary}"
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        else:
            reply_text = "Sorry, I couldn't find the fit analysis for that job."
            
    elif intent == "job_status":
        job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if job:
            reply_text = f"The job '{job[1]}' at {job[2]} is currently in the '{job[4]}' column."
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        else:
            reply_text = "Sorry, I couldn't verify the status of that job."
            
    elif intent == "email_lookup":
        company = slots.get("company")
        if not company and job_id:
            j = conn.execute("SELECT company FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            company = j[0] if j else None
            
        if company:
            email = conn.execute(
                "SELECT sender, subject, body FROM received_emails WHERE user_id = ? AND (sender LIKE ? OR body LIKE ?) ORDER BY received_at DESC LIMIT 1",
                (uid, f"%{company}%", f"%{company}%")
            ).fetchone()
            if email:
                reply_text = f"Found a recent email from {company}. Subject: {email[1]}."
                reply_cards = [{"type": "email", "sender": email[0], "subject": email[1], "body": email[2]}]
            else:
                reply_text = f"No recent emails found from {company}."
        else:
            reply_text = "Which company's emails would you like to check?"
            
    elif intent == "email_count":
        count = conn.execute("SELECT COUNT(*) FROM received_emails WHERE user_id = ?", (uid,)).fetchone()[0]
        reply_text = f"You have {count} synced emails."
        
    elif intent == "last_sync":
        user = conn.execute("SELECT last_scraped_at FROM users WHERE id = ?", (uid,)).fetchone()
        if user and user[0]:
            try:
                dt = datetime.fromisoformat(user[0].split('.')[0])
                date_str = dt.strftime("%b %d at %I:%M %p")
                reply_text = f"Your board was last refreshed on {date_str}."
            except Exception:
                reply_text = f"Your board was last refreshed on {user[0]}."
        else:
            reply_text = "Your board has not been refreshed yet."
            
    elif intent == "top_matches":
        matches = conn.execute(
            "SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE user_id = ? AND status != 'archived' AND ai_score IS NOT NULL ORDER BY ai_score DESC LIMIT 3",
            (uid,)
        ).fetchall()
        if matches:
            list_str = ", ".join([f"{m[1]} at {m[2]} with a score of {m[3]:.1f}" for m in matches])
            reply_text = f"Your top matches are: {list_str}."
            reply_cards = [{"type": "job", "job_id": m[0], "title": m[1], "company": m[2], "score": m[3], "status": m[4], "location": m[5]} for m in matches]
        else:
            reply_text = "You don't have any scored job listings on your board."
            
    elif intent == "cover_letter_status":
        cl = conn.execute("SELECT 1 FROM cover_letters WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if cl:
            reply_text = "Yes, you have a cover letter generated for this job."
        else:
            reply_text = "No cover letter has been generated for this job yet."
        if job:
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
            
    elif intent == "thumbs_up":
        job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if job:
            conn.execute("UPDATE jobs SET feedback = 1 WHERE job_id = ? AND user_id = ?", (job_id, uid))
            conn.commit()
            reply_text = f"Marked '{job[1]}' at {job[2]} as liked."
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        else:
            reply_text = "Sorry, I couldn't find that job."
            
    elif intent == "thumbs_down":
        job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
        if job:
            conn.execute("UPDATE jobs SET feedback = -1 WHERE job_id = ? AND user_id = ?", (job_id, uid))
            conn.commit()
            reply_text = f"Marked '{job[1]}' at {job[2]} as disliked."
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": job[4], "location": job[5]}]
        else:
            reply_text = "Sorry, I couldn't find that job."
            
    conn.close()
    return jsonify({
        "intent": intent,
        "reply_text": reply_text,
        "requires_confirmation": False,
        "reply_cards": reply_cards
    })


@app.route("/api/voice/confirm", methods=["POST"])
def voice_confirm():
    uid = get_user_id()
    pending = session.pop('voice_pending_action', None)
    if not pending:
        return jsonify({"ok": False, "error": "No pending action found or session expired"}), 400
        
    intent = pending.get("intent")
    slots = pending.get("slots", {})
    job_id = slots.get("job_id")
    
    conn = get_conn(DB_PATH)
    reply_cards = []
    
    try:
        if intent == "move_job":
            status = slots.get("status")
            job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not job:
                return jsonify({"ok": False, "error": "Job not found"}), 404
            conn.execute("UPDATE jobs SET status = ? WHERE job_id = ? AND user_id = ?", (status, job_id, uid))
            add_timeline(conn, job_id, f"Pipeline → {status} (Voice)")
            conn.commit()
            
            conn.execute(
                "UPDATE jobs SET status = 'new' WHERE user_id = ? AND status IN ('new', 'scored', 'ready')",
                (uid,)
            )
            conn.commit()
            reply_text = f"Successfully moved '{job[1]}' at {job[2]} to {status}."
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": status, "location": job[5]}]
            
        elif intent == "trigger_refresh":
            import threading
            from scraper import run_all_scrapers
            from linkedin_finder import enrich_jobs_with_contacts
            from ai_engine import process_new_jobs
            
            def run_sync():
                try:
                    thread_conn = get_conn(DB_PATH)
                    run_all_scrapers(DB_PATH, user_id=uid)
                    thread_conn.execute("UPDATE jobs SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
                    thread_conn.commit()
                    enrich_jobs_with_contacts(DB_PATH)
                    thread_conn.execute("UPDATE contacts SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
                    thread_conn.commit()
                    
                    from email_scraper import sync_job_statuses_from_email
                    sync_job_statuses_from_email(DB_PATH, user_id=uid)
                    thread_conn.execute("UPDATE received_emails SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (uid,))
                    thread_conn.commit()
                    
                    min_score = float(os.getenv("MIN_SCORE", "0.0"))
                    process_new_jobs(DB_PATH, min_score=min_score, user_id=uid)
                    thread_conn.execute("UPDATE users SET last_scraped_at = ? WHERE id = ?", (datetime.now().isoformat(), uid))
                    thread_conn.commit()
                    thread_conn.close()
                except Exception as err:
                    print("Voice refresh failed:", err)
                    
            threading.Thread(target=run_sync).start()
            reply_text = "Sync started in the background. It will reload when completed."
            
        elif intent == "regenerate_cover_letter":
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not job:
                return jsonify({"ok": False, "error": "Job not found"}), 404
            j = dict(job)
            contact = conn.execute("SELECT name, title FROM contacts WHERE job_id = ? AND user_id = ? LIMIT 1", (job_id, uid)).fetchone()
            cn = contact[0] if contact else "Hiring Team"
            ct = contact[1] if contact else "Recruiter"
            
            settings = get_user_settings(conn, uid)
            from ai_engine import score_job, generate_cover_letter, generate_linkedin_note
            score_data = score_job(j["title"], j["company"], j.get("description",""), resume_text=settings.get("resume_text"), api_key=settings.get("gemini_api_key"))
            letter = generate_cover_letter(j["title"], j["company"], j.get("description",""), cn, ct, resume_text=settings.get("resume_text"), api_key=settings.get("gemini_api_key"))
            li_note = generate_linkedin_note(cn, ct, j["company"], j["title"], api_key=settings.get("gemini_api_key"))
            
            conn.execute("UPDATE jobs SET ai_score = ?, ai_summary = ?, key_reqs = ? WHERE job_id = ? AND user_id = ?",
                         (score_data["score"], score_data["fit_summary"], json.dumps(score_data.get("key_requirements", [])), job_id, uid))
            conn.execute("INSERT OR REPLACE INTO cover_letters (job_id, subject, body, linkedin_note, created_at, user_id) VALUES (?,?,?,?,?,?)",
                         (job_id, letter["subject"], letter["body"], li_note, datetime.now().isoformat(), uid))
            add_timeline(conn, job_id, "Regenerated cover letter (Voice)")
            conn.commit()
            reply_text = f"Successfully regenerated cover letter for '{j['title']}' at {j['company']}."
            reply_cards = [{"type": "job", "job_id": j["job_id"], "title": j["title"], "company": j["company"], "score": score_data["score"], "status": j["status"], "location": j.get("location","")}]
            
        elif intent == "send_email":
            from notifier import send_email_digest
            j_row = conn.execute("SELECT * FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not j_row:
                return jsonify({"ok": False, "error": "Job not found"}), 404
            j = dict(j_row)
            cl = conn.execute("SELECT * FROM cover_letters WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            c = conn.execute("SELECT * FROM contacts WHERE job_id = ? AND user_id = ? LIMIT 1", (job_id, uid)).fetchone()
            if not cl:
                return jsonify({"ok": False, "error": "No cover letter found to send"}), 400
                
            item = {
                **j, "score": j.get("ai_score", 0),
                "fit_summary": j.get("ai_summary", ""),
                "key_requirements": json.loads(j.get("key_reqs") or "[]"),
                "cover_letter_subject": cl["subject"],
                "cover_letter_body": cl["body"],
                "contact_name": c["name"] if c else "Hiring Team",
                "contact_title": c["title"] if c else "",
                "linkedin_note": cl.get("linkedin_note", "")
            }
            ok = send_email_digest([item])
            if ok:
                conn.execute("UPDATE jobs SET status = 'applied' WHERE job_id = ? AND user_id = ?", (job_id, uid))
                add_timeline(conn, job_id, "Email sent → applied (Voice)")
                conn.commit()
                reply_text = f"Email sent successfully and job status updated to applied."
                reply_cards = [{"type": "job", "job_id": j["job_id"], "title": j["title"], "company": j["company"], "score": j.get("ai_score", 0), "status": "applied", "location": j.get("location","")}]
            else:
                reply_text = "Failed to send email. Ensure SENDER_EMAIL and SENDER_PASSWORD are in your configuration."
                
        elif intent == "archive_job":
            job = conn.execute("SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, uid)).fetchone()
            if not job:
                return jsonify({"ok": False, "error": "Job not found"}), 404
            conn.execute("UPDATE jobs SET status = 'archived' WHERE job_id = ? AND user_id = ?", (job_id, uid))
            add_timeline(conn, job_id, "Pipeline → archived (Voice)")
            conn.commit()
            reply_text = f"Successfully archived the job '{job[1]}' at {job[2]}."
            reply_cards = [{"type": "job", "job_id": job[0], "title": job[1], "company": job[2], "score": job[3], "status": "archived", "location": job[5]}]
            
        else:
            return jsonify({"ok": False, "error": "Invalid pending action"}), 400
            
        conn.close()
        return jsonify({"ok": True, "reply_text": reply_text, "reply_cards": reply_cards})
        
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/voice/digest", methods=["GET"])
def voice_digest():
    uid = get_user_id()
    conn = get_conn(DB_PATH)
    
    user_row = conn.execute("SELECT last_digest_read_at FROM users WHERE id = ?", (uid,)).fetchone()
    last_read = user_row[0] if user_row and user_row[0] else None
    
    if last_read:
        query = "SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE user_id = ? AND scraped_at > ? AND status != 'archived' ORDER BY ai_score DESC LIMIT 5"
        params = (uid, last_read)
    else:
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        query = "SELECT job_id, title, company, ai_score, status, location FROM jobs WHERE user_id = ? AND scraped_at > ? AND status != 'archived' ORDER BY ai_score DESC LIMIT 5"
        params = (uid, yesterday)
        
    rows = conn.execute(query, params).fetchall()
    
    peek = request.args.get("peek") == "true"
    if not peek:
        now_str = datetime.now().isoformat()
        conn.execute("UPDATE users SET last_digest_read_at = ? WHERE id = ?", (now_str, uid))
        conn.commit()
    conn.close()
    
    count = len(rows)
    if count == 0:
        reply_text = "You have no new job recommendations since you last checked."
    else:
        top_job = rows[0]
        reply_text = f"You have {count} new job recommendations since you last checked. Your top match is {top_job[1]} at {top_job[2]} with an AI score of {top_job[3]:.1f}/10."
        if count > 1:
            other_jobs = ", and ".join([f"{r[1]} at {r[2]}" for r in rows[1:]])
            reply_text += f" Other new recommendations include: {other_jobs}."
            
    return jsonify({
        "ok": True,
        "count": count,
        "reply_text": reply_text,
        "jobs": [{"job_id": r[0], "title": r[1], "company": r[2], "score": r[3]} for r in rows],
        "reply_cards": [{"type": "job", "job_id": r[0], "title": r[1], "company": r[2], "score": r[3], "status": r[4], "location": r[5]} for r in rows]
    })


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


@app.route("/logo.svg")
def serve_logo():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_file(os.path.join(base_dir, "logo.svg"), mimetype="image/svg+xml")


@app.route("/favicon.ico")
def serve_favicon():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_file(os.path.join(base_dir, "logo.svg"), mimetype="image/svg+xml")


@app.route("/api/debug/db")
def debug_db():
    uid = session.get("user_id")
    if not uid:
        return "Unauthorized", 401
    import re
    conn = get_conn(DB_PATH)
    try:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        jobs_sample = conn.execute("SELECT job_id, title, status, user_id FROM jobs LIMIT 5").fetchall()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()
        
    db_masked = ""
    if IS_POSTGRES:
        db_masked = re.sub(r":([^@]+)@", ":***@", DATABASE_URL)
        
    return jsonify({
        "ok": True,
        "is_postgres": IS_POSTGRES,
        "database_url_masked": db_masked,
        "db_path": DB_PATH,
        "user_count": user_count,
        "job_count": job_count,
        "jobs_sample": [dict(r) for r in jobs_sample]
    })


def run_dashboard(port=5050):
    init_db(DB_PATH)
    print(f"\nDashboard → http://localhost:{port}")
    print("Press Ctrl+C to stop.\n")
    app.run(debug=False, port=port, use_reloader=False)


if __name__ == "__main__":
    run_dashboard()
