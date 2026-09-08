from dataclasses import dataclass, replace
from time import monotonic
from typing import Literal, Sequence

from ortools.sat.python import cp_model


_DAY_SLOT_COUNT = 96
_ADMISSION_BATCH_SIZE = 30
_MAX_SOLVE_SECONDS = 0.25
_TOTAL_SOLVER_SECONDS = 0.9


@dataclass(frozen=True)
class SlotAllocationTarget:
    item_id: str
    duration_slots: int
    earliest_slot: int
    anchor: Literal["start", "end"] | None = None
    anchor_slot: int | None = None
    require_anchor: bool = True


@dataclass(frozen=True)
class SlotAllocationRelation:
    before_id: str
    after_id: str
    adjacent: bool = False


def solve_slot_allocations(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
    relations: Sequence[SlotAllocationRelation] = (),
) -> dict[str, list[int]] | None:
    """Allocate one day with a deterministic fast path and CP-SAT fallback.

    Greedy allocation handles the common fully feasible case. When it cannot
    place every target, CP-SAT admits each target only when it can coexist with
    all higher-ranked targets and globally reassigns their slots. ``None`` means
    CP-SAT could not produce a reliable answer within its budget and lets the
    caller retain the existing deterministic result.
    """

    greedy_assignments = _greedy_assignments(occupied, targets, relations)
    if greedy_assignments is not None and relations_hold(greedy_assignments, relations):
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
            targets if relations else targets[:batch_end],
            admitted,
            batch_start=batch_start,
            deadline=deadline,
            relations=relations,
        )
        if status != "feasible":
            return None
        admitted.update(decisions)

    selected = [target for target in targets if admitted.get(target.item_id, False)]
    greedy_assignments = _greedy_assignments(occupied, selected, relations)
    if greedy_assignments is not None and relations_hold(greedy_assignments, relations):
        return greedy_assignments
    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        return None
    status, assignments = _solve_required_targets(
        occupied,
        selected,
        deadline=deadline,
        relations=relations,
    )
    return assignments if status == "feasible" else None


def _solve_admission_batch(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
    fixed_decisions: dict[str, bool],
    *,
    batch_start: int,
    deadline: float,
    relations: Sequence[SlotAllocationRelation] = (),
) -> tuple[Literal["feasible", "unknown"], dict[str, bool]]:
    model, scheduled_by_target = _build_optional_model(occupied, targets, relations)
    for item_id, admitted in fixed_decisions.items():
        model.add(scheduled_by_target[item_id] == int(admitted))

    batch_targets = targets[batch_start:batch_start + _ADMISSION_BATCH_SIZE]
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
    solver = _new_solver(max_time_seconds=min(_MAX_SOLVE_SECONDS, remaining_seconds))
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
    relations: Sequence[SlotAllocationRelation] = (),
) -> tuple[cp_model.CpModel, dict[str, cp_model.IntVar]]:
    model = cp_model.CpModel()
    scheduled_by_target: dict[str, cp_model.IntVar] = {}
    variables_by_target: dict[str, dict[int, cp_model.IntVar]] = {}
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
        variables_by_target[target.item_id] = slot_variables
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
    _add_relation_constraints(model, variables_by_target, relations, scheduled_by_target)
    return model, scheduled_by_target


def _solve_required_targets(
    occupied: Sequence[bool],
    targets: Sequence[SlotAllocationTarget],
    *,
    deadline: float,
    relations: Sequence[SlotAllocationRelation] = (),
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

    _add_relation_constraints(model, variables_by_target, relations)
    _add_placement_objective(model, targets, variables_by_target)

    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        return "unknown", {}
    solver = _new_solver(max_time_seconds=min(_MAX_SOLVE_SECONDS, remaining_seconds))
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
    relations: Sequence[SlotAllocationRelation] = (),
) -> dict[str, list[int]] | None:
    if relations:
        ordered: list[SlotAllocationTarget] = []
        by_id = {target.item_id: target for target in targets}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item_id: str) -> bool:
            if item_id in visited:
                return True
            if item_id in visiting or item_id not in by_id:
                return False
            visiting.add(item_id)
            for relation in relations:
                if relation.after_id == item_id and not visit(relation.before_id):
                    return False
            visiting.remove(item_id)
            visited.add(item_id)
            ordered.append(by_id[item_id])
            return True

        if not all(visit(target.item_id) for target in targets):
            return None
        targets = ordered
    current_occupied = list(occupied)
    assignments: dict[str, list[int]] = {}
    for target in targets:
        previous_ends = [max(assignments[relation.before_id]) + 1 for relation in relations
                         if relation.after_id == target.item_id]
        if previous_ends:
            target = replace(target, earliest_slot=max(target.earliest_slot, *previous_ends))
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


def relations_hold(assignments: dict[str, list[int]], relations: Sequence[SlotAllocationRelation]) -> bool:
    for relation in relations:
        before, after = assignments.get(relation.before_id, []), assignments.get(relation.after_id, [])
        if not before and not after:
            continue
        if not before or not after or max(before) >= min(after):
            return False
        if relation.adjacent and max(before) + 1 != min(after):
            return False
    return True


def _add_relation_constraints(
    model: cp_model.CpModel,
    variables: dict[str, dict[int, cp_model.IntVar]],
    relations: Sequence[SlotAllocationRelation],
    scheduled: dict[str, cp_model.IntVar] | None = None,
) -> None:
    boundaries = {}
    for relation in relations:
        if relation.before_id not in variables or relation.after_id not in variables:
            continue
        for item_id in (relation.before_id, relation.after_id):
            if item_id not in boundaries:
                slots = variables[item_id]
                start = model.new_int_var(0, _DAY_SLOT_COUNT, f"{item_id}_start")
                end = model.new_int_var(0, _DAY_SLOT_COUNT, f"{item_id}_end")
                model.add_min_equality(start, [slot + _DAY_SLOT_COUNT * (1 - value) for slot, value in slots.items()] + [_DAY_SLOT_COUNT])
                model.add_max_equality(end, [(slot + 1) * value for slot, value in slots.items()] + [0])
                boundaries[item_id] = start, end
        before_end = boundaries[relation.before_id][1]
        after_start = boundaries[relation.after_id][0]
        constraint = model.add(before_end == after_start if relation.adjacent else before_end <= after_start)
        if scheduled is not None:
            # Admit a connected sequence together; never silently drop its prerequisite.
            model.add(scheduled[relation.before_id] == scheduled[relation.after_id])
            constraint.only_enforce_if(scheduled[relation.before_id])
