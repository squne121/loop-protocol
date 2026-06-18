import { mkdtempSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

import { createValidReport } from '../agent-run-report-test-helpers'
import { updateRetroIndex, verifyRetroIndexArtifact } from '../../scripts/agent-logs/update-retro-index.mjs'
import { buildAgentRunReportCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown } from '../../scripts/agent-logs/lib/validate-final-report.mjs'

function createSourceComment() {
  const report = createValidReport()
  report.docs_read_refs = [
    {
      ref_kind: 'issue',
      ref: 'https://github.com/squne121/loop-protocol/issues/935',
      summary: 'Linked PR #955 validated',
    },
    {
      ref_kind: 'pull_request',
      ref: 'https://github.com/squne121/loop-protocol/pull/955',
      summary: 'Closes #935',
    },
  ]
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122667',
    body: buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-001',
      },
      payloadMarkdown: renderValidatedPublicMarkdown(report),
    }).body,
    linkedPrHints: [955],
    linkedIssueHints: [935],
    branchHint: 'worktree-issue-935-agent-run-report',
  }
}

describe('update-retro-index', () => {
  it('GIVEN dry-run mode WHEN updateRetroIndex builds and upserts THEN it returns summary-only output and create action', async () => {
    const client = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }

    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createSourceComment()],
        prMetadataByNumber: new Map([
          [955, {
            number: 955,
            body: 'Closes #935',
            mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            headRefName: 'worktree-issue-935-agent-run-report',
          }],
        ]),
        associatedPrByMergeSha: new Map([
          ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
        ]),
      },
    })

    expect(result.status).toBe('ok')
    expect(result.action).toBe('create')
    expect(result.summary).toMatchObject({
      generation_verdict: 'complete',
      entry_count: 1,
      orphan_count: 0,
      ambiguous_count: 0,
    })
    expect(JSON.stringify(result.summary)).not.toContain('agent_retro_index/v1')
  })

  it('GIVEN live mode without confirm-live WHEN updateRetroIndex runs THEN it fails closed before comment scanning', async () => {
    const client = {
      listIssueComments: async () => {
        throw new Error('list should not run')
      },
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    await expect(updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: false,
      confirmLive: false,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [],
        sourceComments: [],
        prMetadataByNumber: new Map(),
        associatedPrByMergeSha: new Map(),
      },
    })).rejects.toThrow(/live posting requires --dry-run false and --confirm-live true/)
  })

  it('GIVEN a built artifact and summary WHEN verifyRetroIndexArtifact runs THEN canonical digest is revalidated without expanding schema keys', async () => {
    const client = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }
    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createSourceComment()],
        prMetadataByNumber: new Map([
          [955, {
            number: 955,
            body: 'Closes #935',
            mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            headRefName: 'worktree-issue-935-agent-run-report',
          }],
        ]),
        associatedPrByMergeSha: new Map([
          ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
        ]),
      },
    })
    const tempDir = mkdtempSync(join(tmpdir(), 'retro-index-'))
    const artifactPath = join(tempDir, 'agent-retro-index.json')
    const summaryPath = join(tempDir, 'agent-retro-index-summary.json')
    writeFileSync(artifactPath, `${JSON.stringify(result.index, null, 2)}\n`)
    writeFileSync(summaryPath, `${JSON.stringify(result.summary, null, 2)}\n`)

    expect(verifyRetroIndexArtifact({
      artifactJsonPath: artifactPath,
      summaryJsonPath: summaryPath,
    })).toMatchObject({
      status: 'ok',
      canonical_index_digest: result.summary.canonical_index_digest,
      source_comment_set_digest: result.summary.source_comment_set_digest,
      entry_count: 1,
    })
  })
})
