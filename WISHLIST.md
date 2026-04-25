# Choreizo — Feature Wishlist

Ideas and future improvements. Not committed to or prioritized yet.

---

## UX & Member Experience

- **Build out user dashboard** — today's chores with inline Done/Skip/Ignore buttons on the web UI (no Telegram required), completion history, upcoming chores view
- **Member edit profile** — let members change their own send time and display name from the web UI

## Admin

- **Bulk assignment override** — reassign a chore to a different person from the assignments page (not just status change)
- **Run assignment for a specific date** — useful for testing or catching up after downtime
- **Chore history per chore** — click a chore and see all past assignments, completion rate, who does it most

## Scheduling

- **Time-of-day windows** — assign chores only during certain hours (e.g. "take out trash" only in the evening)
- **Blackout dates** — skip assignments on holidays or user-defined dates
- **Chore pools** — group chores so only one from the pool fires per cycle (e.g. "clean one bathroom per week")

## Notifications & Telegram

- **Configurable magic link TTL** — currently hardcoded at 15 min; expose in admin settings
- **Quiet hours** — don't send reminders between certain hours
- **/upcoming command** — show the next 7 days of likely assignments in Telegram
- **Completion streaks** — send a congratulatory message when a member completes chores N days in a row

## Reliability & Ops

- **Backup/restore docs** — document that state is just `./data/choreizo.db`; one-liner copy to back up
- **Health dashboard tile** — show last scheduler run time and next scheduled run in admin UI
- **Seed chores on first boot** — optional starter set (vacuum, litter, trash, HVAC filter, etc.) when the chores table is empty

## Longer-term / Bigger lifts

- **Multi-member chores** — assign the same chore to more than one person per day
- **Gamification** — completion streaks, fairness leaderboard, points
- **Mobile-friendly admin** — current Pico.css layout is functional but not optimized for phones
- **End-to-end test** — a single pytest that walks invite → magic-link → /me/chores → run-assignment → Telegram callback
