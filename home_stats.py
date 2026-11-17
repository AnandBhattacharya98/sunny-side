"""Live numbers for the public homepage: roles found lately, who's hiring, and where.

Only public listings Sunny Side found by searching (LinkedIn and company job boards) are counted.
Jobs that came from someone's inbox, a link they pasted or a recruiter post are left out, so the
homepage never hints at what any one person applied to. Companies show on the leaderboard only
when they have at least MIN_COMPANY_ROLES open roles.

Every board keeps its own copy of a job, so roles are counted once per (company, title).
The result is cached for CACHE_SECONDS; the homepage never waits on more than one query.
"""
import hashlib
import math
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

import jobmeta

WINDOW_DAYS = 30
CACHE_SECONDS = 600
MIN_COMPANY_ROLES = 2
MIN_ROLES_TO_SHOW = 10  # below this the page keeps its plain hero instead of showing tiny numbers
PUBLIC_SOURCES = ("linkedin", "direct")
MAX_ROWS = 20000

_FRESHER = re.compile(r"\b(intern(ship)?|fresher|graduate|grad|trainee|entry[- ]level|junior|jr\.?|apprentice|campus)\b", re.I)

# City names as job sites write them, mapped to one label
_CITIES = [
    ("Bengaluru", re.compile(r"bengaluru|bangalore", re.I)),
    ("Hyderabad", re.compile(r"hyderabad|secunderabad", re.I)),
    ("Mumbai", re.compile(r"mumbai|navi mumbai|thane", re.I)),
    ("Pune", re.compile(r"\bpune\b", re.I)),
    ("Gurugram", re.compile(r"gurugram|gurgaon", re.I)),
    ("Noida", re.compile(r"\bnoida\b", re.I)),
    ("Delhi", re.compile(r"\bdelhi\b", re.I)),
    ("Chennai", re.compile(r"chennai", re.I)),
    ("Kolkata", re.compile(r"kolkata", re.I)),
    ("Ahmedabad", re.compile(r"ahmedabad", re.I)),
]

# First match wins, so the more specific families come first
_FAMILIES = [
    ("Design", re.compile(r"\b(design(er)?|ux|ui|figma|illustrat)", re.I)),
    ("Data", re.compile(r"\b(data|analytics?|analyst|machine learning|ml|ai|scientist|bi)\b", re.I)),
    ("Product", re.compile(r"\b(product (manager|owner|lead|management)|pm|apm|program manager)\b", re.I)),
    ("Marketing", re.compile(r"\b(marketing|growth|seo|content|brand|social media|copywriter)\b", re.I)),
    ("Sales", re.compile(r"\b(sales|account (executive|manager)|business development|bdr|sdr|customer success)\b", re.I)),
    ("Finance", re.compile(r"\b(finance|financial|accountant|accounts|audit|tax|treasury|ca)\b", re.I)),
    ("People & HR", re.compile(r"\b(hr|human resources|recruit(er|ing)|talent|people)\b", re.I)),
    ("Operations", re.compile(r"\b(operations|ops|supply chain|logistics|procurement)\b", re.I)),
    ("Engineering", re.compile(r"\b(engineer(ing)?|developer|devops|sre|architect|programmer|software|frontend|backend|full ?stack|qa|sdet)\b", re.I)),
]

MAX_PINS = 400
PIN_SPREAD_KM = 6.0  # pins sit around their area's centre so one city isn't a single dot

