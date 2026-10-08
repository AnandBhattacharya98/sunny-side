"""
followups.py — Follow-up reminders for applications that have gone quiet, and the weekly recap.

Everything here is scoped to one user_id.
"""

import json
import os
from datetime import datetime, timedelta
from html import escape

FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "7"))
# Stages where the next move is usually the candidate's nudge
FOLLOWUP_STATUSES = ("applied", "interviewing")


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts)[:26])
    except ValueError:
        return None


def stale_applications(conn, uid: int, days: int = FOLLOWUP_DAYS, now: datetime = None) -> list[dict]:
    """Applied or interviewing jobs with no timeline activity or email for `days` days, and not snoozed."""
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT job_id, title, company, status, url, scraped_at, followup_snoozed_until FROM jobs "
        "WHERE user_id = ? AND status IN (?, ?)",
        (uid, *FOLLOWUP_STATUSES),
    ).fetchall()
    out = []
    for r in rows:
        job_id, title, company, status, url, scraped_at, snoozed_until = tuple(r)
        snooze = _parse(snoozed_until)
        if snooze and snooze > now:
            continue
        last_event = conn.execute(
            "SELECT MAX(created_at) FROM application_timeline WHERE job_id = ? AND user_id = ?", (job_id, uid)
        ).fetchone()[0]
        last_email = conn.execute(
            "SELECT MAX(received_at) FROM received_emails WHERE job_id = ? AND user_id = ?", (job_id, uid)
        ).fetchone()[0]
        stamps = [t for t in (_parse(last_event), _parse(last_email), _parse(scraped_at)) if t]
        last = max(stamps) if stamps else None
        if not last:
            continue
        quiet = (now - last).days
        if quiet < days:
            continue
        contact = conn.execute(
            "SELECT name, title, email FROM contacts WHERE job_id = ? AND user_id = ? LIMIT 1", (job_id, uid)
        ).fetchone()
        out.append({
            "job_id": job_id, "title": title, "company": company, "status": status, "url": url,
            "days_quiet": quiet, "last_activity": last.isoformat(),
            "contact_name": contact[0] if contact else "", "contact_email": contact[2] if contact else "",
        })
    out.sort(key=lambda j: j["days_quiet"], reverse=True)
    return out


def snooze(conn, uid: int, job_id: str, days: int = FOLLOWUP_DAYS) -> bool:
    until = (datetime.now() + timedelta(days=days)).isoformat()
    cur = conn.execute("UPDATE jobs SET followup_snoozed_until = ? WHERE job_id = ? AND user_id = ?",
                       (until, job_id, uid))
    conn.commit()
    return bool(getattr(cur, "rowcount", 1))


def _template_followup(job: dict, candidate_name: str) -> dict:
    who = job.get("contact_name") or "Hiring Team"
    stage = "interview" if job.get("status") == "interviewing" else "application"
    sign = candidate_name or ""
    body = (
        f"Hi {who},\n\n"
        f"I wanted to follow up on my {stage} for the {job['title']} role at {job['company']}. "
        f"I'm still very interested in the position and would be glad to share anything else that helps.\n\n"
        f"Is there an update on next steps, or anything you need from me?\n\n"
        f"Thanks for your time,\n{sign}".rstrip()
    )
    return {"subject": f"Following up: {job['title']} {stage}", "body": body, "mode": "template"}


def draft_followup(job: dict, candidate_name: str = "", resume_text: str = "", api_key: str = None) -> dict:
    """A short, polite follow-up email. Uses Gemini when a key is available, a template otherwise."""
    from ai_engine import _call_gemini, get_fallback_gemini_key
    key = api_key or get_fallback_gemini_key()
    if not key:
        return _template_followup(job, candidate_name)
    stage = "after an interview" if job.get("status") == "interviewing" else "after applying"
    prompt = f"""Write a short, warm follow-up email {stage}, sent {job.get('days_quiet', FOLLOWUP_DAYS)} days after the last contact.
Role: {job['title']} at {job['company']}. Recipient: {job.get('contact_name') or 'the hiring team'}.
Candidate name: {candidate_name or '(leave the signature as just "Thanks")'}.
Candidate background, for one specific line at most: {(resume_text or '')[:800]}
Under 110 words. No exaggeration, no made-up facts, no placeholders in brackets.
Reply ONLY with JSON: {{"subject": "...", "body": "..."}}"""
    try:
        data = json.loads(_call_gemini(prompt, response_json=True, api_key=key))
        if isinstance(data, dict) and data.get("subject") and data.get("body"):
            return {"subject": str(data["subject"])[:200], "body": str(data["body"])[:3000], "mode": "gemini"}
    except Exception as e:
        print(f"  [Follow-up draft fallback] {e}")
    return _template_followup(job, candidate_name)


# ── Weekly recap ─────────────────────────────────────────────────────────

