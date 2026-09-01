from dataclasses import dataclass
from time import monotonic
from typing import Literal, Sequence

from ortools.sat.python import cp_model


_DAY_SLOT_COUNT = 96
_ADMISSION_BATCH_SIZE = 30
_TOTAL_SOLVER_SECONDS = 0.25


@dataclass(frozen=True)
class SlotAllocationTarget:
    item_id: str
    duration_slots: int
    earliest_slot: int
    anchor: Literal["start", "end"] | None = None
    anchor_slot: int | None = None
    require_anchor: bool = True


def solve_slot_allocations(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
) -> dict[str, list[int]] | None:
    """Allocate one day with a deterministic fast path and CP-SAT fallback.

    Greedy allocation handles the common fully feasible case. When it cannot
    place every target, CP-SAT admits each target only when it can coexist with
    all higher-ranked targets and globally reassigns their slots. ``None`` means
    CP-SAT could not produce a reliable answer within its budget and lets the
    caller retain the existing deterministic result.
    """

    greedy_assignments = _greedy_assignments(occupied, targets)
    if greedy_assignments is not None:
        return greedy_assignments

    deadline = monotonic() + _TOTAL_SOLVER_SECONDS
    admitted: dict[str, bool] = {}
    for batch_start in range(0, len(targets), _ADMISSION_BATCH_SIZE):
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            return None
        batch_end = min(batch_start + _ADMISSION_BATCH_SIZE, len(targets))
        status, decisions = _solve_admission_batch(
            occupied,
            targets[:batch_end],
            admitted,
            batch_start=batch_start,
            deadline=deadline,
        )
        if status != "feasible":
            return None
        admitted.update(decisions)

    selected = [target for target in targets if admitted.get(target.item_id, False)]
    greedy_assignments = _greedy_assignments(occupied, selected)
    if greedy_assignments is not None:
        return greedy_assignments
    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        return None
    status, assignments = _solve_required_targets(
        occupied,
        selected,
        deadline=deadline,
    )
    return assignments if status == "feasible" else None


def _solve_admission_batch(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
    fixed_decisions: dict[str, bool],
    *,
    batch_start: int,
    deadline: float,
) -> tuple[Literal["feasible", "unknown"], dict[str, bool]]:
    model, scheduled_by_target = _build_optional_model(occupied, targets)
    for item_id, admitted in fixed_decisions.items():
        model.add(scheduled_by_target[item_id] == int(admitted))

    batch_targets = targets[batch_start:]
    model.maximize(
        sum(
            scheduled_by_target[target.item_id]
            * (1 << (len(batch_targets) - index - 1))
            for index, target in enumerate(batch_targets)
        )
    )
    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        return "unknown", {}
    solver = _new_solver(max_time_seconds=min(0.1, remaining_seconds))
    status = solver.solve(model)
    if status != cp_model.OPTIMAL:
        return "unknown", {}
    return "feasible", {
        target.item_id: bool(solver.value(scheduled_by_target[target.item_id]))
        for target in batch_targets
    }


def _build_optional_model(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
) -> tuple[cp_model.CpModel, dict[str, cp_model.IntVar]]:
    model = cp_model.CpModel()
    scheduled_by_target: dict[str, cp_model.IntVar] = {}
    variables_by_slot: dict[int, list[cp_model.IntVar]] = {
        slot: [] for slot in range(_DAY_SLOT_COUNT)
    }

    for index, target in enumerate(targets):
        scheduled = model.new_bool_var(f"item_{index}_scheduled")
        scheduled_by_target[target.item_id] = scheduled
        allowed_slots = _allowed_slots(occupied, target)
        slot_variables = {
            slot: model.new_bool_var(f"item_{index}_slot_{slot}")
            for slot in allowed_slots
        }
        model.add(sum(slot_variables.values()) == target.duration_slots * scheduled)
        for slot, variable in slot_variables.items():
            variables_by_slot[slot].append(variable)

        if len(allowed_slots) < target.duration_slots:
            model.add(scheduled == 0)
        if target.anchor is not None and target.require_anchor:
            anchor_slot = _required_anchor_slot(target)
            if anchor_slot not in slot_variables:
                model.add(scheduled == 0)
            else:
                model.add(slot_variables[anchor_slot] == scheduled)

    for slot_variables in variables_by_slot.values():
        if len(slot_variables) > 1:
            model.add(sum(slot_variables) <= 1)
    return model, scheduled_by_target