# Approximate centres. Job sites give an area or a city, not an office address, so a pin marks the
# area. Specific Bengaluru areas come before the city so "Whitefield, Bengaluru" lands in Whitefield.
_PLACES = [
    ("Whitefield", "Bengaluru", 12.9698, 77.7500, r"whitefield"),
    ("Electronic City", "Bengaluru", 12.8452, 77.6602, r"electronic ?city"),
    ("Koramangala", "Bengaluru", 12.9352, 77.6245, r"koramangala"),
    ("Indiranagar", "Bengaluru", 12.9784, 77.6408, r"indira ?nagar"),
    ("HSR Layout", "Bengaluru", 12.9116, 77.6474, r"hsr"),
    ("Bellandur", "Bengaluru", 12.9257, 77.6764, r"bellandur|outer ring road|\borr\b"),
    ("Marathahalli", "Bengaluru", 12.9569, 77.7011, r"marathahalli"),
    ("Manyata", "Bengaluru", 13.0475, 77.6200, r"manyata|hebbal|nagawara"),
    ("Bengaluru East", "Bengaluru", 12.9900, 77.7000, r"bengaluru east|bangalore east"),
    ("Bengaluru", "Bengaluru", 12.9716, 77.5946, r"bengaluru|bangalore"),
    ("HITEC City", "Hyderabad", 17.4435, 78.3772, r"hitec|madhapur|gachibowli"),
    ("Hyderabad", "Hyderabad", 17.3850, 78.4867, r"hyderabad|secunderabad"),
    ("Navi Mumbai", "Mumbai", 19.0330, 73.0297, r"navi mumbai"),
    ("Thane", "Mumbai", 19.2183, 72.9781, r"\bthane\b"),
    ("Mumbai", "Mumbai", 19.0760, 72.8777, r"mumbai"),
    ("Pune", "Pune", 18.5204, 73.8567, r"\bpune\b|hinjewadi"),
    ("Gurugram", "Gurugram", 28.4595, 77.0266, r"gurugram|gurgaon"),
    ("Noida", "Noida", 28.5355, 77.3910, r"\bnoida\b"),
    ("New Delhi", "Delhi", 28.6139, 77.2090, r"\bdelhi\b"),
    ("Chennai", "Chennai", 13.0827, 80.2707, r"chennai"),
    ("Kolkata", "Kolkata", 22.5726, 88.3639, r"kolkata"),
    ("Ahmedabad", "Ahmedabad", 23.0225, 72.5714, r"ahmedabad"),
    ("Jaipur", "Jaipur", 26.9124, 75.7873, r"jaipur"),
    ("Kochi", "Kochi", 9.9312, 76.2673, r"kochi|cochin"),
    ("Thiruvananthapuram", "Thiruvananthapuram", 8.5241, 76.9366, r"thiruvananthapuram|trivandrum"),
    ("Coimbatore", "Coimbatore", 11.0168, 76.9558, r"coimbatore"),
    ("Chandigarh", "Chandigarh", 30.7333, 76.7794, r"chandigarh|mohali"),
    ("Indore", "Indore", 22.7196, 75.8577, r"indore"),
    ("Bhubaneswar", "Bhubaneswar", 20.2961, 85.8245, r"bhubaneswar"),
    ("Mysuru", "Mysuru", 12.2958, 76.6394, r"mysore|mysuru"),
    ("Mangaluru", "Mangaluru", 12.9141, 74.8560, r"mangalore|mangaluru"),
    ("Nagpur", "Nagpur", 21.1458, 79.0882, r"nagpur"),
    ("Lucknow", "Lucknow", 26.8467, 80.9462, r"lucknow"),
    ("Visakhapatnam", "Visakhapatnam", 17.6868, 83.2185, r"visakhapatnam|vizag"),
    ("Vadodara", "Vadodara", 22.3072, 73.1812, r"vadodara|baroda"),
    ("Surat", "Surat", 21.1702, 72.8311, r"\bsurat\b"),
]
_PLACES = [(a, c, lat, lng, re.compile(rx, re.I)) for a, c, lat, lng, rx in _PLACES]


def place_of(location: str):
    """(area, city, lat, lng) for a job's location text, or None when it names no place we know
    (just "India", or a remote job)."""
    for area, city, lat, lng, pat in _PLACES:
        if pat.search(location or ""):
            return area, city, lat, lng
    return None


