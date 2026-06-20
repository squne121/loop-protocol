# Agent Run Report

This document describes the `agent_run_report/v1` schema and the `export-chatgpt-context` export surface.

## agent_run_report/v1

Agent run reports are JSON artifacts produced by `scripts/agent-logs/finalize-agent-run.mjs`.
They capture the public-safe summary of a single AI agent run, including:

- `schema`: always `"agent_run_report/v1"`
- `public_surface_kind`: where the report may be surfaced (`none`, `github_issue_comment`, `github_pr_comment`)
- `public_safety`: `{ redaction_status, checked_by, validator_version, checked_at, verdict, blocked_reasons }`
- `actor`: `{ type, name }`
- `authority`: `{ level, basis, evidence_refs }`
- `token_usage`: `{ availability, source, prompt, completion, total }`
- `manifest_refs`: list of manifest digest refs
- `evidence_refs`: list of evidence refs (workflow runs, PR/Issue URLs, artifact digests)
- `commands_summary`: list of command summaries (`command_label`, `exit_code`, `verdict`, `summary`, `artifact_ref`)
- `docs_read_refs`: list of doc read refs

### Forbidden Fields

The following fields **must not** appear in any source JSON file consumed by the export pipeline:

| Field | Reason |
|---|---|
| `raw_transcript` | Full session transcript — never public-safe |
| `transcript_excerpt` | Partial transcript — never public-safe |
| `full_command_output` | Unredacted command stdout/stderr |
| `stdout` | Unredacted stdout |
| `stderr` | Unredacted stderr |
| `local_path` | Local filesystem path — environment-specific and potentially sensitive |

### transcript_hotspot_summary

`transcript_hotspot_summary` is the **only** transcript-derived field permitted in export sources.
It is allowed **only when** `public_safety.redaction_status === "clean"` has been verified.

## ChatGPT Context Bundle Export

The `export-chatgpt-context` CLI generates a public-safe, deterministic Markdown bundle
suitable for pasting into ChatGPT for retrospective analysis.

### Script

```
scripts/agent-logs/export-chatgpt-context.mjs
```

### CLI Usage

```bash
node scripts/agent-logs/export-chatgpt-context.mjs \
  --parent-issue-json artifacts/parent-issue-928.json \
  --target-issue-json artifacts/issue-939.json \
  --retro-index-json artifacts/agent-retro-index.json \
  --source-set-json artifacts/agent-retro-index-source-set.json \
  --run-report-json artifacts/report-1.json \
  --evidence-ref-json artifacts/evidence-refs.json \
  --max-chars 24000 \
  --max-sections 12 \
  --generated-at 2026-06-19T00:00:00.000Z \
  --output artifacts/chatgpt-context.md \
  --summary-json-out artifacts/chatgpt-context-summary.json
```

### Options

| Option | Description |
|---|---|
| `--parent-issue-json` | Path to parent issue JSON (required) |
| `--target-issue-json` | Path to target issue JSON (required) |
| `--retro-index-json` | Path to agent retro index JSON (required) |
| `--source-set-json` | Path to source set JSON (required) |
| `--run-report-json` | Path to run report JSON (repeatable) |
| `--evidence-ref-json` | Path to evidence ref JSON (repeatable) |
| `--max-chars` | Character budget for the bundle (required) |
| `--max-sections` | Maximum number of sections (required) |
| `--generated-at` | ISO-8601 timestamp for bundle header (required) |
| `--output` | Output Markdown path (required, no-overwrite) |
| `--summary-json-out` | Output summary JSON path (required, no-overwrite) |

### Section Priority Order (fixed)

1. `safety_header` — SECURITY_BOUNDARY + chatgpt_context_bundle/v1 header
2. `source_manifest` — source file digests
3. `parent_goal` — parent issue + target issue
4. `priority_signals` — friction, context pollution, human intervention, follow-ups
5. `ci_review_loops` — CI/review loop data
6. `evidence_refs` — deduplicated evidence refs
7. `lower_priority_narrative` — run report summaries
8. `omission_report` — sections dropped due to budget

Lower-priority sections are dropped first when the budget is exceeded.
If the budget is too small to hold `safety_header` + `priority_signals`, the CLI exits with `budget.too_small`.

### Security Properties

- All external-origin text is fenced in `DATA` blocks or blockquotes.
- A final rendered Markdown scan rejects injection patterns.
- Output is written atomically (no partial writes, no overwrite).
- Source files are scanned for forbidden fields before processing.
- Each source file's digest is pinned in the `source_manifest`.

### Library Modules

| Module | Responsibility |
|---|---|
| `lib/chatgpt-context-args.mjs` | CLI argument parsing and validation |
| `lib/chatgpt-context-source-loader.mjs` | Load, validate, and digest source files |
| `lib/chatgpt-context-safety-scan.mjs` | Injection scanner and DATA block wrapping |
| `lib/chatgpt-context-dedupe.mjs` | Evidence ref canonicalization and deduplication |
| `lib/chatgpt-context-budget.mjs` | Priority-aware budget allocation |
| `lib/chatgpt-context-renderer.mjs` | Markdown section renderers |
