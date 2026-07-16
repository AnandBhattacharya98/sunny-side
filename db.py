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
    return conn


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone())


def add_timeline(conn: sqlite3.Connection, job_id: str, event: str) -> None:
    from datetime import datetime
    conn.execute(
        "INSERT INTO application_timeline (job_id, event, created_at) VALUES (?, ?, ?)",
        (job_id, event, datetime.now().isoformat()),
    )
    conn.commit()
