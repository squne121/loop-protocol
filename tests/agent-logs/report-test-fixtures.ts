import { computeObservationSourceProjectionDigest } from '../../scripts/agent-logs/lib/observation-source-adapter.mjs'

export function createValidObservationSourceResult() {
  const projection = {
    schema_version: 'observation_source_result/v1',
    source_kind: 'codex_cli',
    capability_verdict: 'supported',
    availability: 'available',
    projection_mode: 'allowlist_projection',
    safety: {
      verdict: 'pass',
      raw_values_emitted: false,
      forbidden_field_scan: 'pass',
      reason_codes: [],
    },
    metrics: {
      trace_count: 1,
      span_count: 2,
      prompt_tokens: 10,
      completion_tokens: 20,
      total_tokens: 30,
    },
  }
  const digest = computeObservationSourceProjectionDigest(projection)
  return {
    ...projection,
    provenance: {
      schema_version: 'observation_source_provenance/v1',
      ref: {
        kind: 'observation_projection_digest',
        artifact_id: null,
        artifact_digest: null,
        workflow_run_url: null,
        schema_ref: null,
        ref: null,
        digest,
        validation_verdict: 'pass',
      },
      source_projection_digest: digest,
      validator_id: 'observation-source-adapter',
      validator_policy_digest: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
      evidence_mode: 'synthetic_only',
      checked_at: '2026-06-15T22:57:00Z',
    },
  }
}

export function createValidReport() {
  return {
    schema: 'agent_run_report/v1',
    public_surface_kind: 'github_issue_comment',
    public_safety: {
      redaction_status: 'clean',
      checked_by: 'pnpm agent-run-report:check',
      validator_version: '1.0.0',
      checked_at: '2026-06-15T22:57:00Z',
      verdict: 'pass',
      blocked_reasons: [],
      observation_sources: [createValidObservationSourceResult()],
      entirecli_safety: {
        schema_version: 'entirecli_safety_result/v1',
        verdict: 'not_applicable',
        reason_codes: ['entire_absent'],
        raw_values_emitted: false,
        checked_surfaces: {
          entire_binary: false,
          entire_version: null,
          entire_enable_help: false,
          entire_configure_help: false,
        },
      },
    },
    actor: {
      type: 'ai_agent',
      name: 'Codex',
    },
    authority: {
      level: 'non_authoritative',
      basis: 'ai_self_report',
      evidence_refs: [],
    },
    token_usage: {
      availability: 'unavailable',
      source: 'none',
      prompt: null,
      completion: null,
      total: null,
    },
    manifest_refs: [
      {
        kind: 'github_actions_artifact',
        artifact_id: '123456',
        artifact_digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        workflow_run_url: 'https://github.com/squne121/loop-protocol/actions/runs/123456',
        schema_ref: 'docs/schemas/agent-session-manifest.schema.json',
        ref: null,
        digest: null,
        validation_verdict: 'pass',
      },
    ],
    evidence_refs: [
      {
        kind: 'workflow_run',
        artifact_id: null,
        artifact_digest: null,
        workflow_run_url: null,
        schema_ref: null,
        ref: 'https://github.com/squne121/loop-protocol/actions/runs/123456',
        digest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        validation_verdict: 'pass',
      },
    ],
    commands_summary: [
      {
        command_label: 'pnpm test',
        exit_code: 0,
        verdict: 'pass',
        summary: 'schema validation suite passed with redacted output only',
        artifact_ref: 'artifact:123456',
      },
    ],
    docs_read_refs: [
      {
        ref_kind: 'doc_path',
        ref: 'docs/dev/workflow.md',
        summary: 'workflow guardrails reviewed',
      },
    ],
  }
}
