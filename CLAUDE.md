# Kingshot Scheduling Bot

A Discord bot that schedules players into 30-minute Chief Minister (CM) and Noble Advisor (NA) appointment slots during Kingshot KvK events, maximizing the kingdom's total score by matching players to slots based on their available Speedups.

---

## Game Domain

**Kingshot** is a mobile strategy game. During KvK (Kingdom vs Kingdom) events, players can hold minister positions that grant scoring bonuses. The two relevant positions are:

- **Chief Minister (CM)** — gives a bonus to construction (Day 1), research (Day 2), and training (Day 4) speedups.
- **Noble Advisor (NA)** — gives a bonus specifically relevant to Day 4 training speedups, and is considerably more valuable than CM on that day.

Players hold a position for a **30-minute slot**. During that window they use their in-game Speedups to score points. The bonus only applies while they hold the position, so being scheduled to the right slot — and using the right Speedup type — is what drives score.

### Game Days

The bot schedules Days 1, 2, and 4 only. Days 3 and 5 have no CM/NA-relevant Speedup bonuses.

| Day | Relevant Speedup | Track(s) |
|-----|-----------------|----------|
| Day 1 | Construction | CM only |
| Day 2 | Research | CM only |
| Day 4 | Training | NA (priority) + CM |

Day 4 has both tracks because both give a relevant bonus. NA is the better bonus, so the optimizer assigns the highest-priority players to NA first, then fills CM with the rest.

### Speedup Types

Each player submits a screenshot of their in-game Speedups page. The bot reads four values (in days):

| Field | Meaning |
|-------|---------|
| `speedup_construction` | Construction Speedups — used on Day 1 |
| `speedup_research` | Research Speedups — used on Day 2 |
| `speedup_training` | Training/Soldier Speedups — used on Day 4 |
| `speedup_general` | General (wildcard) Speedups — split equally across all three days |

`compute_priorities()` on the `Submission` model distributes `speedup_general / 3` into each day's priority score. The optimizer uses these priority scores to assign players.

### Slot Timing

All slots are **30 minutes** starting at :15 or :45 past the hour. Day 1 starts at **23:45 of Day 0** (the night before). This is intentional — it captures one extra player under the bonus by starting at the last 15 minutes of Day 0 which overlaps with the Day 1 bonus window.

Day 2 and Day 4 follow the same pattern (each starts at 23:45 of the previous day).

### The Boundary Slot (D1-CM-49)

Slot `D1-CM-49` runs **23:45–00:15** straddling the Day 1 / Day 2 midnight boundary. The game's active-day bonus transitions exactly at 00:00 UTC. The player in this slot:

- Uses **construction** Speedups from 23:45–00:00 (Day 1 bonus active)
- Switches to **research** Speedups from 00:00–00:15 (Day 2 bonus now active)

This player is excluded from Day 2 CM assignment since they already cover the Day 2 opening window. They receive a special notice in their DM and reminder.

### Slot Counts

| Pass | Slots |
|------|-------|
| Day 1 CM | 49 (includes boundary D1-CM-49) |
| Day 2 CM | 48 (starts at 00:15 — boundary covers 00:00) |
| Day 4 NA | 49 |
| Day 4 CM | 49 |
| **Total** | **195** |

---

## Bot Architecture

### Stack

- **Python 3.13**, **discord.py**, **SQLAlchemy (async)**, **asyncpg**
- **PostgreSQL** on Railway
- **Anthropic Claude** (`claude-sonnet-4-6`) for message parsing and screenshot extraction
- **scipy** (Hungarian algorithm) for slot assignment optimization

### Deployment

Deployed on **Railway**. `DATABASE_URL` is injected automatically by the Railway Postgres service. See `bot/config.py` for all env-var-overridable settings (`ADMIN_ROLE`, `PLAYER_ROLE`, channel names, etc.).

---

## Event Lifecycle

Each KvK cycle is **28 days** long, anchored to a known Day 1 date in `config.py`. The lifecycle loop ticks every minute and drives transitions automatically for real events. Test events are admin-controlled only.

```
(idle gap)
    │
    ▼  submissions_open (Day 1 - 5 days)
COLLECTING  ──── players submit availability + Speedups screenshot
    │
    ▼  lock (Day 1 - 1 day = "Day 0" midnight UTC)
LOCKED      ──── optimizer runs, schedule released to players via DM
    │
    ▼  archive (Day 1 + 6 days)
ARCHIVED    ──── final CSV posted to #schedule_log
```

Test events (`is_test=True`) never auto-transition and never block real event creation logic (actually they DO block it — a non-archived test event prevents a real event from being created). Use `/schedule reset confirm:True` to delete one.

---

## Player Submission Flow

Players interact entirely via **@mentions** in the scheduling channel. They need to provide two things:

1. **Availability** — at least one time window on Day 1, 2, or 4 (can be updated any time before lock).
2. **Speedups screenshot** — a photo of their in-game Speedups page. Claude vision parses the four values automatically.

A submission is **complete** when both are on file. Only complete submissions are considered by the optimizer at lock time.

### LLM Message Parsing (`bot/llm/`)

Every @mention goes through `process_user_message()` in `agent.py`. The flow is:

