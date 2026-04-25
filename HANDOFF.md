# Choreizo — Development Handoff

This document brings a fresh Claude Code session fully up to speed on the
state of Choreizo. Read it top to bottom before making changes.

## What Choreizo is

A self-hosted, single-Docker-container app that randomly assigns daily
chores to household members and nags them via Telegram until done.
Members reply with inline buttons (Done / Skip / Ignore). High-priority
chores escalate to a backup user and notify the admin if ignored too
long.

The product owner uses the name **Choreizo**; the repo folder is named
`Chorebot`.

## Stack

- **Python 3.12**, FastAPI, uvicorn (async ASGI)
- **SQLAlchemy 2.0** async + aiosqlite, Alembic migrations (with
  `render_as_batch=True` so SQLite ALTERs work)
- **python-telegram-bot ≥21** (long-polling) on the FastAPI event loop
- **APScheduler** (`AsyncIOScheduler`) on the same loop
- **Pydantic Settings** loading from `.env`
- **Pico.css** via CDN, Jinja2 templates, no JS framework
- **bcrypt** for admin password hashing, **itsdangerous** (Starlette
  `SessionMiddleware`) for cookie signing
- **pytest + pytest-asyncio** for tests; in-memory SQLite per fixture

The whole runtime — web, bot, scheduler — runs in a single asyncio loop
in one container.

## Key locked-in decisions (do not reverse without asking)

1. SQLite, single Docker container, self-hosted. No Postgres, no
   external services.
2. Telegram via long-polling (no webhook). Members never type a
   password — they sign in via magic-link DMs.
3. Admin uses a password; admins also get magic-links if they want.
4. Per-chore eligibility is a matrix of allow/deny rules. If any
   `allow` row exists for a chore, only allow-listed users are
   eligible. Otherwise everyone *except* `deny`-rowed users is.
5. Members can self-add chores from `/me/chores`. Admin owns
   allow-list overrides; member checkboxes only manage their own
   `deny` rows.
6. **All four high-priority behaviours are enabled**: rollover,
   hourly reminders, escalation, admin notify.
7. `skip` = not done, retry next cycle. One assignee per chore per day.
8. Fairness-weighted random selection (`weight = 1 / (1 + recent_load)`
   over the last 30 days), with the picked user's load bumped *within
   a single run* so a chain of due chores doesn't dog-pile one member.

## Repo layout

```
Chorebot/
├── Dockerfile, docker-compose.yml, docker-entrypoint.sh
├── .env.example, .gitignore, .dockerignore
├── pyproject.toml          # hatchling build, all deps pinned
├── alembic/, alembic.ini   # one migration so far: ef171a701513
├── app/
│   ├── main.py             # FastAPI app + lifespan + middleware
│   ├── config.py           # Pydantic Settings (admin_password,
│   │                       # session_secret, telegram_bot_token, etc.)
│   ├── db.py               # async engine, FK pragma listener,
│   │                       # AsyncSessionLocal, get_session
│   ├── models.py           # 8 ORM models — see "Data model" below
│   ├── auth.py             # bcrypt hash/verify, current_user dep,
│   │                       # require_admin dep
│   ├── magic.py            # mint_magic_link, consume_magic_link
│   ├── invites.py          # mint_invite, redeem_invite
│   ├── eligibility.py      # resolve_eligible_users + writers
│   ├── assignments.py      # assign_for_date — the engine
│   ├── notifications.py    # users_needing_send, mark_chore_response
│   ├── escalation.py       # build_action_plan (pure planner)
│   ├── stats.py            # completion rate + recent helpers
│   ├── scheduler.py        # 3 jobs: daily_assignment, notify_tick,
│   │                       # hourly_escalation. set_active_bot()
│   │                       # plumbs the live PTB bot into ticks.
│   ├── tg/
│   │   ├── bot.py          # Application builder + /start + callback
│   │   ├── notify.py       # daily-send tick, message format, keyboard
│   │   └── escalation.py   # apply_action_plan + hourly_tick
│   └── web/
│       ├── routes/         # auth, admin, admin_eligibility,
│       │                   # admin_assignments, admin_reports,
│       │                   # invites, me
│       └── templates/      # base.html + login.html + admin/*.html +
│                           # me*.html
└── tests/
    ├── conftest.py         # in-memory SQLite, FK pragma, schema
    │                       # create_all, seeded admin (hunter2),
    │                       # get_session override, client fixture
    ├── test_models.py      # 15 tests
    ├── test_admin.py       # 12 tests
    ├── test_invites.py     # 14 tests
    ├── test_eligibility.py # 13 tests
    ├── test_assignments.py #  7 tests
    ├── test_notifications.py # 17 tests
    ├── test_escalation.py  # 15 tests
    ├── test_reports.py     #  6 tests
    └── test_health.py      #  3 tests
                            # ───────────
                            # 102 tests total, all passing.
```

