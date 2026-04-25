# Choreizo

A self-hosted random daily chore selector and tracker. Python + FastAPI + SQLite in a single Docker container, with a Telegram bot for daily interaction and a small web admin + member panel for management.

---

## Quick start (Docker / Dockge)

```bash
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, ADMIN_PASSWORD, SESSION_SECRET — see below
docker compose up --build
```

The web UI will be at `http://<your-host>:8000`.

On first boot, Alembic runs migrations automatically and an admin user is bootstrapped from `ADMIN_PASSWORD`.

### Minimum required `.env` values

| Variable | How to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ADMIN_PASSWORD` | Pick a strong passphrase |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `BASE_URL` | The URL your browser uses to reach the app, e.g. `http://192.168.1.50:8000` |

---

## First-run checklist

1. Start the container.
2. Open `BASE_URL` → you'll land on the login page. Sign in with username `admin` and your `ADMIN_PASSWORD`.
3. Go to **Members → Invites** and generate an invite code for each household member.
4. Share the invite link (`https://t.me/<botname>?start=<code>`) with each person — they tap it, start the bot, and their Telegram account gets linked automatically.
5. Go to **Chores** and add your household chores (name, frequency in days, priority).
6. Each chore defaults to all linked members being eligible. Members can opt themselves in/out from their own **My Chores** page.
7. Assignments are generated automatically at the hour set by `DAILY_ASSIGNMENT_HOUR` (default 06:00 in your `HOUSE_TIMEZONE`). Use **Today → Run assignment now** to trigger it immediately for testing.

---

## Telegram bot commands

| Command | Who | Effect |
|---|---|---|
| `/start <code>` | Anyone | Claim an invite code and link your Telegram account |
| `/today` | Member | Resend today's assigned chores |
| `/done <id>` | Member | Mark a chore completed |
| `/skip <id>` | Member | Skip (chore retries next cycle) |
| `/ignore <id>` | Member | Acknowledge without acting (closes reminders) |
| `/web` | Member | Get a magic-link to the member web UI |
| `/admin` | Admin | Get a magic-link to the admin panel |
| `/whoami` | Anyone | Show your linked account name and role |
| `/help` | Anyone | List all commands |

---

## Web UI pages

**Admin** (`/admin/*`):
- **Today** — today's assignment grid with Run/Send buttons for testing
- **Chores** — full CRUD; set name, frequency, priority, estimated minutes
- **Members** — manage household members, toggle active/admin/escalation flags
- **Invites** — generate one-time invite codes
- **History** — paginated assignment history with filters and CSV export
- **Stats** — completion rates, skip rates, fairness histogram
- **Settings** — view all current config values

**Member** (`/me/*`):
- **My chores** — today's assignments + checkbox list to opt in/out of any chore
- **Add chore** — any member can add a new chore to the database
- **My history** — personal assignment history

---

## Configuration reference

```bash
# Required
TELEGRAM_BOT_TOKEN=          # from @BotFather
ADMIN_PASSWORD=              # bootstraps the first admin account
SESSION_SECRET=              # random hex string for cookie signing
BASE_URL=http://localhost:8000

# Timing
HOUSE_TIMEZONE=America/Los_Angeles
DAILY_ASSIGNMENT_HOUR=6
HIGH_PRIORITY_REMINDER_INTERVAL_HOURS=3
ESCALATION_AFTER_HOURS=24
ADMIN_NOTIFY_AFTER_HOURS=36
ROLLOVER_HIGH_PRIORITY=true

# Storage
DATABASE_PATH=/data/choreizo.db
LOG_LEVEL=INFO

# Telegram mode
TELEGRAM_MODE=polling        # "polling" works on LAN; "webhook" needs a public URL

# Server
HOST=0.0.0.0
PORT=8000
```

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
# Set DATABASE_PATH=./data/choreizo.db in .env for local runs
mkdir -p data
alembic upgrade head
python -m app.main
```

## Tests

```bash
pytest
```

---

## Data & backups

The SQLite database lives at `/data/choreizo.db` inside the container, mounted from `./data/` on the host. Back it up by copying that file — no special tooling needed.

To restore: stop the container, replace `./data/choreizo.db`, restart.
