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
