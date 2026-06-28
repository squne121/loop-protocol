---
id: agent-observation-capability
status: stable
related_issue: "#1221"
parent_issue: "#1153"
created: "2026-06-28"
---

# Agent Observation Capability Matrix (SSOT)

This document is the SSOT for the `agent_observation_capability/v1` capture-capability
verdict matrix across Claude Code, Codex CLI, and Google Antigravity. Verdicts are fixed
by SYNTHETIC evidence only (synthetic fixtures / read-only host inventory). No real
telemetry, no real Latitude pilot, and no real trace export is run to produce this matrix.

`unsupported` and `unverified` are NOT failures. They are input-availability signals for
Child C0 (observation source / provenance / safety schema admission) and Child C1 (adapter).

The machine check that enforces this contract is
`.claude/scripts/check_session_recording_runtime_safety.py --capability-fixture <path>`,
asserted through `pnpm test` in `tests/session_recording_runtime_safety.test.ts`.

## Contract

```yaml
agent_observation_capability/v1:
  schema: agent_observation_capability/v1
  evidence_mode: synthetic_only
  real_runtime_evidence: blocked_until_pilot_exception_approve_timeboxed_real_pilot
  verdict_enum: [supported, partial, unsupported, unverified]
  supported_predicate:
    runtime_event_observed: true
    capture_artifact_observed: true
    raw_values_emitted: false
  non_failure_verdicts: [unsupported, unverified]
  non_failure_meaning: child_c0_c1_input_availability
```

`supported` holds ONLY when `runtime_event_observed == true` AND
`capture_artifact_observed == true` AND `raw_values_emitted == false`. Under
`evidence_mode: synthetic_only` the only trusted provenance is `synthetic_fixture`;
`real_pilot_verified` provenance stays blocked until the #1220
`LATITUDE_PILOT_EXCEPTION_V1` gate becomes `approve_timeboxed_real_pilot` with all
activation fields machine-verified. The #1220 A1 decision gate default
(`approve_synthetic_only` / `pilot_activation_state: blocked_until_activation`) is
unchanged by this document, and `docs/dev/secret-policy.md` is unchanged.

## Hook coexistence pass contract

A surface that relies on hook coexistence (Latitude async Stop hook plus existing
coordinator hooks) reaches `supported` only when this closed contract holds. The async
hook is NOT a gate; the post-run verifier is the canonical gate; hook exit 0 is not
authoritative.

```yaml
hook_coexistence_pass_requires:
  expected_handlers_fired_once: true
  duplicate_finalization_absent: true
  duplicate_upload_absent: true
  async_hook_not_used_as_gate: true
  post_run_verifier_observed_final_state: true
  runtime_event_and_capture_artifact_correlated: true
  hook_exit_zero_not_authoritative: true
  raw_values_emitted: false
```

## Public-safety admission contract

Every evidence artifact projected into this matrix satisfies:

```yaml
public_safety:
  raw_values_emitted: false
  forbidden_field_scan: pass
  prompt_excerpt_present: false
  tool_io_excerpt_present: false
  local_absolute_path_present: false
  credential_value_present: false
  digest_is_over_public_projection_only: true
```

## Surfaces

Exactly three surfaces, each carrying exactly ONE verdict from the closed enum.

### Claude Code

```yaml
surface: claude_code
verdict: unverified
checked_surfaces:
  - user
  - project
  - local
  - managed_policy
  - plugin
  - skill
  - agent_frontmatter
gate_model:
  async_hook_is_gate: false
  canonical_gate: post_run_verifier
  pass_requires: hook_coexistence_pass_requires
notes: >
  The async Stop hook is a diagnostic / prevention layer, not a gate. supported is admitted
  only when a runtime event and a capture artifact are correlated, the hook_coexistence_pass_requires
  contract holds, and no raw values are emitted. Default synthetic verdict is unverified until a
  synthetic fixture demonstrates the supported predicate.
```

### Codex CLI

```yaml
surface: codex_cli
verdict: unsupported
canonical_feature_key: "[features].hooks"
legacy_alias: codex_hooks
supported_blocked_while:
  - codex_hooks_json_validator_drift
  - non_canonical_hook_key
  - project_layer_untrusted
notes: >
  Codex MUST NOT be supported while .codex/hooks.json and
  scripts/session-recording/validate-codex-hooks.mjs drift. The canonical feature key is
  [features].hooks; codex_hooks is a legacy alias only. Project .codex layer must be trusted.
```

### Google Antigravity

```yaml
surface: google_antigravity
verdict: unverified
non_capture_signals:
  - mcp_connection
  - ide_launch
  - artifacts_generation
  - browser_recording
supported_requires:
  capture_artifact_observed: true
  runtime_event_observed: true
notes: >
  MCP connection, IDE launch, Artifacts generation, and browser recording do NOT count as
  capture evidence. Antigravity stays unverified unless BOTH a capture artifact AND a runtime
  event are observed and correlated.
```

## Negative controls

The following synthetic negative-control fixtures must NOT promote an unsafe state to
`supported` (the checker emits `decision: deny` or `fail_closed`):

- claude duplicate Stop (user + project) -> duplicate finalization / upload
- claude async Latitude Stop finishing after the finalizer -> async hook used as gate
- claude hook exit 0 without a trace artifact -> hook exit 0 not authoritative
- codex current hooks validator drift
- codex legacy `codex_hooks` only (non-canonical key)
- codex untrusted project layer
- antigravity MCP connected but no capture artifact
- supported claimed with runtime event missing
- supported claimed with capture artifact missing
- evidence with raw values emitted
- latitude floating npx package (unpinned provenance)
- latitude provenance unknown

## Related documents

- `docs/dev/session-recording-policy.md` — session recording Kill Switch policy and hook boundary
- `docs/dev/agent-run-report.md` — agent_run_report/v1 and Hook Boundary Policy
- `docs/dev/secret-policy.md` — Secret Inventory (unchanged by this matrix; projection only)
- `.claude/scripts/check_session_recording_runtime_safety.py` — runtime safety + capability checker
- Issue #1153 — parent pilot tracker
- Issue #1220 — LATITUDE_PILOT_EXCEPTION_V1 decision gate