def weekly_summary(conn, uid: int, days: int = 7, now: datetime = None) -> dict:
    now = now or datetime.now()
    since = (now - timedelta(days=days)).isoformat()
    events = [r[0] or "" for r in conn.execute(
        "SELECT event FROM application_timeline WHERE user_id = ? AND created_at >= ?", (uid, since)).fetchall()]

    def count(*words):
        return sum(1 for e in events if any(w in e.lower() for w in words))

    applied = count("→ applied", "-> applied", "email sent")
    interviews = count("→ interviewing", "-> interviewing", "interview round")
    offers = count("→ offer", "-> offer")
    rejections = count("→ rejected", "-> rejected")
    new_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND scraped_at >= ?",
                            (uid, since)).fetchone()[0]
    replies = conn.execute("SELECT COUNT(*) FROM received_emails WHERE user_id = ? AND received_at >= ?",
                           (uid, since)).fetchone()[0]

    # Response rate by source, over every application so far (a week is too few to compare)
    by_source = {}
    for source, status, job_id in conn.execute(
            "SELECT source, status, job_id FROM jobs WHERE user_id = ? AND status IN "
            "('applied', 'interviewing', 'offer', 'rejected')", (uid,)).fetchall():
        s = by_source.setdefault(source or "other", {"source": source or "other", "applied": 0, "responses": 0})
        s["applied"] += 1
        heard = status in ("interviewing", "offer", "rejected") or conn.execute(
            "SELECT 1 FROM received_emails WHERE job_id = ? AND user_id = ? LIMIT 1", (job_id, uid)).fetchone()
        if heard:
            s["responses"] += 1
    sources = sorted(by_source.values(), key=lambda s: s["applied"], reverse=True)
    for s in sources:
        s["rate"] = round(100 * s["responses"] / s["applied"]) if s["applied"] else 0

    return {
        "since": since, "days": days, "new_jobs": new_jobs, "applied": applied, "interviews": interviews,
        "offers": offers, "rejections": rejections, "replies": replies, "sources": sources[:5],
        "follow_ups": stale_applications(conn, uid, now=now),
    }


def summary_sentence(s: dict) -> str:
    if not (s["applied"] or s["interviews"] or s["offers"] or s["replies"]):
        line = f"A quiet week: {s['new_jobs']} new job{'s' if s['new_jobs'] != 1 else ''} landed on your board, but no applications went out."
    else:
        line = (f"This week you applied to {s['applied']}, had {s['interviews']} interview update"
                f"{'s' if s['interviews'] != 1 else ''} and got {s['replies']} repl{'ies' if s['replies'] != 1 else 'y'}.")
        if s["offers"]:
            line += f" And {s['offers']} offer{'s' if s['offers'] != 1 else ''}!"
    if s["follow_ups"]:
        n = len(s["follow_ups"])
        line += f" {n} application{'s have' if n != 1 else ' has'} gone quiet and could use a follow-up."
    return line


def weekly_email_html(s: dict, name: str = "", board_url: str = "") -> str:
    def stat(label, value):
        return (f'<td style="padding:12px;text-align:center;border:1px solid #e5e3db;border-radius:8px;">'
                f'<div style="font-size:22px;font-weight:700;color:#1a1a18;">{value}</div>'
                f'<div style="font-size:12px;color:#5f5e5a;">{label}</div></td>')
    rows = "".join(
        f"<tr><td style='padding:4px 0;'>{escape(x['source'])}</td><td style='text-align:right;'>{x['applied']} applied</td>"
        f"<td style='text-align:right;'>{x['rate']}% heard back</td></tr>" for x in s["sources"])
    quiet = "".join(
        f"<li>{escape(j['title'])} at {escape(j['company'])}, quiet for {j['days_quiet']} days</li>" for j in s["follow_ups"][:5])
    hello = f"Hi {escape(name.split(' ')[0])}," if name else "Hi,"
    link = (f'<p><a href="{escape(board_url)}" style="display:inline-block;padding:10px 18px;background:#534AB7;color:#fff;'
            f'border-radius:8px;text-decoration:none;font-size:14px;">Open your board</a></p>') if board_url else ""
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1a18;">
<h1 style="font-size:20px;font-weight:600;margin:0 0 6px;">Your week in job hunting</h1>
<p style="font-size:14px;line-height:1.6;color:#3d3d3a;">{hello} {escape(summary_sentence(s))}</p>
<table style="width:100%;border-collapse:separate;border-spacing:6px;margin:12px 0;"><tr>
{stat('new jobs', s['new_jobs'])}{stat('applied', s['applied'])}{stat('interviews', s['interviews'])}{stat('replies', s['replies'])}
</tr></table>
{('<h2 style="font-size:15px;margin:18px 0 6px;">Worth a follow-up</h2><ul style="font-size:14px;color:#3d3d3a;padding-left:18px;">' + quiet + '</ul>') if quiet else ''}
{('<h2 style="font-size:15px;margin:18px 0 6px;">Where replies come from</h2><table style="width:100%;font-size:13px;color:#3d3d3a;">' + rows + '</table>') if rows else ''}
{link}
<p style="font-size:12px;color:#888780;border-top:1px solid #e5e3db;padding-top:12px;margin-top:20px;">You get this because daily recommendations are on in your settings.</p>
</div>"""
