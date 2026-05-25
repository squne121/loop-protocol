# REFINEMENT_LOOP_PLAN_V1 Output Guide

## Overview

The `plan_refinement_loop.py` script analyzes Issue body, comments, and known context to produce a deterministic JSON plan describing:

- Policy decisions for investigation (codebase fact-checking)
- Policy decisions for web research (external specification verification)
- Scope signal guarding (anchor reframe exclusions)
- Delivery rollup child materialization slots
- Follow-up issue candidates for out-of-scope work

## Input (REFINEMENT_LOOP_PLANNER_INPUT_V1)

```json
{
  "schema_version": "refinement_loop_planner_input/v1",
  "issue": {
    "number": 123,
    "title": "Issue title",
    "body": "Full markdown body",
    "labels": ["label1", "label2"]
  },
  "comments": null,
  "known_context": {
    "anchor_comment_url": "https://github.com/...",
    "parent_mode": "delivery-rollup",
    "closure_mode": "child-complete"
  }
}
```

## Output (REFINEMENT_LOOP_PLAN_V1)

```json
{
  "schema_version": "refinement_loop_plan/v1",
  "source": {
    "issue_number": 123,
    "issue_body_sha256": "...",
    "comments_sha256": null,
    "known_context_sha256": null,
    "generated_at": "2026-05-25T14:22:00Z"
  },
  "decisions": {
    "investigation_policy": {
      "required": true,
      "reason_code": "target_paths_present",
      "target_paths": [
        ".claude/skills/test/script.py",
        "src/components/Component.ts",
        "docs/dev/test.md"
      ],
      "repo_claims": ["Script command", "Path reference"],
      "evidence_spans": [
        {
          "source": "issue_body",
          "source_ref": null,
          "start_line": 1,
          "end_line": 50,
          "text_sha256": "..."
        }
      ],
      "confidence": "deterministic"
    },
    "web_research_policy": {
      "required": false,
      "reason_code": "no_critical_external_claim",
      "critical_external_claims": [],
      "evidence_spans": [],
      "confidence": "unknown"
    },
    "scope_signal_guard": {
      "triggered": false,
      "reason_code": "no_scope_signal",
      "excluded_by_anchor_reframe": false,
      "evidence_spans": []
    },
    "delivery_rollup": {
      "applicable": false,
      "unmaterialized_slots": [],
      "evidence_spans": []
    },
    "follow_up_materialization": {
      "candidates": []
    }
  },
  "fail_closed": {
    "required": false,
    "reason_codes": [],
    "human_message": ""
  }
}
```

## Decision Fields Explained

### investigation_policy

- `required`: True if codebase fact-checking is needed
- `reason_code`: Why investigation is needed (or `no_repo_fact_claim` if not)
- `target_paths`: Extracted file/directory paths from Outcome/InScope/AC/VC
- `repo_claims`: Text spans claiming repo facts
- `evidence_spans`: Pointers to source evidence in issue body/comments
- `confidence`: `deterministic` if decision is clear, `unknown` otherwise

**Use in SKILL.md**: Set `LOOP_STATE.investigation_policy` from this decision and trigger Step 1 (codebase-investigator) if `required == true`.

### web_research_policy

- `required`: True if external specification verification is needed
- `reason_code`: Why research is needed (keywords like "official", "API", "auth", "migration")
- `critical_external_claims`: Extracted claims about external systems
- `evidence_spans`: Source evidence
- `confidence`: Deterministic or unknown

**Use in SKILL.md**: Set `LOOP_STATE.web_research_policy` from this decision and trigger Step 1b (web-researcher) if `required == true`.

### scope_signal_guard

- `triggered`: True if new scope signals detected
- `excluded_by_anchor_reframe`: True if anchor comment reframe excludes this signal
- `reason_code`: Type of signal (or `anchor_reframe_exclusion` if excluded)

**Use in SKILL.md**: If triggered and NOT excluded, consider human escalation for scope confirmation.

### delivery_rollup

- `applicable`: True if parent issue has unmaterialized child slots
- `unmaterialized_slots`: List of `{child_title_hint, marker, body_line}`
- Marker types: `未起票` (Japanese), `unmaterialized`, `TBD`

**Use in SKILL.md**: Use for tracking child issue materialization status.

### follow_up_materialization

- `candidates`: List of out-of-scope work that could become follow-up issues
- Each candidate has `{dedupe_key, summary, source_evidence}`
- `dedupe_key` is first 16 chars of sha256(summary) for deduplication

**Use in SKILL.md**: Reference candidates in post-approval comments or documentation.

## fail_closed Handling

When `fail_closed.required == true`:

1. The planner detected a structural issue (malformed contract, missing Outcome section, unknown schema)
2. Output is still valid JSON, but should not be used for decisions
3. Human escalation is required with `fail_closed.reason_codes` and `human_message`
4. The orchestrator should NOT attempt to infer missing policy

**reason_codes**:
- `malformed_machine_readable_contract`: YAML block missing `contract_schema_version`
- `missing_required_section`: Outcome or other critical section missing
- `unknown_input_schema`: Input didn't match `REFINEMENT_LOOP_PLANNER_INPUT_V1`
- `planner_internal_error`: Unexpected exception during processing

## Idempotency Guarantee

Same input (same issue body + comments + known_context) will always produce identical JSON output (except for `generated_at` timestamp).

The planner sorts all multi-value fields (`target_paths`, `repo_claims`, etc.) consistently to ensure reproducible output across runs.
