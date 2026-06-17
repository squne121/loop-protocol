import { describe, expect, it } from 'vitest'

import { buildAgentRunReportCommentBody, validateFinalCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown, validateFinalReport } from '../../scripts/agent-logs/lib/validate-final-report.mjs'

function createReport(overrides = {}) {
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
        summary: 'focused tests passed',
        artifact_ref: 'artifact:agent-logs-tests',
      },
    ],
    docs_read_refs: [
      {
        ref_kind: 'issue',
        ref: 'https://github.com/squne121/loop-protocol/issues/937',
        summary: 'implementation contract reviewed',
      },
    ],
    ...overrides,
  }
}

describe('github comment post guard', () => {
  it('GIVEN a valid public report WHEN rendered for posting THEN validator round-trip and final body guard both pass', () => {
    const report = createReport()
    validateFinalReport(report)
    const payloadMarkdown = renderValidatedPublicMarkdown(report)
    const candidate = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      payloadMarkdown,
    })
    const validation = validateFinalCommentBody(candidate.body, {
      expectedOwnership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      expectedDigest: candidate.digest,
    })

    expect(validation.valid).toBe(true)
  })

  it('GIVEN a report that fails the validator WHEN rendered for posting THEN post guard rejects before any comment body exists', () => {
    const report = createReport({
      public_safety: {
        redaction_status: 'dirty',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-17T12:30:00.000Z',
        verdict: 'pass',
        blocked_reasons: [],
      },
    })

    expect(() => renderValidatedPublicMarkdown(report)).toThrow(/public_surface_redaction_status/)
  })
})
