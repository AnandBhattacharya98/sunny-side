"""
db.py — Single source of truth for database setup and helpers.
All tables are created here; every module imports from here.
"""

import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "jobs.db")
if not os.path.isabs(DB_PATH):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(base_dir, DB_PATH)

DATABASE_URL = os.getenv("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import threading
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool

    class DictRowWrapper:
        def __init__(self, tuple_data, keys):
            self.tuple_data = tuple_data
            self._keys = keys
            self.dict_data = dict(zip(keys, tuple_data))

        def __getitem__(self, key):
            if isinstance(key, int):
                return self.tuple_data[key]
            return self.dict_data[key]

        def keys(self):
            return self._keys

        def get(self, key, default=None):
            return self.dict_data.get(key, default)

    class PgCursorWrapper:
        def __init__(self, cursor):
            self.cursor = cursor
            self._lastrowid = None

        def execute(self, query, params=None):
            # Normalize whitespace/newlines for robust pattern matching
            query = " ".join(query.split())

            if params is not None:
                query = query.replace('?', '%s')
            
            query = query.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            query = query.replace("integer primary key autoincrement", "serial primary key")
            query = query.replace("REAL", "DOUBLE PRECISION")
            query = query.replace("real", "DOUBLE PRECISION")
            
            if "INSERT OR IGNORE INTO users" in query:
                query = query.replace("INSERT OR IGNORE INTO users", "INSERT INTO users") + " ON CONFLICT (id) DO NOTHING"
            elif "INSERT OR IGNORE INTO jobs" in query:
                query = query.replace("INSERT OR IGNORE INTO jobs", "INSERT INTO jobs") + " ON CONFLICT (job_id) DO NOTHING"
            elif "INSERT OR IGNORE INTO cover_letters" in query:
                query = query.replace("INSERT OR IGNORE INTO cover_letters", "INSERT INTO cover_letters") + " ON CONFLICT (job_id) DO NOTHING"
            elif "INSERT OR IGNORE INTO tailored_resumes" in query:
                query = query.replace("INSERT OR IGNORE INTO tailored_resumes", "INSERT INTO tailored_resumes") + " ON CONFLICT (job_id) DO NOTHING"
            elif "INSERT OR IGNORE" in query:
                query = query.replace("INSERT OR IGNORE", "INSERT")

            if "INSERT OR REPLACE INTO cover_letters" in query:
                query = query.replace("INSERT OR REPLACE INTO cover_letters", "INSERT INTO cover_letters") + " ON CONFLICT (job_id) DO UPDATE SET subject = EXCLUDED.subject, body = EXCLUDED.body, linkedin_note = EXCLUDED.linkedin_note, created_at = EXCLUDED.created_at, user_id = EXCLUDED.user_id"
            elif "INSERT OR REPLACE INTO application_notes" in query:
                query = query.replace("INSERT OR REPLACE INTO application_notes", "INSERT INTO application_notes") + " ON CONFLICT (job_id) DO UPDATE SET note = EXCLUDED.note, linkedin_note = EXCLUDED.linkedin_note, user_id = EXCLUDED.user_id"
            elif "INSERT OR REPLACE INTO tailored_resumes" in query:
                query = query.replace("INSERT OR REPLACE INTO tailored_resumes", "INSERT INTO tailored_resumes") + " ON CONFLICT (job_id) DO UPDATE SET resume_content = EXCLUDED.resume_content, created_at = EXCLUDED.created_at, user_id = EXCLUDED.user_id"
            elif "INSERT OR REPLACE INTO interview_prep" in query:
                query = query.replace("INSERT OR REPLACE INTO interview_prep", "INSERT INTO interview_prep") + " ON CONFLICT (job_id) DO UPDATE SET quick_questions = EXCLUDED.quick_questions, deep_questions = EXCLUDED.deep_questions, created_at = EXCLUDED.created_at, user_id = EXCLUDED.user_id"

            if "PRAGMA table_info" in query:
                import re
                match = re.search(r"table_info\((.*?)\)", query)
                if match:
                    table_name = match.group(1).replace("'", "").replace('"', '').strip()
                    query = f"SELECT 0, column_name FROM information_schema.columns WHERE table_name = '{table_name}'"
                    params = None

            is_insert_user = "INSERT INTO users" in query
            if is_insert_user and "RETURNING id" not in query:
                query += " RETURNING id"

            if params is not None:
                self.cursor.execute(query, params)
            else:
                self.cursor.execute(query)

            if is_insert_user:
                try:
                    row = self.cursor.fetchone()
                    if row:
                        self._lastrowid = row[0]
                except Exception:
                    pass

            return self

        def fetchone(self):
            try:
                row = self.cursor.fetchone()
                if row is not None:
                    keys = [desc[0] for desc in self.cursor.description]
                    return DictRowWrapper(tuple(row), keys)
            except Exception:
                pass
            return None

        def fetchall(self):
            try:
                rows = self.cursor.fetchall()
                if rows:
                    keys = [desc[0] for desc in self.cursor.description]
                    return [DictRowWrapper(tuple(r), keys) for r in rows]
            except Exception:
                pass
            return []

        @property
        def lastrowid(self):
            return self._lastrowid

        def __iter__(self):
            return iter(self.fetchall())

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class PgConnectionWrapper:
        def __init__(self, conn, pool=None):
            self.conn = conn
            self._pool = pool
            self._released = False

        def cursor(self):
            return PgCursorWrapper(self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor))

        def execute(self, query, params=None):
            cur = self.cursor()
            cur.execute(query, params)
            return cur

        def executescript(self, script_str):
            cur = self.cursor()
            cur.execute(script_str)
            self.commit()

        def commit(self):
            self.conn.commit()

        def rollback(self):
            self.conn.rollback()

        def close(self):
            if self._released:
                return
            self._released = True
            if self._pool is None:
                self.conn.close()
                return
            # Hand the connection back clean, or drop it if it broke
            broken = bool(self.conn.closed)
            if not broken:
                try:
                    self.conn.rollback()
                except psycopg2.Error:
                    broken = True
            try:
                self._pool.putconn(self.conn, close=broken)
            except psycopg2.pool.PoolError:
                self.conn.close()

        def __del__(self):
            # A caller that forgets close() must not leak a pooled connection
            try:
                self.close()
            except Exception:
                pass

        def __getattr__(self, name):
            return getattr(self.conn, name)

    # One pool per process. Gunicorn forks workers, and a connection must never be
    # shared across processes, so a new pool is made whenever the pid changes.
    DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "8"))
    # psycopg2 only keeps this many idle connections open; extras are closed on return.
    # Matches gunicorn's 4 threads per worker.
    DB_POOL_IDLE = min(int(os.getenv("DB_POOL_IDLE", "4")), DB_POOL_MAX)
    _pool = None
    _pool_pid = None
    _pool_lock = threading.Lock()

    def _get_pool():
        global _pool, _pool_pid
        with _pool_lock:
            if _pool is None or _pool_pid != os.getpid():
                _pool = psycopg2.pool.ThreadedConnectionPool(DB_POOL_IDLE, DB_POOL_MAX, DATABASE_URL)
                _pool_pid = os.getpid()
            return _pool

    def _checkout():
        """A live pooled connection, or a direct one when the pool is full."""
        pool = _get_pool()
        for _ in range(2):
            try:
                raw = pool.getconn()
            except psycopg2.pool.PoolError:
                break
            try:
                if raw.closed:
                    raise psycopg2.InterfaceError("closed")
                # The server may have dropped an idle connection; check before handing it out
                with raw.cursor() as cur:
                    cur.execute("SELECT 1")
                raw.rollback()
                return PgConnectionWrapper(raw, pool)
            except psycopg2.Error:
                pool.putconn(raw, close=True)
        return PgConnectionWrapper(psycopg2.connect(DATABASE_URL))