## Data model (8 tables)

- `users` — household members. `is_admin`, `is_escalation`, `active`,
  `send_time` ("HH:MM" check via SQLite GLOB), optional bcrypt
  `password_hash`, `telegram_chat_id` UNIQUE.
- `invite_codes` — one-shot codes the admin generates; primary key is
  the code itself. Has `is_expired` Python property.
- `chores` — name, frequency_days (>0), priority (0|1),
  estimated_minutes, enabled, created_by_user_id (members can add).
- `chore_eligibility` — composite PK (chore_id, user_id), mode
  ('allow'|'deny'). See resolver rules in `app/eligibility.py`
  docstring.
- `assignments` — one row per (chore, user, date). status in
  ('pending','completed','skipped','ignored','overdue','escalated').
  `escalated_from_user_id`, `rolled_over_from_assignment_id` link the
  graph.
- `reminder_events` — audit log for `daily_send`, `hourly_reminder`,
  `escalation`, `admin_notify`. Used as the idempotency token for the
  scheduler ticks.
- `magic_link_tokens` — short-lived (15 min default), one-shot.
- `app_settings` — generic key/value table for runtime tunables.

All datetimes UTC, stored timezone-aware. SQLite drops tz on read; we
re-coerce with `datetime.replace(tzinfo=timezone.utc)` where needed.
SQLite FK enforcement is enabled via a SQLAlchemy `connect` event
listener in `app/db.py` — required for `ondelete="CASCADE"` to fire.

## Phases shipped

| Phase | Scope | Tests |
|-------|-------|-------|
| 1 | FastAPI scaffold, Docker, .env config, /health | 3 |
| 2 | Models + Alembic initial migration | 15 |
| 3 | Admin login, layout shell, Chores/Members CRUD | 12 |
| 4 | Invite codes, magic-link auth, `/start <code>` Telegram linking | 14 |
| 5 | Per-chore eligibility editor + member /me/chores checkboxes | 13 |
| 6 | Assignment engine + APScheduler daily job + Run-now | 7 |
| 7 | Daily Telegram DMs + Done/Skip/Ignore inline buttons | 17 |
| 8 | Rollover, hourly reminders, escalation, admin notify | 15 |
| 9 | Admin /history (paginated) + /stats (completion rates) | 6 |

## Phases NOT shipped (Phase 10 polish)

Skipped on purpose to push the container out:

- Polished README walkthrough (the existing README is from Phase 1
  and is out of date — `HANDOFF.md` here is more current).
- Sample seed chores (a small `app/seed.py` invoked from the
  entrypoint when the chores table is empty would be a nice UX win).
- Backup/restore docs (the data is just `./data/choreizo.db` —
  mention copying that folder).
- An end-to-end walkthrough test that spins up TestClient and walks
  through invite → magic-link → /me/chores → run-assignment →
  callback. Each step is unit-tested today, but a combined e2e isn't.

Suggested next moves for Claude Code: tackle Phase 10 in roughly that
order. None of it changes the data model.

## Auth model (so you don't get tripped up)

