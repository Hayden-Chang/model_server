# AI clock extraction repair and locked cases

## Scope and root cause

The reported full-day narrative produced wrong integer slots, omitted time
evidence, and was normalized into untimed tasks. A future empty day then started
at midnight and incorrectly returned `valid=true`. Supplying a real 08:50 quote
with the wrong 13:00 slot also passed. The prompt-required combined commute title
could not pass a validator requiring that title verbatim in the original text.

The original extraction repair changed the model-facing add schema and its
compilation to existing solver operations. On September 8, the user additionally
approved retaining a midnight-start new task as an explained unscheduled task
without blocking the other valid tasks. The public schema, solver algorithm and
production model selection stay unchanged; the authorized boundary golden and
the iOS local validator must now follow that warning-only contract.

- Wire adds require `sourceText` and explicit nullable `timeConstraint`; timed
  boundaries require HH:mm plus source evidence. Old add slots are rejected at
  the live parser, never treated as a compatibility fallback.
- Source references are verified at their original positions, including period
  prefixes; repeated short quotes cannot cover multiple independent clocks.
- Shared endpoint quotes may anchor both a journey end and a following activity
  start, but the same original clock span must resolve to the same HH:mm.
- A narrow reject-only guard detects the captured dinner quote-clipping failure:
  a model-selected timed source contains an untimed source, whose uniquely
  quoted literal title directly follows that source's valid endpoint evidence
  through conservative grammatical connectors. It requests corrected context;
  it never assigns the previous clock. Explicit flexibility/relative connectors,
  independent sources and summarized titles without a literal match are outside
  this guard. This is not a general natural-language completeness guarantee.
- Global starts are scoped to their own clock, not all clocks in that sentence.
- Existing named time references are checked against currentPlan; they do not
  turn relative insertions into new per-task clock placements.
- Invalid extracted tasks remain in the candidate with empty segments and an
  error. Only errors trigger the existing single thinking-enabled correction.
- Ordinary past/capacity warnings still retain tasks without a second call.
- Existing operation authorization, priorities, insertion/cascade, and public
  serialization remain on their existing code paths.

## Locked business cases

Selectors below are in `business_api/tests`; parameterized selectors include all
of their variants. API fixtures now explicitly describe model extraction; no
test helper parses the prose or invokes production recovery to fabricate it.

| Case | Locked result | Selector |
|---|---|---|
| 11:10, five explicit ranges | 11:30–12:00 王者; 12–13 午饭; 13–14 休息; 14–18 逛街; 18–20 KTV, one call | `test_time_fragment_api.py::test_inline_explicit_time_ranges_preserve_each_requested_interval` |
| Chinese ranges | 08:45–09:45 地铁; 09:45–10 背词; 10–12 收尾; 12–12:30 看书; 12:30–14 饭休; 14–17 短视频; 17–19 待定 | `test_time_fragment_api.py::test_explicit_chinese_time_ranges_preserve_intervals_over_model_duration` |
| Continuous commute narrative | Eight intervals; nearest-quarter rounding; two meal defaults; commute endpoints not extra tasks; 09:45–12 and 19–19:30 remain gaps | `test_time_fragment_api.py::test_continuous_chinese_timepoints_round_and_return_only_intended_intervals` |
| Global start plus priorities | A1–A5, B1–B4, C1 from 16:00; no per-task anchors invented | `test_time_fragment_api.py::test_global_start_schedules_untimed_adds_by_requested_priority` |
| Complete labels without explicit relation | Same natural precedence and scores 10 through 1 | `test_time_fragment_api.py::test_complete_priority_labels_use_natural_order_without_relation` |
| Past and insufficient capacity | All 14 tasks retained; four unplaced; warnings only; one call | `test_time_fragment_api.py::test_past_and_capacity_unplaced_adds_remain_in_first_candidate_without_correction` |
| Correction and ordinary warnings together | Only UNKNOWN_TARGET sent for correction; legal past task retained in corrected candidate | `test_time_fragment_api.py::test_semantic_correction_excludes_normal_unplaced_warnings` |
| Relative insertion and cascade | Existing report, pinned/external blocks, explicit moves, and unaffected gaps preserved | `test_time_fragment_planner.py::test_explicit_add_cascades_following_tasks_around_pinned_and_external_blocks` and adjacent mapped insertion cases |
| Shared client/server goldens | Only the authorized midnight-add fixture and its hash change; the other 23 fixtures remain untouched | `test_time_fragment_planner.py::test_time_fragment_planner_golden_fixture`, `test_golden_fixture_manifest_matches_exact_bytes_and_case_set` |
| Current full-day report | All 12 stated anchors preserved; combined commute 19:30–20:00; 24:00 sleep remains unscheduled with warnings, never noon or next-day placement; no retry | `test_time_fragment_clock_extraction.py::test_entire_reported_day_preserves_clock_anchors_and_flags_only_midnight_boundary` |
| Actual Simulator first model output | Replay captured raw output unchanged: same 13 candidate tasks/operations/segments, but valid warning-only result after one call | `test_midnight_unplaced.py::test_real_september11_output_retains_midnight_todo_without_correction` |
| Other hard errors with midnight add | End-at-zero on a future day, existing moves to midnight and wrong clock evidence stay errors; only those errors go to correction | `test_midnight_unplaced.py::test_midnight_add_does_not_hide_other_hard_errors` |
| Captured dinner context omission | Raw `给吃晚饭` + null cannot silently become00:30; corrected full context starts20:00, retains all other tasks and midnight warnings | `test_shared_clock_context.py::test_recorded_cropped_dinner_context_cannot_become_a_valid_midnight_schedule`, `test_correction_restores_shared_dinner_clock_without_changing_other_operations` |
| Shared vs independent clock mentions | Commute end20:00 and dinner start08:00 citing the same original span are rejected; distinct8点 breakfast/dinner mentions can resolve differently | `test_shared_clock_context.py::test_shared_arrival_clock_has_one_resolution_across_commute_and_dinner`, `test_distinct_eight_oclock_mentions_can_still_resolve_to_different_periods` |
| Flexible activity after arrival | Independent tasks and 有空再/稍后/等一会儿/再 actions do not automatically inherit arrival time | `test_shared_clock_context.py::test_independent_or_explicitly_flexible_tasks_do_not_inherit_arrival_clock` |

