"""
db.py — Single source of truth for database setup and helpers.
All tables are created here; every module imports from here.
"""

import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "jobs.db")


def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE,
            password_hash TEXT,
            resume_text   TEXT,
            imap_email    TEXT,
            imap_password TEXT,
            gemini_api_key TEXT,
            linkedin_profile TEXT,
            created_at    TEXT
        );
    """)
    conn.commit()
    
    admin_exists = conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone()
    if not admin_exists:
        from datetime import datetime
        import hashlib
        import binascii
        salt = b"default_salt_123"
        key = hashlib.pbkdf2_hmac("sha256", b"admin", salt, 100000)
        p_hash = binascii.hexlify(salt + b":" + key).decode("ascii")
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, password_hash, created_at) VALUES (1, ?, ?, ?)",
            ("admin", p_hash, datetime.now().isoformat())
        )
        conn.commit()
        
    tables = ["jobs", "contacts", "cover_letters", "application_notes", "application_timeline", "received_emails", "tailored_resumes"]
    for t in tables:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if columns and "user_id" not in columns:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN user_id INTEGER DEFAULT 1;")
            conn.commit()
            
    # Migrate users table columns if needed
    user_cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    for col in ["imap_email", "imap_password", "gemini_api_key", "linkedin_profile", "name", "designation", "resume_filename"]:
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT;")
            conn.commit()
    if "share_profile" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN share_profile INTEGER DEFAULT 0;")
        conn.commit()
        
    for col, ctype, default in [
        ("weight_thumbs_up", "REAL", "1.0"),
        ("weight_applied", "REAL", "1.0"),
        ("weight_thumbs_down", "REAL", "-1.0"),
        ("weight_rejected", "REAL", "-1.5")
    ]:
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ctype} DEFAULT {default};")
            conn.commit()
        
    # Migrate jobs table columns if needed
    jobs_cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "feedback" not in jobs_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN feedback INTEGER DEFAULT 0;")
        conn.commit()


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT UNIQUE,
            title         TEXT,
            company       TEXT,
            location      TEXT,
            url           TEXT,
            description   TEXT,
            source        TEXT,
            posted_at     TEXT,
            scraped_at    TEXT,
            ai_score      REAL,
            ai_summary    TEXT,
            key_reqs      TEXT,
            status        TEXT DEFAULT 'new'
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT,
            name          TEXT,
            title         TEXT,
            linkedin_url  TEXT,
            email         TEXT,
            found_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS cover_letters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT UNIQUE,
            subject       TEXT,
            body          TEXT,
            linkedin_note TEXT,
            created_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS application_notes (
            job_id        TEXT PRIMARY KEY,
            note          TEXT,
            linkedin_note TEXT
        );

        CREATE TABLE IF NOT EXISTS application_timeline (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT,
            event         TEXT,
            created_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS received_emails (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT,
            sender        TEXT,
            subject       TEXT,
            body          TEXT,
            received_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS tailored_resumes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT UNIQUE,
            resume_content TEXT,
            created_at    TEXT
        );
    """)
    conn.commit()
    migrate_db(conn)
    return conn


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone())


def add_timeline(conn: sqlite3.Connection, job_id: str, event: str) -> None:
    from datetime import datetime
    row = conn.execute("SELECT user_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    uid = row[0] if row else 1
    conn.execute(
        "INSERT INTO application_timeline (job_id, event, created_at, user_id) VALUES (?, ?, ?, ?)",
        (job_id, event, datetime.now().isoformat(), uid),
    )
    conn.commit()