- Sessions live in a Starlette signed cookie (`choreizo_session`).
- Admin password lives in `users.password_hash` (bcrypt). The
  bootstrap admin is created in `app/main.py:_bootstrap_admin()` from
  `ADMIN_PASSWORD` on first boot only.
- Members never set passwords. They get magic-link URLs DM'd by the
  bot (`/auth/magic/{token}`) which set the session.
- The `require_admin` dependency raises 401 (no session) / 403
  (non-admin). A global exception handler turns 401s into a 303
  redirect to `/login` only when `Accept: text/html` is set —
  API/JSON clients still see a real 401.

## Scheduler jobs

All three jobs are in `app/scheduler.py`, started in `lifespan`:

1. `daily_assignment` — cron at `DAILY_ASSIGNMENT_HOUR:00` in
   `HOUSE_TIMEZONE`. Calls `assign_for_date(today)`. Idempotent.
2. `notify_tick` — every minute. No-op when no bot is registered.
   Sends daily DMs when each user's `send_time` window opens.
   Skip-already-sent gating uses `reminder_events.kind='daily_send'`.
3. `hourly_escalation` — at minute 5 of every hour. No-op when no bot.
   Builds an `ActionPlan` then applies it: hourly reminders,
   escalations (creates new assignment under backup user, marks
   original 'escalated'), admin notifies, rollovers (high-priority
   only — marks original 'overdue', creates new for today).

Bot reference is set via `set_active_bot(bot)` in lifespan once the
PTB Application is up. In tests + bot-less dev runs, `_active_bot is
None` and the ticks are no-ops, so the scheduler running during tests
is harmless.

## Telegram bot

- `app/tg/bot.py` builds the PTB `Application` only if
  `TELEGRAM_BOT_TOKEN` is set. Two handlers: `CommandHandler("start")`
  → `redeem_start()` (pure logic), `CallbackQueryHandler` →
  `handle_chore_callback()` (also pure).
- Both pure cores accept a `(chat_id, …)` and use
  `AsyncSessionLocal` directly. **Tests monkeypatch
  `app.tg.bot.AsyncSessionLocal`** to point at the in-memory factory.
- Inline button callback_data format: `assign:{id}:{action}` where
  action ∈ {done, skip, ignore}. `app/tg/notify.callback_for/parse_callback`
  are the codec.

## Critical gotchas

### 1. SQLAlchemy async lazy-load
Async sessions can't lazy-load. After commit, **never** access
`obj.relationship` directly — either eager-load with `selectinload()`
in the query, or `await session.refresh(obj, ['relationship'])`. The
test in `test_models.py::test_assignment_full_lifecycle` uses
`selectinload` for this reason.

### 2. SQLite stores naive UTC
`DateTime(timezone=True)` works on insert but reads back naive.
`app/escalation.py` and `app/magic.py` have `_aware()` helpers that
re-coerce to UTC. New code touching datetimes from SQLite should do
the same.

### 3. FK pragma must be on
SQLite has FKs disabled by default. We turn them on per-connection in
`app/db.py` via a SQLAlchemy `event.listens_for(engine.sync_engine, "connect")`
hook. The test conftest does the same on the in-memory engine.

### 4. The escalation planner has priority interactions
In `build_action_plan`, the ROLLOVER branch `continue`s before the
escalation/admin_notify checks, on purpose — once a high-priority
chore rolls over, the new copy gets its own age clock. Tests for
escalation/admin-notify therefore use `priority=0` chores.

### 5. Module-level test hooks
Two routes use module-level callables that tests monkeypatch:
- `app.web.routes.admin_assignments._session_factory` (Run-now button)
- `app.web.routes.admin_assignments._send_now`           (Send-now button)

This pattern keeps tests free of live globals. Don't refactor away
without preserving the hook.

### 6. Default DB path is /data/choreizo.db
The default `DATABASE_PATH` is the container path. Local dev runs
either need DATABASE_PATH overridden or `/data` to exist. Tests use
`:memory:` via `conftest.py`.

