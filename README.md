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
| `ANTHROPIC_API_KEY` | Optional | Enables Claude-powered scoring and cover letter generation (high quality) |
| `GEMINI_API_KEY` | Optional | Enables Gemini-powered scoring, STT, TTS, and quiz feedback |
| `DATABASE_URL` | Optional | Set to a PostgreSQL connection string to use Postgres instead of SQLite |
| `SENDER_EMAIL` | Optional | Gmail address used for sending daily digest summaries |
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
| `python main.py --demo` | Loads sample jobs and launches the dashboard locally (no API keys required) |

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
