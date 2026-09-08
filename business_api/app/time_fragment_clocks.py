"""Validate model-selected clock evidence, then compile it to solver grid units.

Task recognition and boundary relationships belong to the model. This module
checks the selected source clocks; it does not split prose into inferred tasks.
"""

import re

from .contracts import (
    TimeFragmentExtractedAddOperation,
    TimeFragmentExtractedOperations,
    TimeFragmentModelAddOperation,
    TimeFragmentModelOperations,
    TimeFragmentPlacement,
    TimeFragmentPlanItem,
)
from .time_fragment import _CLOCK_PERIODS, _CLOCK_TOKEN_SOURCE, _parse_clock_token, _round_minutes_to_slot


_CLOCK = re.compile(rf"(?<![零〇一二两三四五六七八九十\d]){_CLOCK_TOKEN_SOURCE}(?!\d)")
_SEPARATOR = re.compile(r"[，,。；;！!？?\n]")
_NEGATION = re.compile(r"不要|别|无需|不许|不能|禁止")
_GLOBAL_START = re.compile(rf"^\s*(?:从|最早从|最早)\s*(?P<clock>{_CLOCK_TOKEN_SOURCE})\s*(?:开始|起|以后|之后)\s*(?:安排.*)?$")
_CLOCK_TO_ACTION = re.compile(r"(?:\s|的时候|安排|开始|进行|去|要|先|再|请|做|时)*")
_SHARED_ENDPOINT_TO_ACTION = re.compile(r"(?:\s|的时候|安排|开始|进行|去|要|先|请|做|时|给)*")
_RELATIVE_ENDPOINT_CONTEXT = re.compile(r"有空|稍后|以后|之后|后|再|等一会|(?:到家|回家)(?:以后|之后|后)")


def _clause_start(text: str, position: int) -> int:
    return max((match.end() for match in _SEPARATOR.finditer(text, 0, position)), default=0)


def _clause(text: str, position: int) -> str:
    start = _clause_start(text, position)
    following = _SEPARATOR.search(text, position)
    return text[start:following.start() if following else len(text)]


def _is_global_clock(text: str, position: int) -> bool:
    match = _GLOBAL_START.fullmatch(_clause(text, position))
    relative_position = position - _clause_start(text, position)
    return match is not None and match.start("clock") <= relative_position < match.end("clock")


def _possible_minutes(token: str) -> set[int]:
    parsed = _parse_clock_token(token)
    if parsed is None:
        return set()
    minutes, period = parsed
    allowed = {minutes}
    if period is None and 0 < minutes <= 12 * 60:
        allowed.add(minutes + 12 * 60)
    return allowed


def _quote_spans(text: str, quote: str) -> list[tuple[int, int]]:
    if not quote.strip():
        return []
    return [
        match.span() for match in re.finditer(re.escape(quote), text)
        if not _NEGATION.search(_clause(text, match.start()))
    ]


def _minutes(clock: str) -> int:
    hour, minute = map(int, clock.split(":"))
    return hour * 60 + minute


def _validate_boundary(
    text: str, clock: str, evidence: str, *, owner: str | None,
) -> tuple[int, int]:
    spans = _quote_spans(text, evidence)
    tokens = list(_CLOCK.finditer(evidence))
    if not spans or len(tokens) != 1:
        raise ValueError("时间依据必须是原文中肯定表述且只含一个钟点的片段")
    token = tokens[0]
    matching = []
    for start, _ in spans:
        evidence_start, evidence_end = start + token.start(), start + token.end()
        for original in _CLOCK.finditer(text):
            if original.end() != evidence_end or original.start() > evidence_start:
                continue
            prefix = text[original.start():evidence_start].strip()
            if prefix and prefix not in _CLOCK_PERIODS:
                continue
            if _is_global_clock(text, original.start()):
                continue
            # Only omitted AM/PM may be resolved by the model; explicit periods stay exact.
            if _minutes(clock) in _possible_minutes(original.group()):
                matching.append((original.span(), (evidence_start, evidence_end)))
    if owner is not None:
        owner_spans = _quote_spans(text, owner)
        matching = [pair for pair in matching if any(
            start <= pair[1][0] and pair[1][1] <= end for start, end in owner_spans
        )]
    matching = list(dict.fromkeys(pair[0] for pair in matching))
    if not matching:
        raise ValueError(f"输出时间 {clock} 与原文钟点不一致，或错误引用了全局开始时间")
    if len(matching) != 1:
        raise ValueError("时间依据对应多处原文，请引用能唯一定位该任务时间的完整片段")
    return matching[0]


