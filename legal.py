"""Public legal and trust pages: privacy, terms, cookies and the rest, plus the site footer details.

Every page is public (no sign-in) so visitors can read them before they sign up. The owner's name
and contact address come from the environment, so they can be changed on the host without a deploy:

  SITE_OWNER_NAME     who made and runs the site, shown in the footer and the policies (default "AB")
  SITE_OWNER_URL      optional link for the owner's name (a portfolio or LinkedIn page)
  SITE_CONTACT_EMAIL  where privacy, deletion and grievance requests go
  SITE_JURISDICTION   the country whose courts and laws the terms name (default "India")
"""
import os
from datetime import date

from flask import Blueprint, abort, redirect, render_template, request

bp = Blueprint("legal", __name__)

# Bump when a policy's substance changes; the cookie banner asks again when CONSENT_VERSION changes.
LAST_UPDATED = date(2026, 11, 14)
CONSENT_VERSION = 1

# slug -> (title, one-line summary for the index and <meta name="description">)
PAGES = {
    "privacy": ("Privacy Policy", "What we collect, why, who processes it and the rights you have over it."),
    "terms": ("Terms of Service", "The rules for using Sunny Side, and what we can and can't promise."),
    "cookies": ("Cookie Policy", "Every cookie and bit of browser storage we use, and how to change your choice."),
    "acceptable-use": ("Acceptable Use Policy", "What you may and may not do with Sunny Side."),
    "disclaimer": ("Disclaimer", "AI suggestions, job listings and practice calls come with limits."),
    "your-data": ("Your Data & Deletion", "How to download your data, delete your account, or ask us to."),
    "accessibility": ("Accessibility Statement", "How we make Sunny Side usable for everyone, and how to tell us when it isn't."),
    "contact": ("Contact & Grievances", "How to reach the person who runs Sunny Side."),
}


def site_info() -> dict:
    owner = os.getenv("SITE_OWNER_NAME", "").strip() or "AB"
    return {
        "name": "Sunny Side",
        "owner": owner,
        "owner_url": os.getenv("SITE_OWNER_URL", "").strip(),
        "email": os.getenv("SITE_CONTACT_EMAIL", "").strip(),
        "jurisdiction": os.getenv("SITE_JURISDICTION", "").strip() or "India",
        "year": date.today().year,
        "updated": LAST_UPDATED,
        "updated_text": f"{LAST_UPDATED.day} {LAST_UPDATED:%B %Y}",
        "consent_version": CONSENT_VERSION,
        "pages": PAGES,
    }


@bp.app_context_processor
def _inject_site():
    return {"site": site_info()}


@bp.route("/legal")
def legal_index():
    return render_template("legal/index.html", slug="legal", title="Legal & policies",
                           summary="Every policy that covers Sunny Side, in plain language.")


def legal_page(slug):
    if slug not in PAGES:
        abort(404)
    title, summary = PAGES[slug]
    return render_template(f"legal/{slug}.html", slug=slug, title=title, summary=summary)


for _slug in PAGES:
    bp.add_url_rule("/" + _slug, endpoint="legal_page", view_func=legal_page, defaults={"slug": _slug})


# Short aliases people type or other sites link to
@bp.route("/privacy-policy")
@bp.route("/terms-of-service")
@bp.route("/tos")
@bp.route("/cookie-policy")
def legal_alias():
    target = {"/privacy-policy": "privacy", "/terms-of-service": "terms", "/tos": "terms",
              "/cookie-policy": "cookies"}[request.path]
    return redirect("/" + target, code=301)


PUBLIC_ENDPOINTS = ("legal.legal_index", "legal.legal_page", "legal.legal_alias")
