import { describe, expect, it } from 'vitest'

import {
  buildAgentRunReportCommentBody,
  formatDigestMarker,
  formatOwnershipMarker,
  parseDigestMarker,
  parseOwnershipMarker,
} from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderPublicMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
import { createValidObservationSourceResult } from './report-test-fixtures'

function createReport() {
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
  }
}

describe('github comment ownership marker format', () => {
  it('GIVEN a stable tuple WHEN formatted THEN ownership and digest markers stay separate', () => {
    const ownership = formatOwnershipMarker({
      repo: 'squne121/loop-protocol',
      issueNumber: 937,
      prNumber: null,
      runId: 'run-937-001',
    })
    const digest = formatDigestMarker('a'.repeat(64))

    expect(ownership).toBe('<!-- agent_run_report:v1 repo=squne121/loop-protocol issue=937 pr=null run_id=run-937-001 -->')
    expect(digest).toBe('<!-- agent_run_report_digest:v1 sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->')
    expect(ownership).not.toContain('sha256=')
  })

  it('GIVEN a formatted body WHEN parsed THEN stable ownership and digest round-trip', () => {
    const payloadMarkdown = renderPublicMarkdown(createReport())
    const candidate = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      payloadMarkdown,
    })
    const [ownershipLine, digestLine] = candidate.body.split('\n')

    expect(parseOwnershipMarker(ownershipLine)).toEqual({
      repo: 'squne121/loop-protocol',
      issueNumber: 937,
      prNumber: null,
      runId: 'run-937-001',
    })
    expect(parseDigestMarker(digestLine)).toBe(candidate.digest)
  })
})
