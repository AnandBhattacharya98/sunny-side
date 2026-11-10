"""Phone practice: the recruiter-screen agent on a real phone call, through LiveKit SIP.

Two ways in:
  1. Dial in. The user rings the practice number (LIVEKIT_SIP_NUMBER). A LiveKit dispatch rule
     sends the call to the agent, which asks for the six-digit code shown on the Calls page
     (typed on the keypad or said out loud) and trades it here for who is calling.
  2. Call me. The user types their number on the Calls page. The server creates a room, sends
     the agent into it with the number to ring, and the agent dials out over
     LIVEKIT_SIP_OUTBOUND_TRUNK_ID.

When the call ends the agent posts the transcript to /api/agent/calls/end, signed with the
shared SUNNY_AGENT_SECRET, and it is logged like a browser practice call.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import requests
from itsdangerous import BadSignature, URLSafeTimedSerializer

import calls as call_log

CODE_TTL_MINUTES = 10
CODE_DIGITS = 6
CALL_REF_MAX_AGE = 3 * 3600  # a call has to end within three hours of starting to be logged
ROOM_PREFIX_IN = "phone-in-"
ROOM_PREFIX_OUT = "phone-out-"
DEFAULT_COUNTRIES = "+91,+1,+44"
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class PhoneError(ValueError):
    """A bad request. The message is safe to show the user."""


# ── Settings ───────────────────────────────────────────────────────────────

def dial_in_number() -> str:
    """The number users ring, as it should be shown on the page (e.g. "+1 415 555 0100")."""
    return " ".join((os.getenv("LIVEKIT_SIP_NUMBER") or "").split())[:40]


def outbound_trunk() -> str:
    return (os.getenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID") or "").strip()


def agent_secret() -> str:
    return (os.getenv("SUNNY_AGENT_SECRET") or "").strip()


def dial_in_ready() -> bool:
    return bool(dial_in_number() and agent_secret() and call_log.practice_configured())


def call_me_ready() -> bool:
    return bool(outbound_trunk() and agent_secret() and call_log.practice_configured())


def allowed_prefixes() -> list[str]:
    raw = os.getenv("PHONE_PRACTICE_COUNTRIES", DEFAULT_COUNTRIES)
    return [p.strip() for p in raw.split(",") if p.strip().startswith("+")]


def agent_authorized(header: str | None) -> bool:
    """True when the request carries `Authorization: Bearer <SUNNY_AGENT_SECRET>`."""
    secret = agent_secret()
    if not secret or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), secret.encode())


# ── Phone numbers ──────────────────────────────────────────────────────────

def normalize_phone(raw) -> str:
    """'+91 98765-43210' -> '+919876543210'. Raises PhoneError for anything that isn't E.164."""
    if not isinstance(raw, str):
        raise PhoneError("Enter your phone number")
    number = re.sub(r"[\s\-().]", "", raw)
    if number.startswith("00"):
        number = "+" + number[2:]
    if not _E164.match(number):
        raise PhoneError("Enter the number with its country code, like +91 98765 43210")
    prefixes = allowed_prefixes()
    if prefixes and not any(number.startswith(p) for p in prefixes):
        raise PhoneError(f"Practice calls can ring numbers starting {', '.join(prefixes)} for now")
    return number


def mask_phone(number: str) -> str:
    """+919876543210 -> Phone •••• 3210, for the call log."""
    digits = re.sub(r"\D", "", number or "")
    return f"Phone •••• {digits[-4:]}" if len(digits) >= 6 else "Phone call"


# ── One-time dial-in codes ─────────────────────────────────────────────────

def _code_hash(code: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode(), f"phone-code:{code}".encode(), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def new_code(conn, user_id: int, app_secret: str, *, role: str = "", company: str = "") -> str:
    """A fresh six-digit code for this user. Any older unused code of theirs stops working."""
    conn.execute("DELETE FROM phone_codes WHERE user_id = ? OR expires_at < ?", (user_id, _iso(_now())))
    expires = _iso(_now() + timedelta(minutes=CODE_TTL_MINUTES))
    for _ in range(20):
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        h = _code_hash(code, app_secret)
        if conn.execute("SELECT 1 FROM phone_codes WHERE code_hash = ?", (h,)).fetchone():
            continue  # another user holds this code right now
        conn.execute(
            "INSERT INTO phone_codes (code_hash, user_id, role, company, expires_at) VALUES (?, ?, ?, ?, ?)",
            (h, user_id, role[:80], company[:80], expires),
        )
        conn.commit()
        return code
    raise PhoneError("Couldn't make a code. Try again.")


def claim_code(conn, code: str, app_secret: str) -> dict | None:
    """Uses up a code. Returns {user_id, role, company} or None if it's wrong, used or expired."""
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != CODE_DIGITS:
        return None
    h = _code_hash(code, app_secret)
    row = conn.execute("SELECT user_id, role, company, expires_at FROM phone_codes WHERE code_hash = ?",
                       (h,)).fetchone()
    conn.execute("DELETE FROM phone_codes WHERE code_hash = ?", (h,))
    conn.commit()
    if not row or row["expires_at"] < _iso(_now()):
        return None
    return {"user_id": row["user_id"], "role": row["role"] or "", "company": row["company"] or ""}


# ── Call references: who a phone call belongs to, carried by the agent ─────

def _serializer(app_secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app_secret, salt="sunny-phone-call")


def make_call_ref(app_secret: str, *, user_id: int, room: str, role: str, phone: str, kind: str = "phone") -> str:
    return _serializer(app_secret).dumps({"u": user_id, "r": room, "role": role, "p": phone, "k": kind})


def read_call_ref(app_secret: str, ref: str, max_age: int = CALL_REF_MAX_AGE) -> dict | None:
    try:
        data = _serializer(app_secret).loads(ref or "", max_age=max_age)
    except BadSignature:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("u"), int) else None


# ── LiveKit server API (Twirp over HTTPS, JSON) ───────────────────────────

def _http_base(ws_url: str) -> str:
    return re.sub(r"^ws", "http", ws_url.rstrip("/"))


def _admin_token(api_key: str, api_secret: str, room: str) -> str:
    now = int(time.time())
    return call_log.sign_jwt({
        "iss": api_key, "sub": "sunny-side-server", "nbf": now - 10, "exp": now + 120,
        "video": {"roomCreate": True, "roomAdmin": True, "room": room},
    }, api_secret)


def _twirp(service: str, method: str, body: dict, room: str, timeout: float = 10.0) -> dict:
    url, key, secret = call_log.livekit_settings()
    res = requests.post(
        f"{_http_base(url)}/twirp/livekit.{service}/{method}",
        data=json.dumps(body),
        headers={"Authorization": f"Bearer {_admin_token(key, secret, room)}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"LiveKit {method} failed: {res.status_code} {res.text[:200]}")
    return res.json() if res.content else {}


def dispatch_agent(room: str, metadata: dict) -> None:
    """Makes the room and sends the practice agent into it with this metadata."""
    _twirp("RoomService", "CreateRoom", {"name": room, "empty_timeout": 120, "departure_timeout": 20}, room)
    _twirp("AgentDispatchService", "CreateDispatch",
           {"room": room, "agent_name": call_log.AGENT_NAME,
            "metadata": json.dumps(metadata, separators=(",", ":"))}, room)
