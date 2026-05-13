"""
Configuration for the scheduling bot.
All configurable values in one place.

Channel and role names are env-var-overridable. The defaults match the
current production names; set ADMIN_ROLE / PLAYER_ROLE / SCHEDULING_CHANNEL /
SCHEDULE_LOG_CHANNEL / SCHEDULE_APPROVE_CHANNEL on Railway (or in .env) to
override without editing this file.
"""

import os
from datetime import datetime, timezone, timedelta


# ─── Discord IDs ────────────────────────────────────────────────
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")  # Optional: enables instant slash command sync

# ─── Discord Roles ──────────────────────────────────────────────
# Case-sensitive Discord role names. Override via env vars if your server
# uses different names.
ADMIN_ROLE = os.environ.get("ADMIN_ROLE", "Admin")
PLAYER_ROLE = os.environ.get("PLAYER_ROLE", "Kingdom 231")

# ─── Discord Channels ───────────────────────────────────────────
# Channel names as Discord stores them (lowercased, hyphens; emojis allowed).
# Discord auto-lowercases what you type when creating a channel, so the
# stored name will already be in this form.
SCHEDULING_CHANNEL = os.environ.get(
    "SCHEDULING_CHANNEL", "🕰️kvk-scheduling"
)  # User-facing: @mentions, submissions, reminders
SCHEDULE_LOG_CHANNEL = os.environ.get(
    "SCHEDULE_LOG_CHANNEL", "🗓️kvk-set-schedules"
)  # Bot records: CSV exports after lock/changes/archive
SCHEDULE_APPROVE_CHANNEL = os.environ.get(
    "SCHEDULE_APPROVE_CHANNEL", "❗kvk-approve-schedules"
)  # Admin action queue: ✅/❌ reactions on change requests

# ─── Cycle Timing ───────────────────────────────────────────────
# Anchor: Day 1 of a known event cycle, at 0:00 UTC.
# Every subsequent cycle is exactly 28 days later.
ANCHOR_DAY1 = datetime(2026, 4, 16, 0, 0, 0, tzinfo=timezone.utc)
CYCLE_LENGTH_DAYS = 28

# Submissions open 5 days before Day 1 (= 4 days before Day 0 lock day).
SUBMISSIONS_OPEN_OFFSET = timedelta(days=-5)

# Lock occurs 1 day before Day 1 (= Day 0, 0:00 UTC).
LOCK_OFFSET = timedelta(days=-1)

# Archive 6 days after Day 1 (= Day 7, 0:00 UTC).
ARCHIVE_OFFSET = timedelta(days=6)

# ─── Slot Configuration ─────────────────────────────────────────
SLOT_DURATION_MINUTES = 30
BOUNDARY_OFFSET_MINUTES = 15

# ─── Optimizer ──────────────────────────────────────────────────
AUTO_BUMP_THRESHOLD = 2.0
GENERIC_SPLIT = 3

# ─── Reminders ──────────────────────────────────────────────────
PERSONAL_REMINDER_MINUTES = 15

# ─── Swap Timing ────────────────────────────────────────────────
SWAP_USER_DEADLINE_MINUTES = 30
SWAP_ADMIN_DEADLINE_MINUTES = 0

# ─── LLM ────────────────────────────────────────────────────────
ANTHROPIC_MODEL = "claude-sonnet-4-6"
LLM_RETRY_DELAY_SECONDS = 3600
