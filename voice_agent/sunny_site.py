"""Talking to the Sunny-Side website from the agent.

Phone calls have no browser to report back, so the agent does it: it trades a caller's code for
who they are, and posts the transcript when the call ends. Requests carry the shared secret
SUNNY_AGENT_SECRET, which must match the one set on the website (Render).
"""
import logging
import os

import aiohttp

logger = logging.getLogger("sunny-site")

DEFAULT_SITE = "https://pm-job-hunter-qfva.onrender.com"


def site_url() -> str:
    return (os.getenv("SUNNY_SITE_URL") or DEFAULT_SITE).strip().rstrip("/")


def configured() -> bool:
    return bool((os.getenv("SUNNY_AGENT_SECRET") or "").strip())


async def post(path: str, body: dict, timeout: float = 60.0) -> tuple[int, dict]:
    """(status, json). A free Render instance can take most of a minute to wake, hence the long timeout.
    Returns (0, {}) when the site can't be reached."""
    secret = (os.getenv("SUNNY_AGENT_SECRET") or "").strip()
    if not secret:
        logger.warning("SUNNY_AGENT_SECRET isn't set; can't reach the website")
        return 0, {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.post(site_url() + path, json=body,
                                 headers={"Authorization": f"Bearer {secret}"}) as res:
                try:
                    data = await res.json(content_type=None)
                except Exception:
                    data = {}
                return res.status, data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("website request %s failed: %s", path, e)
        return 0, {}
