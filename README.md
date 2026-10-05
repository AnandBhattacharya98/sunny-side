# Job Hunter Board

An AI-augmented job application tracker and companion. It automatically scrapes newly posted job openings in India, analyzes fit against your resume, scores candidates, logs timeline actions, synchronizes related Gmail updates, and features an interactive Board Assistant (voice/chat companion) and Quiz Mode partner.

---

## Setup & Installation

1. **Clone & Install Dependencies**:
   ```bash
   pip3 install -r requirements.txt
   ```

2. **Database Configuration**:
   By default, the application runs on a local SQLite database (`jobs.db`). To scale to a production setup, specify a PostgreSQL connection string in the `DATABASE_URL` environment variable.

3. **API Keys**:
   Copy `.env.example` to `.env` in the root directory and add your keys/credentials:
   ```bash
   cp .env.example .env
   ```

---

## Environment Variables

| Variable | Required? | Description |
|:---|:---|:---|
| `FLASK_SECRET_KEY` | **Yes (production)** | Signs login sessions and encrypts saved credentials. If unset, a key is generated into `.secret_key`; losing that file logs everyone out and makes saved Gmail passwords/Gemini keys unreadable |
| `ADMIN_PASSWORD` | **Yes (production)** | Password for the `admin` account, applied at every startup. If unset, a random one is generated and printed to the logs (the old default `admin` password is replaced automatically) |
| `CRON_SECRET` | For daily emails | Token your scheduler sends as `X-Cron-Token` to `POST /api/cron/daily-recommendations`. The endpoint is disabled until this is set |
| `DATA_ENCRYPTION_KEY` | Optional | Fernet key used to encrypt users' saved Gmail app passwords and Gemini keys (defaults to one derived from `FLASK_SECRET_KEY`) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Optional | Enables "Sign in with Google" |
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` | Optional | Enables "Sign in with LinkedIn" |
| `ALLOW_DEV_LOGIN` | Dev only | `1` enables the fake social sign-in page for local testing. Anyone can log in as anyone with it, so never set it on a public server |
| `SESSION_COOKIE_SECURE` | Optional | Defaults to `1` (cookies only over HTTPS). Set `0` for plain-http testing on a non-localhost address |
| `ANTHROPIC_API_KEY` | Optional | Enables Claude-powered scoring and cover letter generation (high quality) |
| `GEMINI_API_KEY` | Optional | Server-wide Gemini key for scoring, STT, TTS, and quiz feedback. Users can add their own key in settings; a user's personal key is never used for anyone else |
| `DATABASE_URL` | Optional | Set to a PostgreSQL connection string to use Postgres instead of SQLite |
| `SENDER_EMAIL` | Optional | Gmail address used to send digests. Each user's digest goes to their own sign-in email (or the Gmail they connected); `RECIPIENT_EMAIL` is only used for the admin account |
| `SENDER_PASSWORD` | Optional | Gmail App Password matching the sender email |
| `PROXYCURL_API_KEY` | Optional | Enables live LinkedIn contact lookup for companies |
| `TELEGRAM_BOT_TOKEN` | Optional | Configures a Telegram bot for mobile push notifications |
| `DB_PATH` | Optional | Path to SQLite database file (default: `jobs.db`) |
| `MIN_SCORE` | Optional | Threshold score to include a scraped job in recommendation runs (default: `6.0`) |
| `DASHBOARD_PORT` | Optional | Local port to run the Flask dashboard server (default: `5050`) |

---

## Commands

| Command | What it does |
|:---|:---|
| `python main.py --dashboard` | Opens the local review dashboard at http://localhost:5050 |
| `python main.py --run-now` | Runs the full pipeline (scrape → contacts → score → notify) for the admin account |
| `python main.py --run-now --all-users` | Loops over every registered user and runs their individual recommendation pipelines |
| `python main.py --schedule` | Runs the pipeline daily at `07:30` (or custom `--time`) for the admin account |
| `python main.py --schedule --all-users` | Runs the daily scheduled recommendation loop for all registered users |
| `python main.py --score-only` | Re-evaluates and scores existing database listings without running new web scrapers |
| `python main.py --demo` | Opens the dashboard locally (bundled demo jobs were removed; sign up and click Refresh) |

---

## Core Features

### 1. Board Assistant (Voice & Chat Companion)
- **Slide-in Panel**: A right-docked panel that lets you interact with your jobs board through text or voice commands.
- **Privacy First**: Displays a consent prompt modal before accessing the microphone.
- **Dual STT/TTS Providers**: Upgrades automatically to Gemini's audio models if an API key is present in settings, with browser-native Web Speech API fallback.
- **Persisted Session Context**: Keeps conversation history in-memory for the current tab session so the assistant can resolve pronouns ("it", "move that one") from previous turns.
- **Rich Cards**: Inline rendering of pipeline statistics, compact job-card nodes, and synced email expandables.

### 2. Kanban Application Pipeline
- Tracks listings across columns: `Self-Import`, `Inbox`, `Shortlist`, `Interviewing`, `Applied`, `Offer`, `Rejected`.
- Moving a listing to `Interviewing` prompts a stage-picker dialog to track recruiter screens, case studies, or coding rounds.

### 3. Quiz Mode
- A tab inside each job card reskins the modal to an accent-purple theme for active interview prep.
- Provides interactive flashcards (quick-fire outline prompts) and accordion lists (deeper model answer guidelines).
- Allows typing or speaking behavioral answers to receive critiques and rephrased model answers.

---

## Running for many users

- Set `FLASK_SECRET_KEY`, `ADMIN_PASSWORD` and `CRON_SECRET` before going live (see `.env.example`).
- Each user's jobs, contacts, emails, cover letters and digests are kept to their own account. Inbox sync only reads the Gmail a user connected themselves; the server's `IMAP_*`/`SENDER_*` inbox is only synced for the admin account.
- Saved Gmail app passwords and Gemini keys are encrypted in the database and never sent back to the browser.
- Refreshing, re-scoring and the daily run happen in the background, so the page stays responsive.
- Profiles appear on the landing page only when a user opts in.
