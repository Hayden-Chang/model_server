# Explicit clocks override the proposal default

With `earliestStartSlot=36` (09:00), a user asking for 08:30 previously received
an unscheduled task and an invalid proposal. A linked sequence could then fail
relation validation too, disabling Apply for the whole day.

The planner now lowers the current proposal's allocation floor to the earliest
valid requested task start. This includes an explicit end minus its duration,
and applies to untimed tasks in the same proposal. Explicit times already past
today are retained as requested. Without an earlier explicit clock, the existing
app default/current-time behavior remains. The request setting is not mutated.

Anchors are captured after source normalization and before relative insertion
derives placements. Only surviving scheduling targets can lower the floor;
invalid adds, unauthorized protected moves, existing history, duration-only edits
and cascades cannot. Existing moves must have a matching affirmative clock tied
to their target or original authorization quote. Both solver and fallback use
the same floor. Day bounds, collisions, fixed/completed/external protection,
relations and the 24:00 unscheduled Todo policy remain in force.

The model prompt preserves explicit early/past clocks instead of adjusting them
to a default. No task-specific meal, journey or work rule is added. The public
request/response schema, correction budgets and app persistence code are unchanged.

## Regression evidence

- New `test_explicit_time_priority.py`: related 08:30/09:00 proposals, same-day
  past clocks with/without an app default, untimed tasks and reversed model order,
  end anchors, existing moves, pinned collisions, fallback allocation, invalid
  or negated evidence, unchanged history, resize, relative insertion and midnight.
- Eight initial new cases failed on unchanged main `c759533`; two additional
  cases exposed invented/negated move clocks in the first candidate and then passed.
- Three previous assertions are deliberately updated for the new requirement:
  future explicit-before-default acceptance, same-day past adds with capacity
  leftovers, and retaining the past clock through semantic correction.
- Incremental selection covers the shared planner and its API, clock, relation,
  insertion and default-header consumers: 252 passing cases, including all 24
  unchanged client/server golden fixtures. No full repository suite is run.
- The companion iOS tests exercise a proposal before the configured default,
  including today before now, through session validation, production Apply and
  repository reload. The opt-in UI case uses a loopback candidate backend with
  deterministic model extraction; it does not send a request to production.

Local evidence records exact selectors, commands, pre-fix failures and candidate
results. Backend tests use deterministic model outputs; they do not establish
the general reliability of a live model or a production deployment.
