"""Compile model-selected activity relationships without inferring tasks from prose."""

from .contracts import (
    TimeFragmentExtractedAddOperation,
    TimeFragmentExtractedOperations,
    TimeFragmentModelOperations,
    TimeFragmentPlacement,
)
from .time_fragment import _round_minutes_to_slot
from .time_fragment_clocks import _minutes, _quote_spans


def compile_temporal_relations(
    extracted: TimeFragmentExtractedOperations, compiled: TimeFragmentModelOperations, text: str,
) -> tuple[TimeFragmentModelOperations, list[tuple[int, int, bool]], list[str]]:
    additions = {}
    duplicates = set()
    for index, operation in enumerate(extracted.operations):
        if isinstance(operation, TimeFragmentExtractedAddOperation):
            if operation.input_order in additions:
                duplicates.add(operation.input_order)
            additions[operation.input_order] = (index, operation)
    operations = list(compiled.operations)
    relations = []
    errors = []
    for relation in extracted.temporal_relations:
        before = additions.get(relation.before_input_order)
        after = additions.get(relation.after_input_order)
        if (before is None or after is None or before == after
                or relation.before_input_order in duplicates or relation.after_input_order in duplicates):
            errors.append("时间关系必须引用两个 inputOrder 唯一的新增任务，不能引用自身或不存在的任务")
            continue
        before_index, before_op = before
        after_index, after_op = after
        if not any(
            _overlaps_action(text, evidence_span, source_span)
            for evidence_span in _quote_spans(text, relation.evidence)
            for source_span in _quote_spans(text, before_op.source_text) + _quote_spans(text, after_op.source_text)
        ):
            errors.append(f"{before_op.title}→{after_op.title} 的 evidence 必须引用相关任务的原文肯定片段，允许只引用然后或完成后所在的任务片段，不要改写 sourceText")
            continue
        if relation.kind == "until":
            start = before_op.time_constraint
            finish = after_op.time_constraint
            end_clock = finish.start_time if finish is not None else None
            stated_end = start.end_time if start is not None else None
            if end_clock is None and stated_end is None:
                errors.append("until 需要前项结束时间或后项开始时间的原文依据；仅有先后要求时使用 before")
                continue
            if end_clock is not None and stated_end is not None and end_clock != stated_end:
                errors.append("until 与明确的起止时间冲突，不能覆盖用户给出的结束时间或跨越当天")
                continue
            if end_clock is not None and stated_end is None:
                end = _round_minutes_to_slot(_minutes(end_clock))
                if start is not None and start.start_time is not None:
                    duration = end - _round_minutes_to_slot(_minutes(start.start_time))
                    if duration <= 0:
                        errors.append("until 的结束时间必须晚于开始时间且属于当天")
                        continue
                    operations[before_index] = operations[before_index].model_copy(update={"duration_slots": duration})
                else:
                    operations[before_index] = operations[before_index].model_copy(
                        update={"placement": TimeFragmentPlacement(anchor="end", slot=end)},
                    )
        relations.append((before_index, after_index, relation.kind == "until"))
    # Any cycle is contradictory, even if the model reverses source appearance order.
    pending = {index for before, after, _ in relations for index in (before, after)}
    while pending:
        ready = {index for index in pending if not any(after == index and before in pending for before, after, _ in relations)}
        if not ready:
            errors.append("时间关系存在循环，任务不能互相要求对方先完成")
            break
        pending -= ready
    return TimeFragmentModelOperations(operations=operations), list(dict.fromkeys(relations)), errors


def _overlaps_action(text: str, evidence: tuple[int, int], source: tuple[int, int]) -> bool:
    start, end = max(evidence[0], source[0]), min(evidence[1], source[1])
    return start < end and bool(text[start:end].strip(" \t\n，,。；;！!？?、"))
