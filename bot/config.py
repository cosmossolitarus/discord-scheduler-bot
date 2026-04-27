"""
Configuration for the scheduling bot.
All configurable values in one place.
"""

from datetime import datetime, timezone, timedelta


# ─── Discord Roles ───────────────────────────────────────────────
ADMIN_ROLE = "admin"
PLAYER_ROLE = "player"

# ─── Discord Channels ───────────────────────────────────────────
SCHEDULING_CHANNEL = "scheduling"       # User-facing: submissions, reminders
SCHEDULE_LOG_CHANNEL = "schedule_log"   # Bot records, CSVs, archive
SCHEDULE_APPROVE_CHANNEL = "schedule_approve"  # Admin action queue

# ─── Cycle Timing ────────────────────────────────────────────────
# Anchor: Day 1 of a known event cycle, at 0:00 UTC.
# Every subsequent cycle is exactly 28 days later.
ANCHOR_DAY1 = datetime(2026, 4, 20, 0, 0, 0, tzinfo=timezone.utc)
CYCLE_LENGTH_DAYS = 28

# Submissions open 4 days before Day 0 (which is 1 day before Day 1).
# Day -4 in event terms (5 days before Day 1).
SUBMISSIONS_OPEN_OFFSET = timedelta(days=-5)

# Lock occurs at Day 0, 0:00 UTC = 1 day before Day 1.
LOCK_OFFSET = timedelta(days=-1)

# Archive at Day 7, 0:00 UTC = 6 days after Day 1.
ARCHIVE_OFFSET = timedelta(days=6)

# ─── Slot Configuration ─────────────────────────────────────────
SLOT_DURATION_MINUTES = 30
BOUNDARY_OFFSET_MINUTES = 15

# ─── Optimizer ───────────────────────────────────────────────────
AUTO_BUMP_THRESHOLD = 2.0
GENERIC_SPLIT = 3

# ─── Reminders ───────────────────────────────────────────────────
DAILY_REMINDER_OFFSET_HOURS = -1
PERSONAL_REMINDER_MINUTES = 15

# ─── Swap Timing ─────────────────────────────────────────────────
SWAP_USER_DEADLINE_MINUTES = 30
SWAP_ADMIN_DEADLINE_MINUTES = 0

# ─── LLM ─────────────────────────────────────────────────────────
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
LLM_RETRY_DELAY_SECONDS = 3600
