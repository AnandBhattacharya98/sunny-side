"""Call log: every call a user makes or logs, plus the practice recruiter-screen voice call.

Three ways a row gets into `calls`:
  1. The Calls page's practice call. The browser joins a LiveKit room, a LiveKit agent
     (voice_agent/agent.py) plays the recruiter, and the browser logs the call when it ends.
  2. curl or Postman: POST /api/calls with the user's personal key in an X-API-Key header.
  3. The page itself (signed in, with the usual CSRF header).

The personal API key is shown once and only its SHA-256 hash is stored, like a password.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone

import db

log = logging.getLogger("calls")

STATUSES = ("completed", "missed", "voicemail", "failed")
KINDS = ("logged", "practice")
API_KEY_PREFIX = "ss_"
MAX_NAME = 200
MAX_PHONE = 30
MAX_SUMMARY = 5000
MAX_TRANSCRIPT = 40000
MAX_DURATION = 24 * 3600

# The LiveKit agent the practice call dispatches. Must match agent_name in voice_agent/agent.py.
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "sunny-screener").strip() or "sunny-screener"
ROOM_TOKEN_TTL = 15 * 60  # a practice call is a few minutes; the token only has to last until it joins


class CallError(ValueError):
    """A bad request body. The message is safe to show the user."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _text(body: dict, key: str, limit: int, required: bool = False) -> str | None:
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise CallError(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise CallError(f"{key} must be text")
    value = value.strip()
    if len(value) > limit:
        raise CallError(f"{key} must be at most {limit} characters")
    return value


def validate(body) -> dict:
    """Checks a POST /api/calls body and returns the clean fields. Raises CallError."""
    if not isinstance(body, dict):
        raise CallError("Send a JSON object")
    name = _text(body, "caller_name", MAX_NAME, required=True)
    phone = _text(body, "phone_number", MAX_PHONE, required=True)
    if len(phone) < 3:
        raise CallError("phone_number must be at least 3 characters")
    status = body.get("status") or "completed"
    if status not in STATUSES:
        raise CallError(f"status must be one of: {', '.join(STATUSES)}")
    duration = body.get("duration_seconds", 0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration != int(duration):
        raise CallError("duration_seconds must be a whole number")
    duration = int(duration)
    if not 0 <= duration <= MAX_DURATION:
        raise CallError("duration_seconds must be between 0 and 86400")
    return {
        "caller_name": name,
        "phone_number": phone,
        "status": status,
        "duration_seconds": duration,
        "summary": _text(body, "summary", MAX_SUMMARY),
    }


def _row(r) -> dict:
    return {
        "id": r["id"],
        "caller_name": r["caller_name"],
        "phone_number": r["phone_number"],
        "status": r["status"],
        "duration_seconds": r["duration_seconds"] or 0,
        "summary": r["summary"],
        "kind": r["kind"] or "logged",
        "has_transcript": bool(r["transcript"]),
        "created_at": r["created_at"],
    }


_COLUMNS = "id, caller_name, phone_number, status, duration_seconds, summary, kind, transcript, created_at"


def list_calls(conn, user_id: int, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM calls WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [_row(r) for r in rows]


def get_call(conn, user_id: int, call_id: int) -> dict | None:
    r = conn.execute(f"SELECT {_COLUMNS} FROM calls WHERE user_id = ? AND id = ?", (user_id, call_id)).fetchone()
    if not r:
        return None
    out = _row(r)
    out["transcript"] = json.loads(r["transcript"]) if r["transcript"] else []
    return out


def add_call(conn, user_id: int, call: dict, kind: str = "logged", transcript: list | None = None) -> dict:
    sql = ("INSERT INTO calls (user_id, caller_name, phone_number, status, duration_seconds, summary, kind, "
           "transcript, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    params = (user_id, call["caller_name"], call["phone_number"], call["status"], call["duration_seconds"],
              call.get("summary"), kind, json.dumps(transcript) if transcript else None, now_iso())
    if db.IS_POSTGRES:
        call_id = conn.execute(sql + " RETURNING id", params).fetchone()[0]
    else:
        call_id = conn.execute(sql, params).lastrowid
    conn.commit()
    return get_call(conn, user_id, call_id)


def set_summary(conn, user_id: int, call_id: int, summary: str) -> None:
    conn.execute("UPDATE calls SET summary = ? WHERE user_id = ? AND id = ?", (summary[:MAX_SUMMARY], user_id, call_id))
    conn.commit()


# ── Personal API key (for curl and Postman) ────────────────────────────────

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key(conn, user_id: int) -> str:
    """Makes a new key, replacing any old one. Only the hash is stored, so the caller must show it now."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    conn.execute("UPDATE users SET calls_api_key_hash = ?, calls_api_key_hint = ? WHERE id = ?",
                 (_hash_key(key), key[-4:], user_id))
    conn.commit()
    return key


def revoke_api_key(conn, user_id: int) -> None:
    conn.execute("UPDATE users SET calls_api_key_hash = NULL, calls_api_key_hint = NULL WHERE id = ?", (user_id,))
    conn.commit()


def api_key_hint(conn, user_id: int) -> str | None:
    row = conn.execute("SELECT calls_api_key_hint FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row and row[0] else None


def user_for_api_key(conn, key: str) -> int | None:
    if not key or not key.startswith(API_KEY_PREFIX) or len(key) > 100:
        return None
    digest = _hash_key(key)
    row = conn.execute("SELECT id, calls_api_key_hash FROM users WHERE calls_api_key_hash = ?", (digest,)).fetchone()
    if row and hmac.compare_digest(row[1] or "", digest):
        return row[0]
    return None


# ── Practice call: a LiveKit room with the recruiter agent ─────────────────

def livekit_settings() -> tuple[str, str, str]:
    """(ws url, api key, api secret). The same keys Sunny's LiveKit voice uses."""
    return ((os.getenv("LIVEKIT_URL") or "").strip(),
            (os.getenv("LIVEKIT_API_KEY") or "").strip(),
            (os.getenv("LIVEKIT_API_SECRET") or "").strip())


def practice_configured() -> bool:
    url, key, secret = livekit_settings()
    return bool(url and key and secret)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def room_token(api_key: str, api_secret: str, *, identity: str, name: str, room: str,
               agent_metadata: dict, ttl: int = ROOM_TOKEN_TTL) -> str:
    """A LiveKit join token that also dispatches the recruiter agent into the room.

    Same claims as livekit-api's AccessToken(...).with_grants(...).with_room_config(...).
    """
    now = int(time.time())
    claims = {
        "iss": api_key, "sub": identity, "name": name, "nbf": now - 10, "exp": now + ttl,
        "video": {"roomJoin": True, "room": room, "canPublish": True, "canSubscribe": True,
                  "canPublishData": True},
        "roomConfig": {"agents": [{"agentName": AGENT_NAME,
                                   "metadata": json.dumps(agent_metadata, separators=(",", ":"))}]},
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    sig = hmac.new(api_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def clean_transcript(items) -> list[dict]:
    """[{role: "agent"|"user", text}] from the browser, trimmed to a sane size."""
    out, total = [], 0
    if not isinstance(items, list):
        return out
    for it in items[:400]:
        if not isinstance(it, dict):
            continue
        role = "agent" if it.get("role") == "agent" else "user"
        text = " ".join(str(it.get("text") or "").split())[:2000]
        if not text:
            continue
        total += len(text)
        if total > MAX_TRANSCRIPT:
            break
        out.append({"role": role, "text": text})
    return out


def fallback_summary(transcript: list[dict], role: str) -> str:
    turns = sum(1 for t in transcript if t["role"] == "user")
    what = f"Practice recruiter screen for {role}" if role else "Practice recruiter screen"
    if not turns:
        return f"{what}. You didn't answer any questions."
    return f"{what}. You answered {turns} time{'s' if turns != 1 else ''}."


def summarize_practice(transcript: list[dict], role: str, api_key: str | None) -> str:
    """A short recap plus one strength and one thing to improve. Falls back to a plain line."""
    if not any(t["role"] == "user" for t in transcript):
        return fallback_summary(transcript, role)
    lines = "\n".join(f"{'Recruiter' if t['role'] == 'agent' else 'Candidate'}: {t['text']}" for t in transcript)
    prompt = (
        "You are reviewing a practice recruiter screening call for a job seeker"
        + (f" targeting {role}" if role else "") + ".\n"
        "Write at most 3 short sentences in plain text, no lists or markdown: what the candidate said about "
        "their background and goals, one thing they did well, and one specific thing to improve.\n\n"
        f"Transcript:\n{lines[:12000]}"
    )
    try:
        from ai_engine import _call_gemini
        text = " ".join(_call_gemini(prompt, api_key=api_key).split())
        return text[:MAX_SUMMARY] or fallback_summary(transcript, role)
    except Exception as e:  # no key, timeout, quota: the call is still logged
        log.info("practice summary fell back: %s", e)
        return fallback_summary(transcript, role)