1. **Screenshots** are parsed first (silently) by `parse_screenshot()` in `screenshot.py`. This uses Claude with a structured tool call (`extract_speedups`) to extract the four Speedup totals from the image.
2. **Text** is parsed by a single LLM call with `tool_choice="any"`, forcing the model to emit one or more tool-use blocks representing the player's intent. The model never writes a free-text reply — only tool calls.
3. **Handlers** execute each parsed action and write to the DB.
4. **Render** — a single merged state summary is shown after any submission-touching action (availability set, screenshot parsed, query).

#### Pre-lock actions (COLLECTING phase)
- `set_availability` — sets or replaces availability windows for one day
- `query` — player asks about their current state
- `greet` / `out_of_scope` / `clarify` — utility intents

#### Post-lock actions (LOCKED phase)
- `move_slot` — request to change slot start time (requires admin ✅)
- `drop_slot` — request to give up an assignment (requires admin ✅)
- `request_new_slot` — request a slot for a day not currently assigned (requires admin ✅)
- `widen_availability` — add more available windows (applied immediately, no approval)
- `swap` — trade slots with another player (requires other player ✅, then admin ✅)

---

## Optimizer (`bot/optimizer/solver.py`)

Uses the **Hungarian algorithm** (`scipy.optimize.linear_sum_assignment`) for weighted bipartite matching. Runs in four sequential passes at lock time:

1. **Day 1 CM** — ranked by `priority_x` (construction + general share)
2. **Day 2 CM** — ranked by `priority_y` (research + general share), boundary player excluded
3. **Day 4 NA** — ranked by `priority_z` (training + general share)
4. **Day 4 CM** — ranked by `priority_z`, Day 4 NA winners excluded

Each player can hold at most one slot per day (one per pass). A tiny `epsilon * slot_index` term breaks ties in favour of earlier slots.

---

## Change Request Flow (post-lock)

Post-lock changes go through a `ChangeRequest` row rather than being applied directly.

- **move/drop/add**: creates `ChangeRequest` in `PENDING_ADMIN`, posts to `#schedule_approve` with ✅/❌ reactions. Admin reacts to approve or reject. On approval, `changes.py` applies the DB change and DMs the player.
- **swap**: creates `ChangeRequest` in `PENDING_CONFIRMATION`, DMs the other player (user B) with ✅/❌. On user B's ✅, transitions to `PENDING_ADMIN` and posts to `#schedule_approve` for admin final approval.

---

## Database Schema (key tables)

| Table | Purpose |
|-------|---------|
| `events` | One row per KvK cycle. Tracks phase, day1 date, test flag. |
| `slots` | 195 rows per event. Each is a 30-min window with a slot ID like `D1-CM-12`. |
| `submissions` | One row per player per event. Stores Speedup values, availability slot IDs, and computed priority scores. |
| `assignments` | One row per assigned slot. Created by the optimizer at lock time. |
| `change_requests` | Post-lock change queue. Tracks status through `PENDING_CONFIRMATION → PENDING_ADMIN → APPROVED/REJECTED`. |
| `audit_log` | Append-only log of all significant actions. |
| `sent_reminders` | Deduplication table so reminders aren't re-sent after a bot restart. |

---

## Discord Channels

| Channel (env-var) | Purpose |
|-------------------|---------|
| `SCHEDULING_CHANNEL` | Player-facing: @mentions, submissions, reminders |
| `SCHEDULE_LOG_CHANNEL` | Bot records: CSV exports after lock/changes/archive |
| `SCHEDULE_APPROVE_CHANNEL` | Admin queue: ✅/❌ reactions on change requests |

---

## Admin Commands (`/schedule`)

| Command | Description |
|---------|-------------|
| `status` | Active event phase, dates, submission stats |
| `pending` | List pending change requests |
| `lookup <player>` | Show one player's submission and assignment |
| `lock` | Force lock + run optimizer |
| `unlock confirm:True` | Roll LOCKED back to COLLECTING (keeps submissions, deletes assignments) |
| `archive` | Force archive the active event |
| `reset confirm:True` | Delete the active event and all associated data |
| `export` | Download CSV of the current schedule |
| `test [date]` | Create a test event in COLLECTING phase |

---

## Key Files

```
bot/
├── main.py                  Entry point, lifecycle loop, on_ready
├── config.py                All configurable values (timing, channels, roles, LLM model)
├── models.py                SQLAlchemy ORM models
├── database.py              Engine setup, init_db(), migrate_db()
├── events.py                create_event(), mark_locked(), mark_archived()
├── cycle.py                 Pure date math: slot generation, timing offsets, boundary check
│
├── cogs/
│   ├── submissions.py       on_message listener → dispatches to LLM agent
│   ├── scheduling.py        lock_and_release(), archive(), player DMs, CSV generation
│   ├── changes.py           Reaction listener for ✅/❌ approval flows
│   ├── reminders.py         Daily channel reminders + 15-min personal DMs
│   └── admin.py             /schedule slash commands
│
├── llm/
│   ├── agent.py             Main entry point: parse → dispatch → render
│   ├── screenshot.py        Claude vision extraction of Speedup values
│   ├── state.py             Builds state envelope passed to the LLM
│   ├── tools.py             Anthropic tool schemas for pre/post-lock actions
│   ├── handlers_collecting.py  Pre-lock action handlers
│   ├── handlers_locked.py   Post-lock action handlers (creates ChangeRequest rows)
│   ├── slots.py             Converts LLM time windows → slot IDs
│   └── utils.py             JSON extraction helper
│
└── optimizer/
    └── solver.py            Hungarian algorithm, four-pass assignment
```
