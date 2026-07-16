"""
main.py — PM Job Hunter entry point.

Works with ZERO API keys out of the box.
Demo data loads automatically so you can explore the dashboard immediately.

Commands:
  python main.py --run-now      Full pipeline (scrape → contacts → score → notify)
  python main.py --dashboard    Open review dashboard at http://localhost:5050
  python main.py --demo         Load demo jobs + open dashboard (no internet needed)
  python main.py --schedule     Run pipeline daily at 07:30 (keep terminal open)
  python main.py --score-only   Re-score existing jobs without scraping

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
        run_all_scrapers(DB_PATH)
        enrich_jobs_with_contacts(DB_PATH)

    # Sync application statuses from user's email
    from email_scraper import sync_job_statuses_from_email
    sync_job_statuses_from_email(DB_PATH)

    digest = process_new_jobs(DB_PATH, min_score=MIN_SCORE)
    notify(digest)
    print(f"\nDone. Open dashboard: python main.py --dashboard\n")


def run_demo():
    """Load demo jobs and open dashboard — works with zero internet or keys."""
    from db import init_db
    from scraper import seed_demo_jobs, init_db as scraper_init
    from linkedin_finder import enrich_jobs_with_contacts
    from ai_engine import process_new_jobs
    import sqlite3

    print("\nLoading demo data…")
    conn = init_db(DB_PATH)
    conn.close()

    conn2 = sqlite3.connect(DB_PATH)
    from scraper import DEMO_JOBS
    from scraper import _insert_job
    for job in DEMO_JOBS:
        _insert_job(conn2, job)
    conn2.close()

    enrich_jobs_with_contacts(DB_PATH)
    process_new_jobs(DB_PATH, min_score=0)  # score everything for demo

    print("Demo data loaded. Opening dashboard…\n")
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
        description="PM Job Hunter — finds, scores, and drafts applications for PM roles in India",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--run-now",    action="store_true", help="Full pipeline")
    ap.add_argument("--dashboard",  action="store_true", help="Open review dashboard")
    ap.add_argument("--demo",       action="store_true", help="Load demo data + open dashboard")
    ap.add_argument("--schedule",   action="store_true", help="Daily schedule (keep terminal open)")
    ap.add_argument("--score-only", action="store_true", help="Re-score without scraping")
    ap.add_argument("--time",       default="07:30",     help="Schedule time HH:MM")
    args = ap.parse_args()

    if args.run_now:
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
        print(f"Scheduler started — runs daily at {args.time}. Press Ctrl+C to stop.")
        schedule.every().day.at(args.time).do(run_pipeline)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
