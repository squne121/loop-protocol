import { describe, expect, it } from 'vitest'

import { postAgentRunReport } from '../../scripts/agent-logs/post-agent-run-report.mjs'
import { buildAgentRunReportCommentBody, sha256Hex } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import { createValidObservationSourceResult } from './report-test-fixtures'

function createDraft() {
  return {
    schema: 'agent_run_draft/v1',
    run_id: 'run-937-001',
    target: {
      kind: 'issue',
      id: 937,
    },
    phase: 'implementation',
    actor: {
      type: 'ai_agent',
      name: 'Codex worker',
    },
    started_at: '2026-06-17T12:00:00.000Z',
  }
}

function createPullRequestDraft() {
  return {
    schema: 'agent_run_draft/v1',
    run_id: 'run-937-pr-001',
    target: {
      kind: 'pull_request',
      id: 977,
    },
    phase: 'implementation',
    actor: {
      type: 'ai_agent',
      name: 'Codex worker',
    },
    started_at: '2026-06-17T12:00:00.000Z',
  }
}

function createReport(summary = 'focused tests passed') {
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
    docs_read_refs: [],
  }
}

describe('github comment upsert flow', () => {
  it('GIVEN a paginated existing comment with the same digest WHEN posting THEN action becomes noop', async () => {
    const report = createReport()
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
    let page = 0
    const client = {
      listIssueComments: async () => {
        page += 1
        if (page === 1) {
          return Array.from({ length: 100 }, (_, index) => ({ id: index + 1, body: 'plain comment body' }))
        }
        return [{ id: 999, html_url: 'https://github.com/squne121/loop-protocol/issues/937#issuecomment-999', body: candidate.body }]
      },
      createIssueComment: async () => {
        throw new Error('create should not run for noop')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run for noop')
      },
    }

    const result = await postAgentRunReport({
      draft: createDraft(),
      report,
      repo: 'squne121/loop-protocol',
      dryRun: false,
      confirmLive: true,
      client,
    })

    expect(result.action).toBe('noop')
    expect(result.sha256).toBe(sha256Hex(payloadMarkdown))
    expect(page).toBe(2)
  })

  it('GIVEN one existing marker comment with a changed digest WHEN posting THEN action becomes update', async () => {
    const originalReport = createReport('old summary')
    const originalCandidate = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      payloadMarkdown: renderValidatedPublicMarkdown(originalReport),
    })
    const client = {
      listIssueComments: async () => [{ id: 10, html_url: 'https://github.com/squne121/loop-protocol/issues/937#issuecomment-10', body: originalCandidate.body }],
      createIssueComment: async () => {
        throw new Error('create should not run for update')
      },
      updateIssueComment: async ({ commentId }) => ({ id: commentId, html_url: 'https://github.com/squne121/loop-protocol/issues/937#issuecomment-10' }),
    }

    const result = await postAgentRunReport({
      draft: createDraft(),
      report: createReport('new summary'),
      repo: 'squne121/loop-protocol',
      dryRun: false,
      confirmLive: true,
      client,
    })

    expect(result.action).toBe('update')
    expect(result.comment_id).toBe(10)
  })

  it('GIVEN an issue-target draft with mismatched CLI overrides WHEN posting THEN it fails closed before scanning comments', async () => {
    const client = {
      listIssueComments: async () => {
        throw new Error('list should not run for mismatched overrides')
      },
      createIssueComment: async () => {
        throw new Error('create should not run for mismatched overrides')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run for mismatched overrides')
      },
    }

    await expect(postAgentRunReport({
      draft: createDraft(),
      report: createReport(),
      repo: 'squne121/loop-protocol',
      issueNumber: 999,
      client,
    })).rejects.toThrow(/override must match draft.target.id/)

    await expect(postAgentRunReport({
      draft: createDraft(),
      report: createReport(),
      repo: 'squne121/loop-protocol',
      prNumber: 977,
      client,
    })).rejects.toThrow(/does not allow --pr-number/)
  })

  it('GIVEN a pull-request draft WHEN posting with matching overrides THEN marker tuple and endpoint number both bind to the PR target', async () => {
    const client = {
      listIssueComments: async ({ issueNumber }) => {
        expect(issueNumber).toBe(977)
        return []
      },
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }

    const result = await postAgentRunReport({
      draft: createPullRequestDraft(),
      report: {
        ...createReport(),
        public_surface_kind: 'github_pr_comment',
      },
      repo: 'squne121/loop-protocol',
      issueNumber: 977,
      prNumber: 977,
      dryRun: true,
      client,
    })

    expect(result).toMatchObject({
      action: 'create',
      issue_number: 977,
      pr_number: 977,
    })
  })
})