def _has_omitted_source_clock(text: str, source: str) -> bool:
    for start, _ in _quote_spans(text, source):
        for token in _CLOCK.finditer(text, 0, start):
            if (
                _CLOCK_TO_ACTION.fullmatch(text[token.end():start])
                and not _is_global_clock(text, token.start())
                and not _NEGATION.search(_clause(text, token.start()))
            ):
                return True
    return False


def _existing_clock_slots(
    extracted: TimeFragmentExtractedOperations, target: TimeFragmentPlanItem,
) -> set[int]:
    slots = {slot for segment in target.segments for slot in (segment.start_slot, segment.end_slot)}
    changes = [op for op in extracted.operations if getattr(op, "target_item_id", None) == target.item_id]
    moves = [op for op in changes if op.type == "move"]
    durations = [op.duration_slots for op in changes if op.type == "changeDuration"]
    if not moves and not durations:
        return slots
    placement = moves[-1].placement if moves else (
        TimeFragmentPlacement(anchor="start", slot=target.segments[0].start_slot)
        if target.segments else None
    )
    if placement is not None:
        duration = durations[-1] if durations else target.duration_slots
        slots.add(placement.slot)
        slots.add(placement.slot + duration if placement.anchor == "start" else placement.slot - duration)
    return slots


def compile_time_fragment_clocks(
    extracted: TimeFragmentExtractedOperations,
    text: str,
    *,
    existing_items: list[TimeFragmentPlanItem],
    earliest_start_slot: int | None = None,
) -> tuple[TimeFragmentModelOperations, dict[int, str]]:
    operations = []
    errors: dict[int, str] = {}
    covered: set[tuple[int, int]] = set()
    resolved_clocks: dict[tuple[int, int], str] = {}
    existing = {item.item_id: item for item in existing_items}
    # A named baseline clock can identify an unchanged task in an after/before request.
    for token in _CLOCK.finditer(text):
        possible_slots = {_round_minutes_to_slot(value) for value in _possible_minutes(token.group())}
        for item in existing_items:
            reference = re.match(
                rf"\s*(?:的)?{re.escape(item.title)}(?:\s*(?:结束后|之后|以后|后|前)|[，,。；;\s]|$)",
                text[token.end():],
            )
            slots = {slot for segment in item.segments for slot in (segment.start_slot, segment.end_slot)}
            if reference and possible_slots & slots:
                covered.add(token.span())
    for index, operation in enumerate(extracted.operations):
        if not isinstance(operation, TimeFragmentExtractedAddOperation):
            operations.append(operation)
            target = existing.get(operation.target_item_id)
            if target is not None:
                slots = _existing_clock_slots(extracted, target)
                quote_spans = _quote_spans(text, operation.authorization_text or "")
                for token in _CLOCK.finditer(text):
                    clause = _clause(text, token.start())
                    quoted = any(start <= token.start() and token.end() <= end for start, end in quote_spans)
                    if target.title in clause or target.item_id in clause or quoted:
                        if any(_round_minutes_to_slot(value) in slots for value in _possible_minutes(token.group())):
                            covered.add(token.span())
            continue
        data = operation.model_dump(mode="json", by_alias=True)
        data.pop("sourceText")
        data.pop("timeConstraint")
        timing = operation.time_constraint
        # Older apps repeat their separate global constraint in the request text.
        # Clear only a start boundary quoted from that exact matching header.
        if earliest_start_slot is not None and timing is not None:
            prefix = f"从 {earliest_start_slot // 4:02}:{earliest_start_slot % 4 * 15:02} 开始\n"
            spans = _quote_spans(text, timing.start_evidence or "")
            if (
                text.startswith(prefix)
                and timing.start_time is not None
                and _minutes(timing.start_time) == earliest_start_slot * 15
                and len(list(_CLOCK.finditer(timing.start_evidence or ""))) == 1
                and spans
                and all(end <= len(prefix) for _, end in spans)
            ):
                timing = None if timing.end_time is None else timing.model_copy(
                    update={"start_time": None, "start_evidence": None},
                )
        placement = None
        try:
            if not _quote_spans(text, operation.source_text):
                raise ValueError("sourceText 必须是原文中的肯定任务片段")
            if timing is None:
                if _has_cropped_shared_endpoint(text, operation, extracted):
                    raise ValueError("任务引用裁掉了共享终点的时间上下文，请补全 sourceText 和该任务的时间依据")
                if _has_omitted_source_clock(text, operation.source_text) or any(
                    not _is_global_clock(operation.source_text, token.start())
                    and not _NEGATION.search(_clause(operation.source_text, token.start()))
                    for token in _CLOCK.finditer(operation.source_text)
                ):
                    raise ValueError("任务原文含明确钟点，不能返回 timeConstraint=null")
            else:
                for boundary_index, (clock, evidence) in enumerate((
                    (timing.start_time, timing.start_evidence),
                    (timing.end_time, timing.end_evidence),
                )):
                    if clock is not None:
                        assert evidence is not None
                        owner = operation.source_text if (
                            boundary_index == 0 or evidence in operation.source_text
                        ) else None
                        span = _validate_boundary(text, clock, evidence, owner=owner)
                        if span in resolved_clocks and resolved_clocks[span] != clock:
                            raise ValueError("同一原文钟点被多个任务引用时必须解析为同一个时间")
                        resolved_clocks[span] = clock
                        covered.add(span)
                if timing.start_time is not None:
                    assert timing.start_evidence is not None
                    if timing.start_evidence not in operation.source_text:
                        raise ValueError("开始时间依据必须属于该任务的 sourceText")
                    placement = TimeFragmentPlacement(
                        anchor="start", slot=_round_minutes_to_slot(_minutes(timing.start_time)),
                    )
                else:
                    assert timing.end_time is not None
                    assert timing.end_evidence is not None
                    if timing.end_evidence not in operation.source_text:
                        raise ValueError("单独结束时间依据必须属于该任务的 sourceText")
                    placement = TimeFragmentPlacement(
                        anchor="end", slot=_round_minutes_to_slot(_minutes(timing.end_time)),
                    )
                if timing.start_time is not None and timing.end_time is not None:
                    start = _minutes(timing.start_time)
                    end = _minutes(timing.end_time)
                    duration = _round_minutes_to_slot(end) - _round_minutes_to_slot(start)
                    if start >= end or duration <= 0:
                        raise ValueError("起止时间必须同日递增，量化后至少占一个时间格")
                    data["durationSlots"] = duration
        except ValueError as error:
            errors[index] = str(error)
        data["placement"] = placement
        operations.append(TimeFragmentModelAddOperation.model_validate(data))

    missing = [
        token.group() for token in _CLOCK.finditer(text)
        if token.span() not in covered
        and not _is_global_clock(text, token.start())
        and not _NEGATION.search(_clause(text, token.start()))
    ]
    if missing:
        errors[-1] = "原文明确钟点未被时间依据覆盖：" + "、".join(dict.fromkeys(missing))
    return TimeFragmentModelOperations(operations=operations), errors


