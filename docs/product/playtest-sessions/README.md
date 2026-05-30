# Playtest Sessions

This directory stores individual playtest session records.

## Schema

Session records follow the schema defined in `../playtest-log.md`.
See that file for the full YAML schema, field definitions, and policies.

## Session Modes

| mode | description |
|---|---|
| `human_internal` | Developer self-test, direct gameplay observation |
| `browser_automation` | Playwright E2E automated test run |
| `ai_simulation` | Agent-driven simulation (not yet implemented) |

## Naming Convention

```text
PT-YYYYMMDD-NNN-<short-slug>.md
```

Example: `PT-20260529-001-movement-projectile-smoke.md`

## Important Policies

- **Automatic E2E results are NOT UX validity evidence.**
  `browser_automation` sessions confirm integration correctness only.
  Human playtesting is required to evaluate feel, responsiveness, and UX.
  (Ref: `playtest-log.md` policy `ai_result_is_ux_evidence: false`)
- Do NOT commit raw recordings, video, or PII (`raw_recording_committed: false`).
- `session_mode: browser_automation` sessions with `classification: design hypothesis invalidated`
  or `decision: spec_delta_issue` MUST set `automation.human_review_required: true`.

## Running E2E Playwright Tests

```bash
# Install Chromium (first time)
pnpm playwright:install

# Run headless (CI mode)
pnpm test:e2e

# Run with headed browser (visual confirmation)
pnpm test:e2e:headed

# Interactive UI mode (debug / step through)
pnpm test:e2e:ui

# Debug mode (pause on test)
pnpm test:e2e:debug
```

Failure artifacts (trace + report) are saved to:
- `test-results/` — trace files (retain-on-failure)
- `playwright-report/` — HTML report

## Sample Session Files

- `PT-20260529-001-movement-projectile-smoke.md` — minimal browser_automation sample
