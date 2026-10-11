"""YouTube videos for the skill gaps on a job: the skills the job description asks for that the resume lacks.

Videos come from the YouTube Data API when YOUTUBE_API_KEY is set. Results are saved per skill in the
skill_videos table and shared by every job and user, because the search only depends on the skill name.
Without a key, or when YouTube fails, each gap gets a plain YouTube search link instead."""
import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import requests

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_PER_GAP = 2
MAX_GAPS = 4            # each search costs 100 of the 10,000 free daily quota units
CACHE_DAYS = 30
TIMEOUT = 8


def api_key() -> str:
    return os.getenv("YOUTUBE_API_KEY", "").strip()


def search_link(skill: str) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(f"{skill} tutorial")


def _skill_key(skill: str) -> str:
    return " ".join(skill.lower().split())


def search_youtube(skill: str, key: str) -> list[dict] | None:
    """Top tutorial videos for a skill, or None if YouTube could not be reached."""
    try:
        r = requests.get(SEARCH_URL, timeout=TIMEOUT, params={
            "part": "snippet", "type": "video", "maxResults": VIDEOS_PER_GAP,
            "q": f"{skill} tutorial", "relevanceLanguage": "en", "safeSearch": "strict",
            "videoEmbeddable": "true", "key": key,
        })
        if r.status_code != 200:
            log.warning("YouTube search for %r failed: %s %s", skill, r.status_code, r.text[:200])
            return None
        items = r.json().get("items") or []
    except (requests.RequestException, ValueError) as e:
        log.warning("YouTube search for %r failed: %s", skill, e)
        return None
    videos = []
    for it in items:
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid:
            continue
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        videos.append({
            "id": vid,
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "thumb": thumb,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return videos[:VIDEOS_PER_GAP]


def _cached(conn, skill_key: str) -> list[dict] | None:
    row = conn.execute("SELECT videos, fetched_at FROM skill_videos WHERE skill_key = ?", (skill_key,)).fetchone()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row[1]) < datetime.now() - timedelta(days=CACHE_DAYS):
            return None
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _save(conn, skill_key: str, videos: list[dict]) -> None:
    conn.execute("DELETE FROM skill_videos WHERE skill_key = ?", (skill_key,))
    conn.execute("INSERT INTO skill_videos (skill_key, videos, fetched_at) VALUES (?, ?, ?)",
                 (skill_key, json.dumps(videos), datetime.now().isoformat()))
    conn.commit()


def videos_for_gaps(conn, gaps: list[str]) -> list[dict]:
    """One entry per gap (up to MAX_GAPS): the skill, its videos, and a search link for more."""
    key = api_key()
    out, seen = [], set()
    for skill in gaps:
        if not isinstance(skill, str) or not skill.strip():
            continue
        sk = _skill_key(skill)
        if sk in seen:
            continue
        seen.add(sk)
        videos = _cached(conn, sk)
        if videos is None and key:
            videos = search_youtube(skill, key)
            if videos is not None:
                _save(conn, sk, videos)
        out.append({"skill": skill.strip(), "videos": videos or [], "more": search_link(skill.strip())})
        if len(out) >= MAX_GAPS:
            break
    return out
