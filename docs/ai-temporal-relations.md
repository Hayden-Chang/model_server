# Continuous activities and relative scheduling

This repair changes only the model-to-planner contract. A chronological narrative
could previously become two-slot work/rest tasks and midnight dinner/travel while
still returning valid=true. The model now emits source-backed temporalRelations
alongside operations; the planner enforces those relations before enabling Apply.

## Behavior

- `before` requires the predecessor to finish before the successor begins and
  preserves gaps. `until` shares a source-backed end/start boundary and derives
  the preceding continuous activity's duration when its own end was unstated.
- For example, `09:00 工作，11:30 午间休息，13:30 继续工作` becomes work
  09:00–11:30 and rest 11:30–13:30. Untimed activities after work retain their
  normal default duration but cannot move before their predecessor.
- Independent tasks and priority-only lists use an explicit empty relationship
  list and retain the previous allocator behavior. Explicit ends are never
  overwritten by a conflicting `until`. The model must preserve explicit
  durations and independent gaps rather than classify them as continuity.
- The required internal `temporalRelations` field distinguishes a reviewed
  empty list from an omitted relationship review. Missing/invalid output uses
  the existing single correction; it does not imply semantic completeness.
- Evidence is checked at exact affirmative source positions. A whole range can
  support each clock when that clock is uniquely identifiable. An untimed
  activity may quote a linked successor's clock only if that successor owns
  validated evidence; its own omitted clock still fails.
- Cycles, missing/duplicate references, contradictory boundaries and final
  sequence violations block Apply. Linked activities are admitted together
  when capacity is insufficient. A new task at 24:00 stays an unscheduled Todo
  with the existing warning; its boundary may still end a preceding activity.
- Correction can include the prior extraction to retain correct ranges and
  relationships, but only inside the existing input-size limit. Initial 30 s,
  correction 15 s and total 45 s budgets, model selection and solver budgets
  remain unchanged.
- App request/response fields, temporary IDs, protected-task authorization,
  persistence and UI actions are unchanged. No relations or source evidence
  leak into the public proposal.

## Incremental regression mapping

Exact parameterized selectors and results are retained with the final-head
campaign. The selected modules directly exercise these changed boundaries:

| Changed boundary | Selected tests in business_api/tests |
|---|---|
| New relationships, correction retention, source ownership and day-end integration | All new cases in test_temporal_relations.py |
| Clock compiler and its model-entry callers | test_time_fragment_clock_extraction.py, test_shared_clock_context.py, test_existing_clock_adjustments.py, test_explicit_duration_insertion.py, test_global_start_recovery.py, test_midnight_unplaced.py |
| Model envelope, public privacy and operation normalization | test_time_fragment_contracts.py; planning/correction consumers in test_time_fragment_api.py |
| Shared allocation and final candidate validation | test_time_fragment_planner.py and test_time_fragment_solver.py, including all 24 client/server golden fixtures |
| Service input/timeout budgets and model parameters | test_planning_timeout_budget.py; test_model_client.py::test_correction_forwards_low_effort_and_bounds_timeout; test_api.py::test_litellm_client_forwards_pipeline_thinking_mode (planning variants only) |

Authentication-only and admin-only cases are excluded from the final selection.
No full repository suite or iOS UI suite is selected.

The missing-review regression first reproduced unsafe acceptance on the unchanged
base; two new relation-contract cases also failed there. Two additional API
regressions failed with the old clock compiler alone:
valid range quotes and linked-successor source context were rejected. All use
synthetic narratives. Existing fixture operations, expected time segments and
golden files are unchanged; only fake model envelopes acquire the required
empty relationship list.

## Evidence boundary

A candidate-only call through the existing model gateway also checked the
authorized original narrative: all nine activities had the expected intervals
after one model call (approximately 6.8 s including the test transport).
The raw private request, extraction and trace remain outside the repository.
This is one live sample plus deterministic regressions, not a universal model
accuracy guarantee. It does not constitute a production deployment or a
post-fix Simulator end-to-end run.

## Research

Reviewed 2026-09-08: [DeepSeek JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)
recommends a complete JSON example;
[Structured Outputs handling mistakes](https://developers.openai.com/api/docs/guides/structured-outputs#handling-mistakes)
distinguishes schema conformance from content correctness; and
[LangExtract source grounding](https://github.com/google/langextract#why-langextract)
uses exact source locations for checking extractions. These informed the prompt
example and source-position validation; no new provider or library is introduced.