def _spread(lat: float, lng: float, key: str):
    """Move a pin a fixed, made-up distance from the area centre, the same for the same company every time."""
    h = hashlib.sha1(key.encode()).digest()
    angle = h[0] / 255 * 2 * math.pi
    dist = (0.25 + 0.75 * (h[1] / 255)) * PIN_SPREAD_KM
    dlat = dist / 111.0 * math.cos(angle)
    dlng = dist / (111.0 * math.cos(math.radians(lat))) * math.sin(angle)
    return round(lat + dlat, 5), round(lng + dlng, 5)


def _pins(seen: dict) -> list:
    """One pin per company per area, newest roles first, at most MAX_PINS pins."""
    pins = {}
    for (ckey, _), (company, title, location, scraped) in seen.items():
        p = place_of(location)
        if not p:
            continue
        area, city, lat, lng = p
        k = (ckey, area)
        if k not in pins:
            plat, plng = _spread(lat, lng, ckey + "|" + area)
            pins[k] = {"company": company, "area": area, "city": city, "lat": plat, "lng": plng, "roles": [], "fresh": scraped}
        pin = pins[k]
        if len(pin["roles"]) < 8:
            pin["roles"].append({"title": title[:90], "fresher": bool(_FRESHER.search(title)), "family": family_of(title)})
        pin["count"] = pin.get("count", 0) + 1
    out = sorted(pins.values(), key=lambda p: p["fresh"], reverse=True)[:MAX_PINS]
    for p in out:
        p.pop("fresh", None)
    return out


_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def city_of(location: str, title: str = "") -> str:
    loc = location or ""
    for name, pat in _CITIES:
        if pat.search(loc):
            return name
    if jobmeta.is_remote(loc, title):
        return "Remote"
    return ""


