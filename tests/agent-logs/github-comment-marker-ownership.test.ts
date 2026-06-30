import { describe, expect, it } from 'vitest'

import { buildAgentRunReportCommentBody, validateFinalCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import { createValidObservationSourceResult } from '../agent-run-report-test-helpers'

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
    docs_read_refs: [
      {
        ref_kind: 'issue',
        ref: 'https://github.com/squne121/loop-protocol/issues/937',
        summary: 'implementation contract reviewed',
      },
    ],
  }
}

describe('github comment ownership marker enforcement', () => {
  it('GIVEN marker text in a user-provided summary WHEN posting body is assembled THEN ownership marker count fails closed', () => {
    const payloadMarkdown = renderValidatedPublicMarkdown(
      createReport('summary <!-- agent_run_report:v1 repo=squne121/loop-protocol issue=937 pr=null run_id=run-937-001 -->')
    )
    expect(() => buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      payloadMarkdown,
    })).toThrow(/ownership marker must appear exactly once/)
  })

  it('GIVEN a tampered first marker WHEN final body is validated THEN ownership mismatch fails closed', () => {
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
    const tampered = candidate.body.replace('issue=937', 'issue=938')
    const validation = validateFinalCommentBody(tampered, {
      expectedOwnership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      expectedDigest: candidate.digest,
    })

    expect(validation.valid).toBe(false)
    expect(validation.errors.some((error) => error.code === 'github_comments.ownership_mismatch')).toBe(true)
  })

  it('GIVEN a malformed ownership-like marker inside the payload WHEN final body is validated THEN the broad injection scan fails closed', () => {
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
    const tampered = `${candidate.body}\n<!-- agent_run_report:v1 issue=937 run_id=run-937-001 -->`
    const validation = validateFinalCommentBody(tampered, {
      expectedOwnership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      expectedDigest: candidate.digest,
    })

    expect(validation.valid).toBe(false)
    expect(validation.errors.some((error) => error.code === 'github_comments.ownership_marker_count')).toBe(true)
  })
})
