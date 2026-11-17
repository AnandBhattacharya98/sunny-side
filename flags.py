"""Feature flags: switch parts of the site off (or to admin-only) from the Admin Console, without a redeploy.

Each flag has three states:
  on     everyone gets the feature
  admin  only the admin account sees it (try a new feature on the live site before anyone else)
  off    nobody gets it; its pages send people back to the board and its API answers 404

A flag's state is saved in the feature_flags table. Until someone changes it in the console, the
default comes from the FLAG_<KEY> environment variable (e.g. FLAG_LEARN=off), then from FLAGS below.
New, unfinished features should default to "admin" so they ship dark and get switched on later.

To add a flag: add an entry to FLAGS with the URL prefixes the feature owns, then hide its links with
{% if feature('your_key') %} in templates, or check flags.enabled('your_key', is_admin) in Python.
"""
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

STATES = ("on", "admin", "off")
STATE_LABELS = {"on": "Everyone", "admin": "Admin only", "off": "Off"}
CACHE_SECONDS = 10  # each worker re-reads the table this often, so a switch reaches every worker within seconds


@dataclass(frozen=True)
class Flag:
    label: str
    description: str
    default: str = "on"
    paths: tuple = field(default_factory=tuple)  # URL prefixes this feature owns


FLAGS = {
    "sunny": Flag("Sunny, the board assistant",
                  "The talking assistant on the board (chat, voice and spoken quiz answers).",
                  paths=("/api/voice/",)),
    "career_switch": Flag("Career Switch",
                          "Suggested career pivots with live openings and resume comparison.",
                          paths=("/career-switch", "/api/career-switch")),
    "learn": Flag("Learn",
                  "Short courses and quizzes on the skills a resume is missing.",
                  paths=("/learn", "/api/learn")),
    "calls": Flag("Calls",
                  "Practice recruiter calls, phone practice and the call log.",
                  paths=("/calls", "/api/calls", "/api/agent/")),
}

_cache = {"at": 0.0, "states": {}}
_lock = threading.Lock()


def migrate(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS feature_flags (key TEXT PRIMARY KEY, state TEXT, updated_at TEXT)")
    conn.commit()


def _default(key: str) -> str:
    env = os.getenv("FLAG_" + key.upper(), "").strip().lower()
    return env if env in STATES else FLAGS[key].default


def _saved_states() -> dict:
    """Saved states from the database, cached for a few seconds. On a database error, the last good copy."""
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < CACHE_SECONDS:
            return _cache["states"]
    from db import get_conn, DB_PATH
    try:
        conn = get_conn(DB_PATH)
        try:
            rows = conn.execute("SELECT key, state FROM feature_flags").fetchall()
        finally:
            conn.close()
        states = {r[0]: r[1] for r in rows if r[1] in STATES}
    except Exception:
        states = _cache["states"]
    with _lock:
        _cache.update(at=now, states=states)
    return states


def state(key: str) -> str:
    if key not in FLAGS:
        return "on"
    return _saved_states().get(key) or _default(key)


def enabled(key: str, is_admin: bool = False) -> bool:
    s = state(key)
    return s == "on" or (s == "admin" and is_admin)


def set_state(conn, key: str, new_state: str) -> None:
    if key not in FLAGS:
        raise KeyError(key)
    if new_state not in STATES:
        raise ValueError(new_state)
    now = datetime.now().isoformat()
    if conn.execute("SELECT 1 FROM feature_flags WHERE key = ?", (key,)).fetchone():
        conn.execute("UPDATE feature_flags SET state = ?, updated_at = ? WHERE key = ?", (new_state, now, key))
    else:
        conn.execute("INSERT INTO feature_flags (key, state, updated_at) VALUES (?, ?, ?)", (key, new_state, now))
    conn.commit()
    clear_cache()


def clear_cache() -> None:
    with _lock:
        _cache.update(at=0.0, states={})


def flag_for_path(path: str):
    """The flag that owns a URL path, or None."""
    for key, f in FLAGS.items():
        for prefix in f.paths:
            base = prefix.rstrip("/")
            if path == base or path.startswith(base + "/"):
                return key
    return None


def listing(conn) -> list:
    """Every flag with its current state, for the Admin Console."""
    saved = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT key, state, updated_at FROM feature_flags").fetchall()}
    out = []
    for key, f in FLAGS.items():
        s, updated = saved.get(key, (None, None))
        out.append({"key": key, "label": f.label, "description": f.description,
                    "state": s if s in STATES else _default(key), "updated_at": updated, "default": _default(key)})
    return out