def family_of(title: str) -> str:
    for name, pat in _FAMILIES:
        if pat.search(title or ""):
            return name
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def compute(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    since = (now - timedelta(days=WINDOW_DAYS)).isoformat()
    week = (now - timedelta(days=7)).isoformat()
    marks = ",".join("?" for _ in PUBLIC_SOURCES)
    rows = conn.execute(
        f"SELECT company, title, location, scraped_at FROM jobs WHERE scraped_at >= ? AND source IN ({marks}) "
        f"ORDER BY scraped_at DESC LIMIT {MAX_ROWS}",
        (since, *PUBLIC_SOURCES),
    ).fetchall()

    seen = {}
    for r in rows:
        company, title, location, scraped = r[0], r[1], r[2], r[3]
        if not company or not title or _norm(company) in ("unknown", "confidential"):
            continue
        key = (_norm(company), _norm(title))
        if key not in seen:  # rows come newest first, so the first copy is the latest sighting
            seen[key] = (company.strip(), title.strip(), location or "", scraped or "")

    by_company = Counter()
    company_label = {}
    cities, families = Counter(), Counter()
    fresher = new_this_week = 0
    for (ckey, _), (company, title, location, scraped) in seen.items():
        by_company[ckey] += 1
        company_label.setdefault(ckey, company)
        if _FRESHER.search(title):
            fresher += 1
        if scraped >= week:
            new_this_week += 1
        c = city_of(location, title)
        if c:
            cities[c] += 1
        f = family_of(title)
        if f:
            families[f] += 1

    top = [{"company": company_label[k], "roles": n} for k, n in by_company.most_common(6) if n >= MIN_COMPANY_ROLES]
    return {
        "roles": len(seen),
        "companies": len(by_company),
        "fresher": fresher,
        "new_this_week": new_this_week,
        "top_companies": top,
        "cities": [{"name": n, "roles": c} for n, c in cities.most_common(6)],
        "families": [{"name": n, "roles": c} for n, c in families.most_common(6)],
        "pins": _pins(seen),
        "window_days": WINDOW_DAYS,
        "show": len(seen) >= MIN_ROLES_TO_SHOW,
        "updated_at": now.isoformat(timespec="minutes"),
    }


def get(conn_factory) -> dict:
    """Cached numbers. conn_factory opens a connection only when the cache is stale.
    On a database error the last good copy is kept (or an empty, hidden result)."""
    now = time.monotonic()
    with _lock:
        if _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    try:
        conn = conn_factory()
        try:
            data = compute(conn)
        finally:
            conn.close()
    except Exception:
        data = _cache["data"] or {"roles": 0, "companies": 0, "fresher": 0, "new_this_week": 0, "top_companies": [],
                                  "cities": [], "families": [], "pins": [], "window_days": WINDOW_DAYS, "show": False, "updated_at": ""}
    with _lock:
        _cache.update(at=now, data=data)
    return data


def clear_cache() -> None:
    with _lock:
        _cache.update(at=0.0, data=None)


# ── The signed-in Map view ──────────────────────────────────────────────────

BOARD_GROUPS = {
    "new": "To review", "scored": "To review", "ready": "To review", "whatsapp": "To review",
    "shortlisted": "Shortlisted", "applied": "Applied", "interviewing": "Interviewing",
    "offer": "Offer", "rejected": "Closed",
}


def board_pins(conn, uid: int):
    """One pin per company per area from this person's own board (archived jobs left out).
    Returns (pins, jobs_without_a_place)."""
    rows = conn.execute(
        "SELECT job_id, title, company, location, status, ai_score FROM jobs "
        "WHERE user_id = ? AND COALESCE(status, 'new') != 'archived' "
        "ORDER BY COALESCE(ai_score, 0) DESC LIMIT 2000", (uid,)).fetchall()
    pins, unplaced = {}, 0
    for r in rows:
        job_id, title, company, location, status, score = r[0], r[1], r[2], r[3], r[4], r[5]
        place = place_of(location or "")
        if not place:
            unplaced += 1
            continue
        area, city, lat, lng = place
        company = (company or "Unknown").strip()
        k = (_norm(company), area)
        if k not in pins:
            plat, plng = _spread(lat, lng, _norm(company) + "|" + area)
            pins[k] = {"company": company, "area": area, "city": city, "lat": plat, "lng": plng, "jobs": []}
        pins[k]["jobs"].append({
            "id": str(job_id), "title": (title or "")[:100],
            "group": BOARD_GROUPS.get(status or "new", "To review"),
            "score": round(float(score), 1) if score else None,
        })
    return list(pins.values()), unplaced


def nearby_openings(conn, uid: int, limit: int = 300) -> list:
    """Public openings (as on the homepage) that aren't already on this person's board."""
    data = get(lambda: conn_factory_passthrough(conn))
    mine = {(_norm(r[0]), _norm(r[1])) for r in conn.execute(
        "SELECT company, title FROM jobs WHERE user_id = ?", (uid,)).fetchall()}
    out = []
    for p in data.get("pins", []):
        roles = [r for r in p["roles"] if (_norm(p["company"]), _norm(r["title"])) not in mine]
        if roles:
            out.append({**p, "roles": roles, "count": len(roles)})
        if len(out) >= limit:
            break
    return out


class _NoClose:
    """Lend an open connection to get() without letting it close it."""
    def __init__(self, conn):
        self._c = conn

    def execute(self, *a, **k):
        return self._c.execute(*a, **k)

    def close(self):
        pass


def conn_factory_passthrough(conn):
    return _NoClose(conn)


def map_places() -> list:
    """Every city and area the map knows, for the illustrated base map's names."""
    out, seen = [], set()
    for area, city, lat, lng, _ in _PLACES:
        if area in seen or area in ("Bengaluru East",):
            continue
        seen.add(area)
        out.append({"name": area, "lat": lat, "lng": lng, "kind": "city" if area == city or area == "New Delhi" else "area"})
    return out
