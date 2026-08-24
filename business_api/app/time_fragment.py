from datetime import datetime

from .contracts import TimeFragmentPlanResponse
from .postprocessors import ModelOutputInvalid


def validate_plan_for_request(plan: TimeFragmentPlanResponse, now: str) -> None:
    local_day = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    scheduled: list[tuple[datetime, datetime]] = []

    for task in plan.tasks:
        if task.start is None or task.end is None:
            continue
        try:
            start = datetime.fromisoformat(task.start)
            end = datetime.fromisoformat(task.end)
        except ValueError as error:
            raise ModelOutputInvalid("task time is not a valid ISO 8601 datetime") from error
        if (
            start.strftime("%Y-%m-%dT%H:%M:%S") != task.start
            or end.strftime("%Y-%m-%dT%H:%M:%S") != task.end
        ):
            raise ModelOutputInvalid("task times must use local YYYY-MM-DDTHH:MM:SS format")
        if start.tzinfo is not None or end.tzinfo is not None:
            raise ModelOutputInvalid("task times must be local datetimes without timezone suffixes")
        if start.date() != local_day or end.date() != local_day:
            raise ModelOutputInvalid("task falls outside the request's local day")
        if start >= end:
            raise ModelOutputInvalid("task start must be before end")
        alignment_parts = (
            start.minute % 15,
            end.minute % 15,
            start.second,
            end.second,
            start.microsecond,
            end.microsecond,
        )
        if any(alignment_parts):
            raise ModelOutputInvalid("task times must align to 15-minute boundaries")
        scheduled.append((start, end))

    if scheduled != sorted(scheduled):
        raise ModelOutputInvalid("scheduled tasks must be sorted by start time")
    for (_, previous_end), (next_start, _) in zip(scheduled, scheduled[1:]):
        if previous_end > next_start:
            raise ModelOutputInvalid("scheduled tasks must not overlap")
