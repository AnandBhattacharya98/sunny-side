"""The Progress page's numbers, and calendar invites for interviews.

Everything is computed from the user's own jobs and their timeline, so it
needs no extra tracking."""
import re
from datetime import datetime, timedelta, timezone

# How far along the pipeline each status is. Rejected is a stage too: it means an answer came back.
STAGES = ["saved", "shortlisted", "applied", "interviewing", "offer"]
_RANK = {"new": 0, "scored": 0, "ready": 0, "whatsapp": 0, "archived": 0,
         "shortlisted": 1, "applied": 2, "interviewing": 3, "offer": 4}
_MOVE = re.compile(r"(?:→|->)\s*([a-z]+)")

SOURCE_LABELS = {"linkedin": "LinkedIn", "direct": "Company sites", "remotive": "Remotive", "jobicy": "Jobicy",
                 "arbeitnow": "Arbeitnow", "whatsapp": "Self-import", "gmail": "Email", "manual": "Added by you"}


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_progress(conn, user_id: int, now: datetime | None = None, weeks: int = 8) -> dict:
    now = now or datetime.now(timezone.utc)
    jobs = conn.execute("SELECT job_id, status, source FROM jobs WHERE user_id = ?", (user_id,)).fetchall()
    moves = conn.execute(
        "SELECT t.job_id, t.event, t.created_at FROM application_timeline t "
        "JOIN jobs j ON j.job_id = t.job_id WHERE j.user_id = ?", (user_id,)).fetchall()

    reached = {}      # job_id -> furthest stage rank
    rejected = set()
    applied_at = {}   # job_id -> first time it moved to applied
    for job_id, status, _ in jobs:
        reached[job_id] = _RANK.get(status, 0)
        if status == "rejected":
            rejected.add(job_id)
    for job_id, event, created_at in moves:
        if job_id not in reached:
            continue
        m = _MOVE.search(event or "")
        if not m:
            continue
        status = m.group(1)
        reached[job_id] = max(reached[job_id], _RANK.get(status, 0))
        if status == "applied":
            when = _parse(created_at)
            if when and (job_id not in applied_at or when < applied_at[job_id]):
                applied_at[job_id] = when
    # A rejection after applying counts as having applied.
    for job_id in rejected:
        if reached[job_id] < 2 and job_id in applied_at:
            reached[job_id] = 2

    counts = [sum(1 for r in reached.values() if r >= i) for i in range(len(STAGES))]
    funnel = []
    for i, stage in enumerate(STAGES):
        prev = counts[i - 1] if i else None
        funnel.append({"stage": stage, "count": counts[i],
                       "rate": round(100 * counts[i] / prev) if prev else None})

    # Reply rate: applications that led to an interview, an offer or a rejection.
    by_source = {}
    for job_id, status, source in jobs:
        if reached[job_id] < 2:
            continue
        key = source or "manual"
        if job_id.startswith("wa_"):
            key = "whatsapp"
        elif job_id.startswith("auto_"):
            key = "gmail"
        row = by_source.setdefault(key, {"source": key, "label": SOURCE_LABELS.get(key, key.title()), "applied": 0, "replied": 0})
        row["applied"] += 1
        if reached[job_id] >= 3 or job_id in rejected:
            row["replied"] += 1
    sources = sorted(by_source.values(), key=lambda r: -r["applied"])
    for r in sources:
        r["rate"] = round(100 * r["replied"] / r["applied"]) if r["applied"] else 0

    # Applications per week, oldest first, for the last `weeks` weeks.
    start_of_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_starts = [start_of_week - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    per_week = [0] * weeks
    for when in applied_at.values():
        idx = (when - week_starts[0]).days // 7
        if 0 <= idx < weeks:
            per_week[idx] += 1
    total_applied = counts[2]
    replied = sum(r["replied"] for r in sources)
    return {
        "funnel": funnel,
        "rejected": len(rejected),
        "sources": sources,
        "weeks": [{"start": ws.date().isoformat(), "applied": n} for ws, n in zip(week_starts, per_week)],
        "total_applied": total_applied,
        "reply_rate": round(100 * replied / total_applied) if total_applied else None,
    }


def upcoming_interviews(conn, user_id: int, now: datetime | None = None, within_days: int = 30) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT job_id, title, company, interview_round, interview_at FROM jobs "
        "WHERE user_id = ? AND interview_at IS NOT NULL AND status NOT IN ('rejected', 'archived')", (user_id,)).fetchall()
    out = []
    for job_id, title, company, round_name, at in rows:
        when = _parse(at)
        if when and now - timedelta(hours=2) <= when <= now + timedelta(days=within_days):
            out.append({"job_id": job_id, "title": title, "company": company, "round": round_name or "Interview",
                        "at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "when": when})
    out.sort(key=lambda r: r["when"])
    return out


def _ics_text(value: str) -> str:
    value = (value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return re.sub(r"\r?\n", lambda m: "\\n", value)


def _fold(line: str) -> str:
    """RFC 5545 lines are folded at 75 octets."""
    out, raw = [], line.encode("utf-8")
    while len(raw) > 75:
        cut = 75
        while cut and (raw[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def interview_ics(job: dict, at: datetime, minutes: int = 60, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    stamp = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    round_name = job.get("interview_round") or "Interview"
    summary = f"{round_name}: {job.get('title') or 'Interview'} at {job.get('company') or ''}".strip()
    desc = "Added from Sunny Side."
    url = job.get("url") or ""
    if url:
        desc += f"\nPosting: {url}"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Sunny Side//Interviews//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{re.sub(r'[^A-Za-z0-9_-]', '', job.get('job_id', 'job'))}-{stamp(at)}@sunny-side",
        f"DTSTAMP:{stamp(now)}", f"DTSTART:{stamp(at)}", f"DTEND:{stamp(at + timedelta(minutes=minutes))}",
        f"SUMMARY:{_ics_text(summary)}", f"DESCRIPTION:{_ics_text(desc)}",
    ]
    if url.startswith(("http://", "https://")):
        lines.append(f"URL:{url}")
    for trigger, label in (("-P1D", "tomorrow"), ("-PT1H", "in an hour")):
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(summary)} {label}", f"TRIGGER:{trigger}", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
