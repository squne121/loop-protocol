import { describe, expect, it } from 'vitest'

import { buildAgentRunReportCommentBody, validateFinalCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown } from '../../scripts/agent-logs/lib/validate-final-report.mjs'

function createReport(summary) {
  return {
    schema: 'agent_run_report/v1',
    public_surface_kind: 'github_issue_comment',
    public_safety: {
      redaction_status: 'clean',
      checked_by: 'pnpm agent-run-report:check',
      validator_version: '1.0.0',
      checked_at: '2026-06-17T12:30:00.000Z',
      verdict: 'pass',
      blocked_reasons: [],
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
      name: 'Codex worker',
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
    manifest_refs: [],
    evidence_refs: [],
    commands_summary: [
      {
        command_label: 'pnpm test -- tests/agent-logs',
        exit_code: 0,
        verdict: 'pass',
        summary,
        artifact_ref: 'artifact:agent-logs-tests',
      },
    ],
    docs_read_refs: [],
  }
}

describe('github comment body size gate', () => {
  it('GIVEN an oversized payload markdown WHEN final body is validated THEN UTF-8 byte gating fails closed', () => {
    const payloadMarkdown = renderValidatedPublicMarkdown(createReport('focused tests passed'))
    const candidate = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      payloadMarkdown,
    })
    const validation = validateFinalCommentBody(candidate.body, { maxBytes: 64 })

    expect(validation.valid).toBe(false)
    expect(validation.errors.some((error) => error.code === 'github_comments.body_too_large')).toBe(true)
    expect(validation.byteLength).toBeGreaterThan(64)
  })
})