def _solve_required_targets(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
    *,
    deadline: float,
) -> tuple[Literal["feasible", "infeasible", "unknown"], dict[str, list[int]]]:
    if not targets:
        return "feasible", {}

    model = cp_model.CpModel()
    variables_by_target: dict[str, dict[int, cp_model.IntVar]] = {}
    variables_by_slot: dict[int, list[cp_model.IntVar]] = {
        slot: [] for slot in range(_DAY_SLOT_COUNT)
    }

    for target in targets:
        allowed_slots = _allowed_slots(occupied, target)
        if len(allowed_slots) < target.duration_slots:
            return "infeasible", {}

        slot_variables = {
            slot: model.new_bool_var(f"item_{len(variables_by_target)}_slot_{slot}")
            for slot in allowed_slots
        }
        variables_by_target[target.item_id] = slot_variables
        model.add(sum(slot_variables.values()) == target.duration_slots)
        for slot, variable in slot_variables.items():
            variables_by_slot[slot].append(variable)

        if target.anchor is not None and target.require_anchor:
            anchor_slot = _required_anchor_slot(target)
            if anchor_slot not in slot_variables:
                return "infeasible", {}
            model.add(slot_variables[anchor_slot] == 1)

    for slot_variables in variables_by_slot.values():
        if len(slot_variables) > 1:
            model.add(sum(slot_variables) <= 1)

    _add_placement_objective(model, targets, variables_by_target)

    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        return "unknown", {}
    solver = _new_solver(max_time_seconds=min(0.1, remaining_seconds))
    status = solver.solve(model)
    if status == cp_model.UNKNOWN:
        return "unknown", {}
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return "infeasible", {}

    return "feasible", {
        target.item_id: [
            slot
            for slot, variable in variables_by_target[target.item_id].items()
            if solver.value(variable)
        ]
        for target in targets
    }


def _new_solver(*, max_time_seconds: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = max_time_seconds
    return solver


def _required_anchor_slot(target: SlotAllocationTarget) -> int | None:
    if target.anchor == "start":
        return target.anchor_slot
    return target.anchor_slot - 1 if target.anchor_slot is not None else None


def _greedy_assignments(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
) -> dict[str, list[int]] | None:
    current_occupied = list(occupied)
    assignments: dict[str, list[int]] = {}
    for target in targets:
        allowed_slots = _allowed_slots(current_occupied, target)
        if target.anchor == "end":
            allowed_slots.reverse()
        if target.anchor is not None and target.require_anchor:
            anchor_slot = _required_anchor_slot(target)
            if anchor_slot not in allowed_slots:
                return None
        selected_slots = allowed_slots[: target.duration_slots]
        if len(selected_slots) != target.duration_slots:
            return None
        selected_slots.sort()
        assignments[target.item_id] = selected_slots
        for slot in selected_slots:
            current_occupied[slot] = True
    return assignments


def _allowed_slots(
    occupied: Sequence[bool],
    target: SlotAllocationTarget,
) -> list[int]:
    if target.anchor == "start" and target.anchor_slot is not None:
        start, end = max(target.earliest_slot, target.anchor_slot), _DAY_SLOT_COUNT
    elif target.anchor == "end" and target.anchor_slot is not None:
        start, end = target.earliest_slot, min(target.anchor_slot, _DAY_SLOT_COUNT)
    else:
        start, end = target.earliest_slot, _DAY_SLOT_COUNT
    return [slot for slot in range(start, end) if not occupied[slot]]


def _add_placement_objective(
    model: cp_model.CpModel,
    targets: Sequence[SlotAllocationTarget],
    variables_by_target: dict[str, dict[int, cp_model.IntVar]],
) -> None:
    position_terms: list[cp_model.LinearExpr] = []
    target_count = len(targets)

    for index, target in enumerate(targets):
        rank_weight = target_count - index
        slot_variables = variables_by_target[target.item_id]
        ordered_slots = sorted(slot_variables)
        preferred_slot = max(ordered_slots) if target.anchor == "end" else min(ordered_slots)

        for slot in ordered_slots:
            current = slot_variables[slot]
            distance = abs(slot - preferred_slot)
            if distance:
                position_terms.append(current * distance * rank_weight)
    model.minimize(sum(position_terms))