def _has_cropped_shared_endpoint(
    text: str, operation: TimeFragmentExtractedAddOperation, extracted: TimeFragmentExtractedOperations,
) -> bool:
    # Locate the model's action quote independently of its display title or peer quote length.
    prefix = _SHARED_ENDPOINT_TO_ACTION.match(operation.source_text)
    if _RELATIVE_ENDPOINT_CONTEXT.match(operation.source_text, prefix.end()):
        return False
    for peer in extracted.operations:
        if not isinstance(peer, TimeFragmentExtractedAddOperation) or peer is operation:
            continue
        timing = peer.time_constraint
        if timing is None or timing.start_time is None or timing.end_time is None:
            continue
        assert timing.end_evidence is not None
        try:
            clock_start, clock_end = _validate_boundary(text, timing.end_time, timing.end_evidence, owner=peer.source_text)
        except ValueError:
            continue  # The peer's own validation reports malformed evidence.
        for source_start, _ in _quote_spans(text, operation.source_text):
            if _RELATIVE_ENDPOINT_CONTEXT.search(text[clock_end:source_start]):
                continue
            for evidence_start, evidence_end in _quote_spans(text, timing.end_evidence):
                if not evidence_start <= clock_start < clock_end <= evidence_end <= source_start:
                    continue
                if _SHARED_ENDPOINT_TO_ACTION.fullmatch(text[evidence_end:source_start]):
                    return True
    return False
