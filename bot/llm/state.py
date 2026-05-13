"""
State envelope construction.

Given an inbound message + event + DB session, builds the structured state
the LLM needs to make decisions. The envelope contains:

  - the user's submission status (screenshot, availability summary, resources)
  - their current assignments (post-lock only)
  - the list of @mentioned discord IDs that are valid swap partners (post-lock)
  - basic event info (day1 date, phase)

We deliberately do NOT include other players' data. For swap requests, the
user must @mention the target; we resolve those mentions against the event's
assigned-users set so the LLM sees only the relevant subset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import GENERIC_SPLIT
from bot.llm.slots import summarize_availability
from bot.models import Assignment, EventPhase, Slot, Submission

if TYPE_CHECKING:  # avoid runtime discord dep
    import discord
    from bot.models import Event


async def build_state_envelope(
    session: AsyncSession,
    event: "Event",
    message: "discord.Message",
) -> dict:
    """Return the state dict the LLM sees alongside the user's message."""
    event_id = event.event_id
    day1 = event.day1_date
    user_id = message.author.id

    # User's submission (if any)
    sub_result = await session.execute(
        select(Submission).where(
            Submission.event_id == event_id,
            Submission.discord_id == user_id,
        )
    )
    submission = sub_result.scalar_one_or_none()

    submission_dict: dict = {
        "has_screenshot": bool(submission and submission.has_screenshot),
        "has_availability": bool(submission and submission.has_availability),
        "resources": None,
        "availability_summary": None,
    }
    if submission and submission.has_screenshot:
        submission_dict["resources"] = {
            "construction_days": submission.resource_x or 0,
            "research_days": submission.resource_y or 0,
            "troops_days": submission.resource_z or 0,
            "general_days": submission.resource_generic or 0,
            "generic_split": GENERIC_SPLIT,
        }
    if submission and submission.has_availability:
        submission_dict["availability_summary"] = summarize_availability(
            day1, submission.availability or []
        )

    state: dict = {
        "phase": event.phase.value,
        "is_test_event": event.is_test,
        "event": {
            "day1_date": day1.strftime("%Y-%m-%d"),
            "day1_weekday": day1.strftime("%A"),
        },
        "user": {
            "discord_id": user_id,
            "display_name": getattr(message.author, "display_name", str(message.author)),
        },
        "submission": submission_dict,
    }

    # Post-lock additions: current assignments + valid swap partners
    if event.phase == EventPhase.LOCKED:
        assn_result = await session.execute(
            select(Assignment, Slot)
            .join(Slot, Assignment.slot_id == Slot.slot_id)
            .where(
                Assignment.event_id == event_id,
                Assignment.discord_id == user_id,
            )
            .order_by(Slot.day, Slot.track.desc(), Slot.slot_index)
        )
        rows = assn_result.all()

        state["assignments"] = [
            {
                "day": slot.day,
                "track": slot.track,
                "track_label": "Noble Advisor" if slot.track == "NA" else "Chief Minister",
                "start_utc": slot.start_time.strftime("%H:%M"),
                "end_utc": slot.end_time.strftime("%H:%M"),
                "date": slot.start_time.strftime("%Y-%m-%d"),
                "is_boundary": slot.slot_id == "D1-CM-49",
                "slot_id": slot.slot_id,  # for handler use; not surfaced to user
            }
            for _, slot in rows
        ]

        # Valid swap partners = @mentioned non-bot users who ALSO have an
        # assignment in this event.
        mentioned_ids = [m.id for m in message.mentions if not m.bot]
        if mentioned_ids:
            mres = await session.execute(
                select(Assignment.discord_id).distinct().where(
                    Assignment.event_id == event_id,
                    Assignment.discord_id.in_(mentioned_ids),
                )
            )
            valid_ids = set(mres.scalars().all())
        else:
            valid_ids = set()

        state["valid_swap_partners"] = [
            {
                "discord_id": m.id,
                "display_name": m.display_name,
            }
            for m in message.mentions
            if m.id in valid_ids
        ]
    else:
        state["assignments"] = []
        state["valid_swap_partners"] = []

    return state


def render_state_for_prompt(state: dict) -> str:
    """Render the state dict as a human-readable block for the system prompt.

    JSON would also work but a labelled-text block is easier for the LLM to
    digest and avoids escaping issues with @mentions and other special chars.
    """
    parts = [
        f"PHASE: {state['phase']}{' (TEST EVENT)' if state['is_test_event'] else ''}",
        f"EVENT DAY 1: {state['event']['day1_weekday']}, {state['event']['day1_date']} (00:00 UTC)",
        f"USER: {state['user']['display_name']} (id={state['user']['discord_id']})",
    ]

    sub = state["submission"]
    parts.append("")
    parts.append("SUBMISSION:")
    parts.append(f"  Screenshot on file: {'yes' if sub['has_screenshot'] else 'no'}")
    parts.append(f"  Availability on file: {'yes' if sub['has_availability'] else 'no'}")
    if sub["resources"]:
        r = sub["resources"]
        parts.append(
            f"  Resources (in days): construction={r['construction_days']:.2f}, "
            f"research={r['research_days']:.2f}, troops={r['troops_days']:.2f}, "
            f"general={r['general_days']:.2f} (general is split by {r['generic_split']})"
        )
    if sub["availability_summary"]:
        parts.append("  Current availability:")
        for line in sub["availability_summary"].split("\n"):
            parts.append(f"    {line}")

    if state["phase"] == "locked":
        parts.append("")
        parts.append("CURRENT ASSIGNMENTS:")
        if not state["assignments"]:
            parts.append("  (none — user is on the waitlist)")
        else:
            for a in state["assignments"]:
                label = f"Day {a['day']} ({a['track_label']})"
                boundary = " [BOUNDARY SLOT: 1st 15 min uses Day 1 resources, last 15 min uses Day 2 resources]" if a["is_boundary"] else ""
                parts.append(
                    f"  {label}: {a['start_utc']}-{a['end_utc']} UTC on {a['date']}{boundary}"
                )

        if state["valid_swap_partners"]:
            parts.append("")
            parts.append("VALID SWAP PARTNERS IN THIS MESSAGE:")
            for p in state["valid_swap_partners"]:
                parts.append(f"  {p['display_name']} (id={p['discord_id']})")
        else:
            parts.append("")
            parts.append("VALID SWAP PARTNERS IN THIS MESSAGE: (none — user did not @mention an assigned player)")

    return "\n".join(parts)
