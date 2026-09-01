from app.time_fragment_solver import SlotAllocationTarget, solve_slot_allocations


def test_admission_order_is_preserved_across_solver_batches() -> None:
    occupied = [slot >= 30 for slot in range(96)]
    targets = [
        SlotAllocationTarget(
            item_id=f"task-{index}",
            duration_slots=1,
            earliest_slot=0,
        )
        for index in range(31)
    ]

    assignments = solve_slot_allocations(occupied, targets)

    assert assignments is not None
    assert list(assignments) == [f"task-{index}" for index in range(30)]
    assert sorted(slot for slots in assignments.values() for slot in slots) == list(range(30))


def test_placement_respects_strict_admission_order() -> None:
    durations = [2, 1, 2, 1, 1, 1, 1, 1, 1, 2]
    targets = [
        SlotAllocationTarget(
            item_id=f"task-{index}",
            duration_slots=duration,
            earliest_slot=64,
        )
        for index, duration in enumerate(durations)
    ]

    assignments = solve_slot_allocations([False] * 96, targets)

    assert assignments is not None
    next_slot = 64
    for target, duration in zip(targets, durations):
        assert assignments[target.item_id] == list(range(next_slot, next_slot + duration))
        next_slot += duration
