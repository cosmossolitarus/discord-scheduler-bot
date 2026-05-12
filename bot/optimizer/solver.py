"""
Scheduling optimizer using weighted bipartite matching.
Uses scipy.optimize.linear_sum_assignment (Hungarian algorithm).
"""

from dataclasses import dataclass
import numpy as np
from scipy.optimize import linear_sum_assignment


@dataclass
class UserForOptimizer:
    discord_id: int
    priority: float
    available_slots: set[str]


@dataclass
class SlotForOptimizer:
    slot_id: str
    slot_index: int


@dataclass
class AssignmentResult:
    discord_id: int
    slot_id: str
    priority: float


def optimize_pass(
    users: list[UserForOptimizer],
    slots: list[SlotForOptimizer],
) -> list[AssignmentResult]:
    if not users or not slots:
        return []

    n_users = len(users)
    n_slots = len(slots)

    max_slot_index = max(s.slot_index for s in slots)
    epsilon = 0.001 / max_slot_index if max_slot_index > 0 else 0.001

    LARGE = 1e12
    # Tiny floor so 0-priority users still compete weakly for leftover slots
    # rather than being filtered out entirely.
    FLOOR = 1e-6

    cost_matrix = np.full((n_users, n_slots), LARGE)

    for i, user in enumerate(users):
        for j, slot in enumerate(slots):
            if slot.slot_id in user.available_slots:
                effective_priority = max(user.priority, FLOOR)
                weight = effective_priority + epsilon * slot.slot_index * effective_priority
                cost_matrix[i, j] = -weight

    row_indices, col_indices = linear_sum_assignment(cost_matrix)

    results = []
    for row, col in zip(row_indices, col_indices):
        if cost_matrix[row, col] >= LARGE / 2:
            continue
        results.append(AssignmentResult(
            discord_id=users[row].discord_id,
            slot_id=slots[col].slot_id,
            priority=users[row].priority,
        ))

    return results


def run_full_optimization(
    submissions: list[dict],
    slots_by_pass: dict[str, list[SlotForOptimizer]],
) -> dict[str, list[AssignmentResult]]:
    results = {}

    # Pass 1: Day 1 CM
    d1_slot_set = {sl.slot_id for sl in slots_by_pass["D1-CM"]}
    d1_users = [
        UserForOptimizer(
            discord_id=s["discord_id"],
            priority=s["priority_x"],
            available_slots=s["availability"] & d1_slot_set,
        )
        for s in submissions
        if s["availability"] & d1_slot_set
    ]
    results["D1-CM"] = optimize_pass(d1_users, slots_by_pass["D1-CM"])

    # Boundary: last player from Day 1 gets first position of Day 2
    boundary_player = None
    for r in results["D1-CM"]:
        if r.slot_id == "D1-CM-49":
            boundary_player = r.discord_id
            break

    # Pass 2: Day 2 CM
    d2_slot_set = {sl.slot_id for sl in slots_by_pass["D2-CM"]}
    d2_users = [
        UserForOptimizer(
            discord_id=s["discord_id"],
            priority=s["priority_y"],
            available_slots=s["availability"] & d2_slot_set,
        )
        for s in submissions
        if s["discord_id"] != boundary_player
        and s["availability"] & d2_slot_set
    ]
    results["D2-CM"] = optimize_pass(d2_users, slots_by_pass["D2-CM"])

    if boundary_player is not None:
        boundary_sub = next(
            (s for s in submissions if s["discord_id"] == boundary_player), None
        )
        results["boundary"] = [AssignmentResult(
            discord_id=boundary_player,
            slot_id="D2-BOUNDARY",
            priority=boundary_sub["priority_y"] if boundary_sub else 0,
        )]
    else:
        results["boundary"] = []

    # Pass 3: Day 4 NA (priority track)
    d4na_slot_set = {sl.slot_id for sl in slots_by_pass["D4-NA"]}
    d4na_users = [
        UserForOptimizer(
            discord_id=s["discord_id"],
            priority=s["priority_z"],
            available_slots=s["availability"] & d4na_slot_set,
        )
        for s in submissions
        if s["availability"] & d4na_slot_set
    ]
    results["D4-NA"] = optimize_pass(d4na_users, slots_by_pass["D4-NA"])

    # Pass 4: Day 4 CM (exclude Pass 3 players)
    assigned_d4na = {r.discord_id for r in results["D4-NA"]}
    d4cm_slot_set = {sl.slot_id for sl in slots_by_pass["D4-CM"]}
    d4cm_users = [
        UserForOptimizer(
            discord_id=s["discord_id"],
            priority=s["priority_z"],
            available_slots=s["availability"] & d4cm_slot_set,
        )
        for s in submissions
        if s["discord_id"] not in assigned_d4na
        and s["availability"] & d4cm_slot_set
    ]
    results["D4-CM"] = optimize_pass(d4cm_users, slots_by_pass["D4-CM"])

    return results