## Running locally (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DATABASE_PATH=/tmp/choreizo.db
export ADMIN_PASSWORD=dev-password
export SESSION_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
alembic upgrade head
python -m app.main
```

## Running in Docker

```bash
cp .env.example .env       # then fill in TELEGRAM_BOT_TOKEN,
                           # ADMIN_PASSWORD, SESSION_SECRET
docker compose up -d --build
docker compose logs -f
```

The entrypoint (`docker-entrypoint.sh`) runs `alembic upgrade head`
then `python -m app.main`. State persists in `./data/`.

## Tests

```bash
pytest -q                  # all 102 tests, ~15s
pytest -v tests/test_X.py  # single file
```

Conftest sets `SESSION_SECRET=test-secret`, points DATABASE_PATH at a
throwaway file, and overrides `get_session` to use an in-memory engine
created per fixture. Each `client` fixture provisions a seeded admin
named **admin** with password **hunter2**.

## Configuration knobs (Pydantic Settings)

All come from env vars / `.env`:

- `TELEGRAM_BOT_TOKEN` — empty string disables the bot (tests use this)
- `ADMIN_PASSWORD` — bootstrap admin password (only used at first boot)
- `SESSION_SECRET` — cookie + magic-link signing key
- `BASE_URL` — used in magic-link URLs DM'd by the bot
- `HOUSE_TIMEZONE` — IANA name, default `America/Los_Angeles`
- `DAILY_ASSIGNMENT_HOUR` — int hour, default 6
- `HIGH_PRIORITY_REMINDER_INTERVAL_HOURS` — default 3
- `ESCALATION_AFTER_HOURS` — default 24
- `ADMIN_NOTIFY_AFTER_HOURS` — default 36
- `ROLLOVER_HIGH_PRIORITY` — default true
- `DATABASE_PATH` — default `/data/choreizo.db`
- `LOG_LEVEL` — default INFO

`get_settings()` is `lru_cache`d — restart the app to pick up new env
values.

## Testing the bot end-to-end

Once the container's up:

1. Visit http://localhost:8000, log in as `admin` with your
   `ADMIN_PASSWORD`.
2. Add a chore in **Chores** (e.g. "Vacuum", every 7 days, normal).
3. Go to **Invites**, mint a code with your name.
4. In Telegram, message your bot `/start <code>`. The bot links your
   `telegram_chat_id` to the new user and DMs you a magic-link. Click
   it to sign in to the web UI as that member.
5. From `/admin/assignments`, click **Run assignment now** to force
   today's assignments. Click **Send now** to push the daily DM
   immediately (otherwise it sends at your `send_time`).
6. Hit **Done** on a chore in Telegram — the message edits in place
   to "✅ Done" and the assignment row's status flips.

## Files modified frequently — known patterns

- Adding a route: drop a new module in `app/web/routes/`, register it
  in `app/main.py` (the `from app.web.routes import …` block plus an
  `app.include_router(...)`).
- Adding a template: `app/web/templates/admin/<name>.html`, extend
  `base.html`, pass `current_user` in the context dict so the nav
  renders correctly.
- Adding a migration: `alembic revision --autogenerate -m "msg"`,
  review the diff, commit. The Alembic env.py uses
  `app.config.get_settings().sqlite_sync_url` and
  `Base.metadata` from `app.db`.
- Adding a settings field: add to `app/config.py:Settings`, update
  `.env.example`, document here if it changes behaviour.

## Recent state

Last full test run: **102 passed in 18.30s**.
Last code change: container hardening — Dockerfile entrypoint runs
`alembic upgrade head` before `python -m app.main`; added
`docker-entrypoint.sh`, `.dockerignore`, `.gitignore`, `data/.gitkeep`.

---

If you're picking this up in Claude Code, a good first step is:

1. `pytest -q` to confirm all 102 tests still pass on your machine.
2. `docker compose up -d --build` to verify the container builds and
   the healthcheck goes green.
3. Walk through the "Testing the bot end-to-end" section above.
4. Then tackle Phase 10 polish (README rewrite + seed chores +
   backup docs) in whatever order you prefer.