Two old *bug* expectations are deliberately not retained: a 10:00 request with
an 11:00 model clock may no longer become a valid 08:00 free task; a negated
11:30–12:00 activity may no longer be silently added at 11:15. New model-entry
tests require rejection/correction instead. This does not change ordinary
untimed tasks, priorities, or scheduling warnings.

## Regression gate and evidence

Before production edits, two new API regressions using the historical slot-52
failure (with and without a true 08:50 quote) both failed because the old code
returned `valid=true`. The same tests now reject the invalid model output.
Additional tests cover all clock-contract error paths, merged source titles,
punctuation variants, cropped quotes, mixed move/add requests, duplicate clocks,
global and individual clocks in one sentence, relative references, and midnight.

Affected test mapping:

- `contracts.py`, `pipelines.py`, and the live parser → model/public contract
  tests; all migrated API fixtures; required/missing/null/old-field regressions.
- `time_fragment_clocks.py` → all clock extraction tests, with API-level final
  candidate and correction assertions rather than parser-only acceptance.
- `time_fragment.py` entry and normalization → planner/golden tests, including
  protected objects, identity, duration, priority, insertion, segmentation and
  earliest-start/capacity boundaries.
- Pipeline parameters → exact
  `test_api.py::test_litellm_client_forwards_pipeline_thinking_mode` selector.

The focused backend campaign selects these four affected test modules, the new
midnight regression module, and one exact
parameter-forwarding selector, not the full repository test suite. The few
auth/envelope-only API tests in the selected module run incidentally. The iOS
validator, golden consumer, and visible explanation are verified separately in
an isolated iOS worktree. The UI component must be approved before production
page integration; component screenshots are not real-model or persistence proof.

Fixed extraction fixtures prove the model-output-to-public-plan contract, not
the success rate or latency of a fresh external model generation. Live model
calls require a separate bounded approval; no production deployment is part of
this repair. In particular, 24:00 as an end boundary is allowed, but a positive
duration starting at 24:00 cannot be scheduled on the selected day. Only a new
add at that day-end slot is warning-only; existing moves and malformed clocks
keep their previous error behavior. Rounding to the day-end slot has the same
unscheduled result. This change does not alter default duration inference or
where otherwise untimed tasks are placed.

## Research-backed boundary

Checked 2026-09-08 against local Pydantic 2.13.5: JSON Schema distinguishes
[required fields from null values](https://json-schema.org/understanding-json-schema/reference/object).
Cross-field or contextual constraints require
[custom validation](https://pydantic.dev/docs/validation/latest/concepts/validators/).
These sources support the structural/semantic split; task ownership and clock
scope are application rules verified by the reproductions above, not claims
that a schema standard can understand the user's prose.
