"""
main.py — Job Hunter Board entry point.

Works with ZERO API keys out of the box.
Demo data loads automatically so you can explore the dashboard immediately.

Commands:
  python main.py --run-now              Full pipeline (scrape → contacts → score → notify) for the admin account
  python main.py --run-now --all-users  Same, but loops over every registered user
  python main.py --dashboard            Open review dashboard at http://localhost:5050
  python main.py --demo                 Load demo jobs + open dashboard (no internet needed)
  python main.py --schedule             Run pipeline daily at 07:30 for the admin account (keep terminal open)
  python main.py --schedule --all-users Same, but re-scores + re-recommends for every registered user daily
  python main.py --score-only           Re-score existing jobs without scraping

Optional .env keys (all features work without them):
  ANTHROPIC_API_KEY   Better scoring + AI-written cover letters
  SENDER_EMAIL        Email digest delivery (Gmail)
  SENDER_PASSWORD     Gmail app password
  PROXYCURL_API_KEY   Live LinkedIn contact lookup
  TELEGRAM_BOT_TOKEN  Telegram phone notifications
"""

import argparse, os, schedule, time
from dotenv import load_dotenv
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(base_dir, ".env"))

DB_PATH    = os.getenv("DB_PATH", "jobs.db")
MIN_SCORE  = float(os.getenv("MIN_SCORE", "6.0"))
DASH_PORT  = int(os.getenv("DASHBOARD_PORT", "5050"))


def run_pipeline(scrape=True):
    from db import init_db
    from scraper import run_all_scrapers
    from linkedin_finder import enrich_jobs_with_contacts
    from ai_engine import process_new_jobs
    from notifier import notify

    print("\n" + "="*55)
    print("PM JOB HUNTER")
    print("="*55)

    init_db(DB_PATH)

    if scrape:
        run_all_scrapers(DB_PATH, user_id=1)
        enrich_jobs_with_contacts(DB_PATH, user_id=1)

    # Sync application statuses from user's email
    from email_scraper import sync_job_statuses_from_email
    sync_job_statuses_from_email(DB_PATH, user_id=1)

    digest = process_new_jobs(DB_PATH, min_score=MIN_SCORE, user_id=1)
    notify(digest, user_id=1)
    print(f"\nDone. Open dashboard: python main.py --dashboard\n")

def run_pipeline_for_user(user_id: int, scrape: bool = True):
    """Runs the full scrape → contacts → email-sync → score → notify
    pipeline scoped to a single user_id. Mirrors the logic used by the
    dashboard's /api/refresh route so behavior stays consistent between
    manual "Refresh Now" clicks and the scheduled daily run."""
    from scraper import run_all_scrapers
    from linkedin_finder import enrich_jobs_with_contacts
    from ai_engine import process_new_jobs
    from notifier import notify
    from email_scraper import sync_job_statuses_from_email

    print(f"\n--- Running daily pipeline for user_id={user_id} ---")

    if scrape:
        run_all_scrapers(DB_PATH, user_id=user_id)

    enrich_jobs_with_contacts(DB_PATH, user_id=user_id)
    sync_job_statuses_from_email(DB_PATH, user_id=user_id)

    digest = process_new_jobs(DB_PATH, min_score=MIN_SCORE, user_id=user_id)
    notify(digest, user_id=user_id)
    return digest


def run_pipeline_for_all_users(scrape: bool = True):
    """Loops over every registered user and runs run_pipeline_for_user()
    for each one. This is what should be scheduled to run daily so every
    signed-up user gets fresh recommendations, not just the admin account."""
    from db import init_db, get_conn

    print("\n" + "="*55)
    print("PM JOB HUNTER - DAILY RUN FOR ALL USERS")
    print("="*55)

    init_db(DB_PATH)
    conn = get_conn(DB_PATH)
    users = conn.execute("SELECT id, username FROM users").fetchall()
    conn.close()

    print(f"Found {len(users)} user(s) to process.\n")

    results = {}
    for u in users:
        uid = u["id"]
        uname = u["username"]
        try:
            digest = run_pipeline_for_user(uid, scrape=scrape)
            results[uname] = len(digest)
        except Exception as e:
            print(f"  [User '{uname}'] Pipeline failed: {e}")
            results[uname] = f"error: {e}"

    print("\n" + "-"*55)
    print("Daily run summary:")
    for uname, outcome in results.items():
        print(f"  {uname}: {outcome}")
    print("-"*55)
    print(f"\nDone. Open dashboard: python main.py --dashboard\n")
    return results



def run_demo():
    """Opens the dashboard on the existing database (built-in demo jobs were removed)."""
    print("\nDemo jobs are no longer bundled. Sign up in the dashboard and click Refresh to pull live jobs.\n")
    open_dashboard()


def open_dashboard():
    from db import init_db
    from dashboard import app
    init_db(DB_PATH)
    print(f"Dashboard → http://localhost:{DASH_PORT}")
    print("Press Ctrl+C to stop.\n")
    app.run(debug=False, port=DASH_PORT, use_reloader=False)


def main():
    ap = argparse.ArgumentParser(
        description="Job Hunter Board — finds, scores, and drafts applications for roles in India",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--run-now",    action="store_true", help="Full pipeline")
    ap.add_argument("--dashboard",  action="store_true", help="Open review dashboard")
    ap.add_argument("--demo",       action="store_true", help="Load demo data + open dashboard")
    ap.add_argument("--schedule",   action="store_true", help="Daily schedule (keep terminal open)")
    ap.add_argument("--score-only", action="store_true", help="Re-score without scraping")
    ap.add_argument("--time",       default="07:30",     help="Schedule time HH:MM")
    ap.add_argument("--all-users",  action="store_true", help="Apply --run-now / --schedule to every registered user instead of just the admin account (id 1)")
    args = ap.parse_args()

    if args.run_now:
        if args.all_users:
            run_pipeline_for_all_users()
        else:
            run_pipeline()
    elif args.dashboard:
        open_dashboard()
    elif args.demo:
        run_demo()
    elif args.score_only:
        from db import init_db
        from ai_engine import process_new_jobs
        from notifier import notify
        init_db(DB_PATH)
        notify(process_new_jobs(DB_PATH, min_score=MIN_SCORE))
    elif args.schedule:
        target = run_pipeline_for_all_users if args.all_users else run_pipeline
        scope = "every registered user" if args.all_users else "the admin account"
        print(f"Scheduler started — runs daily at {args.time} for {scope}. Press Ctrl+C to stop.")
        schedule.every().day.at(args.time).do(target)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
