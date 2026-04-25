# Choreizo — Plan

A self-hosted random daily chore selector and tracker. One Docker container, runs on your LAN, talks to your household via Telegram, managed via a small web admin panel.

---

## 1. Goals & non-goals

**Goals**
- Define each chore once a year with a frequency (e.g., vacuum every 7 days, filter every 42 days, water-filter purge every 365 days).
- Each morning, send every active household member a personal Telegram message listing *their* chores for the day.
- Members respond with `Completed`, `Skip`, or `Ignore` (as commands or inline buttons).
- High-priority chores get follow-up reminders, roll over if missed, can escalate to another member, and can notify the admin.
- Minimal web dashboard + full admin panel for CRUD on chores, members, eligibility, and viewing history.
- Self-hosted as a single Docker container with a SQLite database on a mounted volume.

**Non-goals (v1)**
- Multi-household / SaaS mode. One household per deployment.
- Mobile app. Telegram is the mobile experience.
- Complex gamification (streaks, points, leaderboards) — could come later.
- OAuth / SSO. Admin panel uses a password; members authenticate via Telegram.

---

## 2. Tech stack

| Concern               | Choice                                                  |
|-----------------------|---------------------------------------------------------|
| Language              | Python 3.12                                             |
| Web framework         | FastAPI                                                 |
| Server                | Uvicorn (single worker — we share state with the bot)   |
| DB                    | SQLite via SQLAlchemy 2.x + Alembic migrations          |
| Templates             | Jinja2 (server-rendered admin panel — no SPA)           |
| Styling               | Pico.css or Tailwind CDN (decide at scaffolding time)   |
| Telegram              | `python-telegram-bot` v21+ (async)                      |
| Scheduling            | APScheduler (AsyncIOScheduler)                          |
| Config                | Pydantic Settings + `.env` file                         |
| Timezone              | `zoneinfo` (stdlib) — configurable per-deployment       |
| Container             | `python:3.12-slim` base, non-root user, single process  |
| Tests                 | pytest + httpx + `pytest-asyncio`                       |

Everything runs in one async event loop: the FastAPI app, the PTB Application (polling or webhook), and the APScheduler jobs. No separate worker container.

---

## 3. Data model

SQLite schema. Times stored as UTC ISO8601 strings; rendered in the configured house timezone.

```
users
  id                INTEGER PK
  name              TEXT NOT NULL
  telegram_chat_id  INTEGER UNIQUE        -- null until /start <code>
  telegram_username TEXT
  is_admin          BOOLEAN DEFAULT 0
  is_escalation     BOOLEAN DEFAULT 0     -- receives escalations
  active            BOOLEAN DEFAULT 1
  send_time         TEXT DEFAULT '08:00'  -- HH:MM in house timezone
  created_at        TEXT

invite_codes
  code              TEXT PK               -- short, unguessable
  intended_name     TEXT
  is_admin          BOOLEAN DEFAULT 0
  expires_at        TEXT
  used_at           TEXT                  -- null if unused
  used_by_user_id   INTEGER FK users

chores
  id                  INTEGER PK
  name                TEXT NOT NULL
  description         TEXT
  frequency_days      INTEGER NOT NULL    -- 1, 7, 14, 42, 365, ...
  priority            INTEGER DEFAULT 0   -- 0=normal, 1=high
  estimated_minutes   INTEGER
  enabled             BOOLEAN DEFAULT 1
  created_by_user_id  INTEGER FK users    -- who added it; null = admin/seed
  created_at          TEXT

chore_eligibility
  chore_id          INTEGER FK chores
  user_id           INTEGER FK users
  mode              TEXT CHECK(mode IN ('allow','deny'))
  PRIMARY KEY (chore_id, user_id)
  -- If a chore has any 'allow' rows => allow-list semantics (only those users eligible).
  -- Otherwise all active users are eligible EXCEPT those with a 'deny' row.

assignments
  id                INTEGER PK
  chore_id          INTEGER FK chores
  user_id           INTEGER FK users
  assigned_date     TEXT                  -- YYYY-MM-DD in house tz
  status            TEXT                  -- pending|completed|skipped|ignored|overdue|escalated
  assigned_at       TEXT
  responded_at      TEXT
  completed_at      TEXT
  escalated_from    INTEGER FK users      -- nullable
  rolled_over_from  INTEGER FK assignments -- nullable, chain of rollovers
  notes             TEXT

reminder_events
  id                INTEGER PK
  assignment_id     INTEGER FK assignments
  kind              TEXT                  -- daily_send|hourly_reminder|escalation|admin_notify
  sent_at           TEXT

magic_link_tokens
  token             TEXT PK               -- random, single-use
  user_id           INTEGER FK users
  created_at        TEXT
  expires_at        TEXT
  used_at           TEXT

settings                                 -- single-row key/value
  key               TEXT PK
  value             TEXT
```

