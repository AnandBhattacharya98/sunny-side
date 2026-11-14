"""
security.py: shared guards used across the app.

- Response headers (CSP, HSTS, framing, sniffing, referrer, permissions).
- safe_url: keeps javascript:/data: links out of href attributes.
- Outbound fetch guard (SSRF): pages the server fetches on a user's behalf must be public
  http(s) addresses, never the server itself, the private network or cloud metadata.
- Resume upload parsing with type checks and size/page caps.

Nothing here makes the site uncrackable; each guard removes one class of attack.
"""

import io
import ipaddress
import os
import socket
from urllib.parse import urlparse, urljoin

import requests

# ── Response headers ──────────────────────────────────────────────────────
# The pages use inline scripts and onclick handlers, so script-src keeps 'unsafe-inline'.
# The policy still blocks scripts from any other site, plugins, <base> hijacking,
# framing (clickjacking) and form posts to other sites.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    "img-src 'self' data: blob: https:",
    "media-src 'self' data: blob: https:",
    # LiveKit voice connects to its own cloud over https and wss
    "connect-src 'self' https: wss:",
    "worker-src 'self' blob:",
    "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "manifest-src 'self'",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Sunny's voice needs the microphone on this site; nothing else gets device access.
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
    "X-Permitted-Cross-Domain-Policies": "none",
}
HSTS = "max-age=31536000; includeSubDomains"


def apply_security_headers(response, is_https: bool):
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if is_https:
        response.headers.setdefault("Strict-Transport-Security", HSTS)
    # Don't advertise the server software
    response.headers.pop("Server", None)
    return response


# ── Links ─────────────────────────────────────────────────────────────────
SAFE_LINK_SCHEMES = ("http", "https", "mailto", "tel")


def safe_url(value) -> str:
    """The URL if it's an http(s)/mailto/tel link (or a same-site path), else '#'."""
    url = str(value or "").strip()
    if not url:
        return "#"
    # Browsers ignore tabs/newlines inside schemes ("java\tscript:"), so check without them
    compact = "".join(ch for ch in url if ch not in "\t\r\n ").lower()
    if compact.startswith("/") and not compact.startswith("//"):
        return url
    scheme = compact.split(":", 1)[0] if ":" in compact else ""
    if scheme in SAFE_LINK_SCHEMES:
        return url
    if not scheme and "." in compact.split("/", 1)[0]:
        return "https://" + url  # "www.acme.com/jobs" → a real link, never a relative path
    return "#"


# ── Outbound fetches (SSRF guard) ─────────────────────────────────────────
class UnsafeURL(ValueError):
    pass


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_private or addr.is_loopback or addr.is_link_local
                                   or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def check_public_url(url: str) -> str:
    """Raises UnsafeURL unless url is http(s) on a standard port and every address it resolves to is public."""
    if os.getenv("ALLOW_PRIVATE_FETCH") == "1":  # local testing against a dev server only
        return url
    try:
        u = urlparse(str(url).strip())
    except ValueError:
        raise UnsafeURL("bad url")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL("only http(s) links can be fetched")
    if u.username or u.password:
        raise UnsafeURL("links with a login in them can't be fetched")
    if u.port not in (None, 80, 443, 8080, 8443):
        raise UnsafeURL("unusual port")
    host = u.hostname.rstrip(".").lower()
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeURL("internal host")
    try:
        ips = _resolve(host, u.port or (443 if u.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError):
        raise UnsafeURL("host not found")
    if not ips or not all(_ip_is_public(ip) for ip in ips):
        raise UnsafeURL("private address")
    return url


def _resolve(host: str, port: int) -> list[str]:
    """Every address a host name points at (tests replace this to stay offline)."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


FETCH_MAX_BYTES = 3 * 1024 * 1024


def safe_get(url: str, max_redirects: int = 5, max_bytes: int = FETCH_MAX_BYTES, **kwargs) -> requests.Response:
    """requests.get that refuses private/internal targets (re-checking every redirect hop)
    and stops reading after max_bytes, so a huge page can't exhaust the server's memory."""
    kwargs.setdefault("timeout", 10)
    kwargs["allow_redirects"] = False
    kwargs["stream"] = True
    current = url
    for _ in range(max_redirects + 1):
        check_public_url(current)
        resp = requests.get(current, **kwargs)
        if getattr(resp, "is_redirect", False) and resp.headers.get("Location"):
            current = urljoin(current, resp.headers["Location"])
            resp.close()
            continue
        if not hasattr(resp, "iter_content"):
            return resp
        body = b""
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) >= max_bytes:
                break
        resp.close()
        resp._content = body[:max_bytes]
        resp._content_consumed = True
        return resp
    raise UnsafeURL("too many redirects")


# ── Resume uploads ────────────────────────────────────────────────────────
RESUME_MAX_BYTES = int(os.getenv("RESUME_MAX_MB", "5")) * 1024 * 1024
RESUME_MAX_PAGES = 30
RESUME_MAX_CHARS = 60_000
UNSUPPORTED_RESUME = "Unsupported file format. Please upload PDF, TXT, or DOCX."


class ResumeError(ValueError):
    pass


def extract_resume_text(file) -> str:
    """Text from an uploaded PDF, DOCX or TXT resume. Raises ResumeError with a message safe to show."""
    name = (file.filename or "").lower()
    data = file.read(RESUME_MAX_BYTES + 1)
    if len(data) > RESUME_MAX_BYTES:
        raise ResumeError(f"That file is too big. Resumes can be up to {RESUME_MAX_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise ResumeError("That file is empty.")
    if name.endswith(".txt"):
        if b"\x00" in data[:4096]:
            raise ResumeError("That doesn't look like a text file.")
        text = data.decode("utf-8", errors="ignore")
    elif name.endswith(".pdf"):
        if not data.lstrip()[:5].startswith(b"%PDF"):
            raise ResumeError("That doesn't look like a PDF.")
        import pypdf
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ResumeError("That PDF is password-protected. Upload a copy without a password.")
            parts = []
            for page in reader.pages[:RESUME_MAX_PAGES]:
                parts.append(page.extract_text() or "")
                if sum(len(p) for p in parts) > RESUME_MAX_CHARS:
                    break
            text = "\n".join(parts)
        except ResumeError:
            raise
        except Exception as e:
            print(f"[resume upload] PDF parse failed: {e!r}")
            raise ResumeError("Couldn't read that PDF. Try saving it again, or upload a DOCX or TXT.")
    elif name.endswith(".docx"):
        if not data.startswith(b"PK"):
            raise ResumeError("That doesn't look like a Word (.docx) file.")
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                # Refuse zip bombs: a real resume unpacks to a few MB at most
                if sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024:
                    raise ResumeError("That file is too big once unpacked.")
            import docx
            doc = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
        except ResumeError:
            raise
        except Exception as e:
            print(f"[resume upload] DOCX parse failed: {e!r}")
            raise ResumeError("Couldn't read that Word file. Try saving it again, or upload a PDF or TXT.")
    else:
        raise ResumeError(UNSUPPORTED_RESUME)
    return text[:RESUME_MAX_CHARS]