def get_conn(db_path: str = DB_PATH):
    if IS_POSTGRES:
        return _checkout()
    else:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn


def migrate_db(conn) -> None:
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
    
    _ensure_admin(conn)

    tables = ["jobs", "contacts", "cover_letters", "application_notes", "application_timeline", "received_emails", "tailored_resumes"]
    for t in tables:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if columns and "user_id" not in columns:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN user_id INTEGER DEFAULT 1;")
            conn.commit()
            
    # Migrate users table columns if needed
    user_cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    for col in ["imap_email", "imap_password", "gemini_api_key", "linkedin_profile", "name", "designation", "resume_filename", "resume_profile_json", "last_scraped_at", "email", "auth_provider"]:
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
        
    for col in ["matched_skills", "missing_skills"]:
        if col not in jobs_cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT;")
            conn.commit()

    # Alter jobs table for daily picks and interview stage tracking
    for col, ctype, default in [
        ("is_daily_pick", "INTEGER", "0"),
        ("picked_at", "TEXT", "NULL"),
        ("interview_round", "TEXT", "NULL"),
        ("interview_round_updated_at", "TEXT", "NULL")
    ]:
        if col not in jobs_cols:
            alter_q = f"ALTER TABLE jobs ADD COLUMN {col} {ctype}"
            if default != "NULL":
                alter_q += f" DEFAULT {default}"
            conn.execute(alter_q)
            conn.commit()

    # Alter users table for recommendations configurations
    for col, ctype, default in [
        ("daily_recs_enabled", "INTEGER", "1"),
        ("daily_recs_min_score", "REAL", "7.5"),
        ("daily_recs_time", "TEXT", "'07:30'"),
        ("last_digest_read_at", "TEXT", "NULL")
    ]:
        if col not in user_cols:
            alter_q = f"ALTER TABLE users ADD COLUMN {col} {ctype}"
            if default != "NULL":
                alter_q += f" DEFAULT {default}"
            conn.execute(alter_q)
            conn.commit()

    # Create interview_prep table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interview_prep (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT UNIQUE,
            quick_questions TEXT,
            deep_questions TEXT,
            created_at TEXT,
            user_id INTEGER DEFAULT 1
        );
    """)
    conn.commit()

    _encrypt_legacy_secrets(conn)
    _run_once(conn, "2026-10-reset-landing-wall-optin", _reset_landing_wall_optin)

    if IS_POSTGRES:
        seq_tables = ["users", "jobs", "contacts", "cover_letters", "application_timeline", "received_emails", "tailored_resumes", "interview_prep"]
        for t in seq_tables:
            try:
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE(max(id), 1)) FROM {t}")
                conn.commit()
            except Exception as e:
                pass



def _ensure_admin(conn) -> None:
    """Seeds the admin account (id 1) and keeps its password off the old 'admin' default.
    ADMIN_PASSWORD, when set, is applied on every startup."""
    from auth import hash_password, verify_password
    from datetime import datetime
    import secrets

    admin_pw = os.getenv("ADMIN_PASSWORD", "").strip()
    row = conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()
    if not row:
        generated = not admin_pw
        pw = admin_pw or secrets.token_urlsafe(12)
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, password_hash, created_at) VALUES (1, ?, ?, ?)",
            ("admin", hash_password(pw), datetime.now().isoformat())
        )
        conn.commit()
        if generated:
            print(f"Created admin account. Username: admin  Password: {pw}  (set ADMIN_PASSWORD to choose your own)")
        return

    current_hash = row[0] or ""
    if admin_pw:
        if not verify_password(admin_pw, current_hash):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = 1", (hash_password(admin_pw),))
            conn.commit()
    elif verify_password("admin", current_hash):
        pw = secrets.token_urlsafe(12)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = 1", (hash_password(pw),))
        conn.commit()
        print(f"Admin still had the default password 'admin'; it has been replaced. New password: {pw}  "
              f"(set ADMIN_PASSWORD to choose your own)")


def _run_once(conn, key: str, fn) -> None:
    """Runs a one-time data fix and records it in app_meta so it never repeats."""
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    if conn.execute("SELECT 1 FROM app_meta WHERE key = ?", (key,)).fetchone():
        return
    fn(conn)
    from datetime import datetime
    try:
        conn.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (key, datetime.now().isoformat()))
        conn.commit()
    except Exception:
        # Another worker recorded it at the same moment; the fix itself is idempotent
        conn.rollback()


def _reset_landing_wall_optin(conn) -> None:
    """Social sign-ins used to be added to the public landing page automatically, and the
    signup checkbox was pre-ticked. Hide everyone once; people can opt back in from settings."""
    conn.execute("UPDATE users SET share_profile = 0 WHERE share_profile = 1")
    conn.commit()


def _encrypt_legacy_secrets(conn) -> None:
    """One-time upgrade: encrypt credentials that were stored in plain text."""
    from crypto_util import encrypt_secret, ENC_PREFIX
    rows = conn.execute("SELECT id, imap_password, gemini_api_key FROM users").fetchall()
    for r in rows:
        uid, imap_pw, gem_key = r[0], r[1], r[2]
        updates = {}
        if imap_pw and not imap_pw.startswith(ENC_PREFIX):
            updates["imap_password"] = encrypt_secret(imap_pw)
        if gem_key and not gem_key.startswith(ENC_PREFIX):
            updates["gemini_api_key"] = encrypt_secret(gem_key)
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*updates.values(), uid))
            conn.commit()


def get_user_secrets(conn, user_id: int) -> dict:
    """Decrypted imap_email / imap_password / gemini_api_key for one user."""
    from crypto_util import decrypt_secret
    row = conn.execute("SELECT imap_email, imap_password, gemini_api_key FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return {"imap_email": "", "imap_password": "", "gemini_api_key": ""}
    return {
        "imap_email": row[0] or "",
        "imap_password": decrypt_secret(row[1] or ""),
        "gemini_api_key": decrypt_secret(row[2] or ""),
    }


_migrated = set()


def init_db(db_path: str = DB_PATH):
    conn = get_conn(db_path)
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

        CREATE TABLE IF NOT EXISTS password_resets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            token_hash    TEXT UNIQUE,
            expires_at    TEXT,
            used_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS tailored_resumes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT UNIQUE,
            resume_content TEXT,
            created_at    TEXT
        );
    """)
    conn.commit()
    if db_path not in _migrated:
        migrate_db(conn)
        _migrated.add(db_path)
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
