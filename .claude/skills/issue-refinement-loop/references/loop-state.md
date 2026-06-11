---
topic: loop_state
file: references/loop-state.md
loaded_when: need to understand LOOP_STATE field semantics or routing decisions
owner: issue-refinement-loop orchestrator
moved_from: SKILL.md##LOOP_STATE Summary
must_not: re-implement routing logic — use decide_next_loop_action.py
schema: schemas/loop_state.schema.json
---

# LOOP_STATE Reference

Full field definitions and routing semantics for the `LOOP_STATE_V1` schema.
The canonical machine-readable schema is `schemas/loop_state.schema.json`.

## Field Index

| field | type | routing_critical | description |
|---|---|---|---|
| `schema_version` | string const | no | `"loop_state/v1"` |
| `issue_number` | int | no | Target issue number |
| `iteration` | int (0-indexed) | yes | Current iteration count |
| `max_iterations` | int (default 3) | yes | Upper bound; escalates to human at `iteration >= max_iterations` |
| `last_verdict` | `approve\|needs-fix\|null` | yes | Most recent review verdict |
| `blockers_history` | array | yes | All iteration blocker lists for escalation summary |
| `improvements_applied` | array of string | no | Rewrite notes per iteration |
| `removed_state_labels` | array of string | no | Labels removed for hygiene |
| `termination_reason` | enum\|null | yes | Why the loop ended |
| `scope_rollup_decision` | string\|null | yes | Output of scope rollup preflight |
| `anchor_comment` | object | yes | Snapshot + classification of anchor comment |
| `investigation_policy` | object | yes | Whether codebase investigation is required |
| `scope_signal_guard` | object | yes | Whether scope change signal was detected |
| `web_research_policy` | object | yes | Whether web research is required |
| `web_research` | object | no | Web research execution state |
| `product_spec_context` | object | yes | Product/Spec work kind signal |
| `delivery_rollup` | object | yes | Parent delivery rollup applicability |
| `follow_up_materialization` | object | yes | Follow-up issue candidates |
| `superseded_decision` | object | yes | If this issue was superseded by a human decision |

## Routing Semantics

### iteration / max_iterations

`iteration` is the current 0-indexed round number passed to `decide_next_loop_action.py`.
Continuation is possible as long as a next round exists: `iteration + 1 < max_iterations`.

| condition | next action |
|---|---|
| `last_verdict == approve` | `proceed_to_step_4_5` (child/follow-up materialization) |
| `last_verdict == needs-fix` AND `iteration + 1 < max_iterations` | `continue_to_step_4` (rewrite) |
| `last_verdict == needs-fix` AND `iteration + 1 >= max_iterations` | `human_escalation` |
| `termination_reason != null` | loop already terminated — no action |

### termination_reason values

| value | meaning |
|---|---|
| `approved` | Review issued `approve` verdict |
| `human_escalation` | `max_iterations` exceeded or hard stop signal |
| `superseded_by_decision` | Human anchor comment superseded the loop |
| `null` | Loop not yet terminated |

### scope_rollup_decision

Set at Step 0 (before any iteration). If non-null, the orchestrator records the rollup decision but does not stop — the planner may still proceed if rollup is advisory.

### scope_signal_guard

| field | meaning |
|---|---|
| `triggered` | A scope change signal was detected |
| `excluded_by_anchor_reframe` | The signal was excluded by an anchor comment reframe |
| `reason_code` | Detailed reason code from planner |

When `triggered == true` AND `excluded_by_anchor_reframe == false`, the loop stops with `human_escalation`. See `references/scope-signal-guard.md` for signal taxonomy.

### delivery_rollup

| field | meaning |
|---|---|
| `applicable` | This is a delivery-rollup parent issue |
| `unmaterialized_slots` | Child issue slots not yet created |

If `applicable == true` AND `unmaterialized_slots` is non-empty, the orchestrator performs child materialization in Step 4.5 before terminating.

### follow_up_materialization

`candidates` is a list of follow-up issue proposals. Dedupe uses `dedupe_key` (not title). Candidates are materialized in Step 4.5 after approval.

### superseded_decision

If a human anchor comment supersedes the loop (e.g., closes the issue as won't fix, or redirects to an alternative), `superseded_decision` captures the summary. The loop terminates with `termination_reason: superseded_by_decision`.

## Next Action Script

Use `decide_next_loop_action.py` to compute the next action from the current LOOP_STATE:

```bash
uv run python3 .claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py \
  --loop-state-file <path/to/loop_state.json> \
  --review-result-verdict <approve|needs-fix> \
  --max-iterations <N>
```

Exit codes:
- `0`: pass — `NEXT_ACTION` is actionable
- `1`: warn — `NEXT_ACTION` is actionable but has notes
- `2`: human_escalation — stop and report
- `3`: inconsistent_state — state file is corrupt or contradictory

Priority: `inconsistent_state (3)` > `human_escalation (2)` > `warn (1)` > `pass (0)`.