Indexes: `assignments (user_id, assigned_date)`, `assignments (chore_id, assigned_date)`, `assignments (status)`.

---

## 4. Assignment algorithm

A single job runs once a day at `DAILY_ASSIGNMENT_HOUR` (default 06:00 house-tz, before anyone's send time).

**Step 1 — Handle yesterday's unresolved assignments.**
For every `pending` assignment from yesterday or earlier:
- If chore is high-priority and `ROLLOVER_HIGH_PRIORITY=true` → mark it `overdue`, leave it assigned (it will re-appear in today's message flagged "Overdue").
- Otherwise mark it `ignored` (no response) and move on.

**Step 2 — Select chores due today.**
A chore is due today if:
```
last_completed_at + frequency_days <= today
```
and no active (`pending`/`overdue`) assignment already exists for it. For a brand-new chore with no completions, seed it as if last_completed_at = created_at.

**Step 3 — Assign each due chore.**
For each due chore:
1. Compute eligible users = active users that pass the `chore_eligibility` rules.
2. If eligible set is empty → log a warning in admin panel and skip.
3. Weight each eligible user: `weight = 1 / (1 + completions_last_14d)` — lightly favors anyone who's done less recently; pure random is fine too but this keeps fairness smooth.
4. Random-weighted pick. Create an `assignments` row (`status=pending`).

**Step 4 — Nothing for a user?**
If someone has no chores, they get no message that day (or a quiet "nothing today" — configurable).

The algorithm is deterministic given the RNG seed (optional env var for reproducibility during testing).

---

## 5. Telegram bot

### Commands
| Command              | Who     | Effect                                                |
|----------------------|---------|--------------------------------------------------------|
| `/start <code>`      | anyone  | Claim invite code, link this chat id to the user.      |
| `/today`             | member  | Resend today's assigned chores.                        |
| `/upcoming`          | member  | Next 7 days of *likely* assignments (heuristic).       |
| `/done <id>`         | member  | Mark assignment completed.                             |
| `/skip <id>`         | member  | Skip for today (counts as not done; no rollover).      |
| `/ignore <id>`       | member  | Acknowledge but don't act (closes the reminder loop).  |
| `/help`              | anyone  | Command list.                                          |
| `/whoami`            | anyone  | Show linked identity / "not linked".                   |
| `/web`               | member  | DM a one-time magic-link URL to log in to the web app. |
| `/admin`             | admin   | URL + one-time login token for web admin.              |

### Daily message shape
```
Good morning, Collin!

Today's chores:
  1. Vacuum living room  [normal]
  2. Change HVAC filters [HIGH — due since Tue] (Overdue)

Reply: /done 1  /skip 1  /ignore 1
— or tap buttons below —
[ ✅ Done 1 ] [ ⏭ Skip 1 ] [ 🙈 Ignore 1 ]
[ ✅ Done 2 ] [ ⏭ Skip 2 ] [ 🙈 Ignore 2 ]
```

### High-priority reminder flow
Background job runs every 15 min and, for each `pending`/`overdue` high-priority assignment:
- If now > send_time and `(now - last_reminder) >= HIGH_PRIORITY_REMINDER_INTERVAL_HOURS` → send hourly-style reminder (configurable, default 3h).
- If `(now - assigned_at) >= ESCALATION_AFTER_HOURS` and not escalated yet → reassign: create a new `assignment` row for another eligible user, link `escalated_from`, notify both parties.
- If `(now - assigned_at) >= ADMIN_NOTIFY_AFTER_HOURS` and admin not notified → DM the designated admin/escalation user with the unresolved chore.

All reminder events are written to `reminder_events` for auditability and to avoid duplicate sends.

---

## 6. Web interfaces

Server-rendered (Jinja2 + Pico.css), same FastAPI app, cookie-session auth. Two modes: **member** (any linked household member) and **admin** (flagged users). Admin sees everything; members see their own stuff.

### 6.1 Auth

- **Admin.** Password login (`ADMIN_PASSWORD` env var bootstraps the first admin). Additional admins are flagged via the member edit page. They can also use the Telegram magic-link as a fallback.
- **Member.** Magic-link only. In Telegram, the user sends `/web` → bot DMs a one-time signed URL `/auth/telegram?token=...`. Clicking it sets a cookie session (default 30 days). No password to remember.
- Sessions are signed with `SESSION_SECRET`. Magic-link tokens are single-use and expire in 10 min.

### 6.2 Admin-only pages (`is_admin = true`)

- `/` — Dashboard: today's assignments grid (rows = members, columns = chores), "Overdue" panel, "Last 7 days completion rate" tile.
- `/chores` — Full list + CRUD. Each row: name, frequency, priority, estimated minutes, eligible-users summary, last completed, next due.
- `/chores/new` and `/chores/{id}/edit` — Full edit form. Includes per-chore eligibility editor (grid of members as checkboxes, optional "deny" mode).
- `/members` — List, add, edit (name, send time, active, is_admin, is_escalation).
- `/members/invite` — Generate invite code, copy link `https://t.me/<botname>?start=<code>`.
- `/history` — Paginated assignments with filters (user, chore, status, date range). CSV export.
- `/reports` — Simple charts: completion rate per user, chores most-often-skipped, fairness histogram.
- `/settings` — House timezone, daily assignment hour, reminder/escalation intervals, bot token status, DB path, etc.

### 6.3 Member-facing pages (any linked user)

- `/me` — Personal dashboard: today's chores, upcoming, my completion stats.
- `/me/chores` — **Checkbox list** of every enabled chore in the database. Each row shows the chore name, frequency, priority, and a checkbox indicating whether *I* participate in it. Tick/untick, press Save, done. This edits only my own `chore_eligibility` rows.
  - UI mockup:
    ```
    My Chores                                            [ Save ]
    ────────────────────────────────────────────────────
    [x] Vacuum living room          every 7 days   normal
    [x] Scoop kitty litter          every 7 days   normal
    [ ] Clean guest bathroom        every 14 days  normal
    [x] Change HVAC filters         every 42 days  HIGH
    [ ] Purge water filter          every 365 days HIGH
    [x] Take out trash              every 3 days   normal
    ...
    ```
- `/me/chores/new` — Add a brand-new chore to the database. Fields: name, description, frequency (days), priority (normal/high), estimated minutes. New chores default to *only the creator opted in*; other members can opt in from their own `/me/chores` page, and an admin can adjust eligibility from the chore's edit page.
- `/me/chores/{id}/edit` — Only visible if I created this chore (`created_by_user_id = me`) and I am not an admin overriding. Lets me edit the chore definition. Admins can edit any chore from the admin page.
- `/me/chores/{id}/delete` — Same rule: only chores I created. Soft-delete (`enabled = 0`) so history is preserved.
- `/me/history` — My past assignments with status filter.

### 6.4 Unified eligibility model

The admin's per-chore editor and the member's `/me/chores` checkboxes write to the same `chore_eligibility` table. A member can only touch their own row; admins can touch anyone's. The resolved "eligible members for chore X" is computed per the rules in §3.

Forms use HTMX where it genuinely simplifies (table inline edits, checkbox saves); otherwise plain POST.

---

## 7. Scheduler jobs

| Job                        | Cadence                 | Purpose                                     |
|----------------------------|-------------------------|---------------------------------------------|
| `daily_assignment`         | once/day @ 06:00 house-tz | Build today's assignments.                |
| `daily_send`               | every minute            | Send each user their message at their send_time. |
| `reminder_tick`            | every 15 min            | High-priority reminders / escalations / admin notifies. |
| `housekeeping`             | nightly                 | Archive old assignments, vacuum SQLite.     |

All jobs are idempotent via `reminder_events` / assignment status so restarts are safe.

---

## 8. Configuration (.env)

```
# Required
TELEGRAM_BOT_TOKEN=123456:abc...
ADMIN_PASSWORD=choose-a-long-passphrase
SESSION_SECRET=random-hex-string
BASE_URL=https://choreizo.home.yourlan

# Timing
HOUSE_TIMEZONE=America/Los_Angeles
DAILY_ASSIGNMENT_HOUR=6
HIGH_PRIORITY_REMINDER_INTERVAL_HOURS=3
ESCALATION_AFTER_HOURS=24
ADMIN_NOTIFY_AFTER_HOURS=36
ROLLOVER_HIGH_PRIORITY=true

# Storage
DATABASE_URL=sqlite:////data/choreizo.db
LOG_LEVEL=INFO

# Telegram mode
TELEGRAM_MODE=polling        # or 'webhook' if BASE_URL is externally reachable
```

---

## 9. Project layout

```
Chorebot/
├── PLAN.md                      # this doc
├── README.md                    # quickstart (written during scaffolding)
├── Dockerfile
├── docker-compose.yml           # single-service, for one-command run
├── pyproject.toml
├── .env.example
├── .dockerignore
├── alembic.ini
├── alembic/
│   └── versions/
├── data/                        # SQLite volume mount target (gitignored)
├── app/
│   ├── __init__.py
│   ├── main.py                  # builds FastAPI, PTB Application, scheduler; runs them together
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── seeders.py               # optional: seed default chores
│   ├── assignment.py            # due-chore detection + weighted pick
│   ├── scheduler.py             # APScheduler wiring
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── bot.py               # Application factory
│   │   ├── handlers.py          # /start, /done, /skip, ...
│   │   ├── messages.py          # message composition
│   │   └── keyboards.py         # inline keyboards
│   ├── web/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── deps.py              # FastAPI dependencies
│   │   ├── routes_dashboard.py
│   │   ├── routes_chores.py
│   │   ├── routes_members.py
│   │   ├── routes_history.py
│   │   ├── routes_settings.py
│   │   ├── static/
│   │   └── templates/
│   │       ├── base.html
│   │       ├── dashboard.html
│   │       ├── chores/{list,form}.html
│   │       ├── members/{list,form,invite}.html
│   │       └── history.html
│   └── api/
│       └── routes.py            # thin JSON API (optional, for future)
└── tests/
    ├── test_assignment.py
    ├── test_telegram_handlers.py
    └── test_web_auth.py
```

---

## 10. Docker

Single container, non-root user, `/data` volume for the SQLite DB.

```dockerfile
FROM python:3.12-slim
RUN useradd -m -u 1000 choreizo && mkdir /data && chown choreizo /data
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
USER choreizo
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "app.main"]
```

`app.main` does roughly:
```python
async def main():
    await init_db_and_migrate()
    bot_app = build_telegram_application()
    scheduler = build_scheduler(bot_app)
    scheduler.start()
    await bot_app.initialize()
    await bot_app.start()
    # Run uvicorn in the same loop
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level=LOG_LEVEL)
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), bot_app.updater.start_polling())
```

`docker-compose.yml`:
```yaml
services:
  choreizo:
    build: .
    container_name: choreizo
    restart: unless-stopped
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
```

---

## 11. Build phases

Each phase leaves a runnable app; you can stop at any point.

1. **Scaffolding.** Repo layout, Dockerfile, docker-compose, pyproject, config, "hello world" FastAPI route, Alembic init.
2. **Data model + migrations.** Models, first migration, a couple of Pydantic schemas.
3. **Admin panel skeleton.** Login, logged-in shell, empty Chores / Members pages with working CRUD (no eligibility yet).
4. **Invite codes + Telegram linking.** `/start <code>` claims identity.
5. **Eligibility editor.** Allow/deny-list UI on chores.
6. **Assignment engine.** `assignment.py` + daily_assignment job, plus an admin "Run assignment now" button for testing.
7. **Daily send.** Compose and send per-member DMs with inline buttons. Handle Done/Skip/Ignore callbacks.
8. **High-priority behaviors.** Reminder tick job: hourly reminders, rollover, escalation, admin notify.
9. **History + reports.** List with filters, CSV export, simple charts.
10. **Polish.** Tests, README, sample seed chores, backup/restore doc.

---

## 12. Resolved decisions

1. **Telegram connectivity:** long-polling (no webhook). Works on LAN without exposing the container.
2. **Admin auth:** password (`ADMIN_PASSWORD` env) primary, Telegram magic-link via `/admin` as fallback.
3. **Member auth:** Telegram magic-link only (`/web` command in bot → one-time signed URL → 30-day cookie session).
4. **Styling:** Pico.css.
5. **Member self-service:** non-admin members can log in, see all chores as a checkbox list, toggle their own participation, and add new chores to the database. They can edit/delete only chores they themselves created. Admins can edit any chore.
6. **Skip semantics:** `/skip` = not done, chore retries next cycle (treated like "ignored" for scheduling; tracked separately for stats so you can see who tends to skip vs ignore).
7. **Multi-person chores:** v1 is strictly one assignee per chore per day. Deferred.

## 13. Remaining open items (low-stakes, can defer)

- **Seed chores.** Do you want a starter set (vacuum, litter, bathrooms, HVAC filter, water filter, trash, etc.) prepopulated on first boot, or start empty and let you add them via the web UI? Defaulting to **start empty** unless you say otherwise.
- **Admin override on member-created chores.** When an admin edits a member-created chore, should the creator be notified (e.g., via Telegram DM)? Defaulting to **no notification** for v1.
- **Session cookie duration for members.** 30 days is my default. Easy to change.

Ready to move to Phase 1 (scaffolding) whenever you are.
