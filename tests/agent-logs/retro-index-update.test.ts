import { mkdtempSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

import { createValidReport } from './report-test-fixtures'
import { extractPayloadFromMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
import { parseChecklistIssueNumbers, updateRetroIndex, verifyRetroIndexArtifact } from '../../scripts/agent-logs/update-retro-index.mjs'
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

function createBlockedSourceComment() {
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122669',
    body: '<!-- agent_run_report:v1 repo=squne121/loop-protocol issue=935 pr=955 run_id=run-935-blocked -->',
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
    writeFileSync(join(tempDir, 'agent-retro-index-source-set.json'), `${JSON.stringify(result.sourceCommentRefs, null, 2)}\n`)
    writeFileSync(summaryPath, `${JSON.stringify(result.summary, null, 2)}\n`)

    expect(verifyRetroIndexArtifact({
      artifactJsonPath: artifactPath,
      sourceSetJsonPath: join(tempDir, 'agent-retro-index-source-set.json'),
      summaryJsonPath: summaryPath,
    })).toMatchObject({
      status: 'ok',
      canonical_index_digest: result.summary.canonical_index_digest,
      source_comment_set_digest: result.summary.source_comment_set_digest,
      entry_count: 1,
    })
  })

  it('GIVEN a verified artifact bundle WHEN live update runs THEN the posted canonical payload stays byte-equivalent to the built artifact', async () => {
    const dryRunClient = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }
    const built = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: dryRunClient,
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
    const createdBodies = []
    const liveClient = {
      listIssueComments: async () => [],
      createIssueComment: async ({ body }) => {
        createdBodies.push(body)
        return {
          id: 101,
          html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000000',
        }
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: false,
      confirmLive: true,
      issueCommentClient: liveClient,
      artifactBundle: {
        index: built.index,
        sourceCommentRefs: built.sourceCommentRefs,
        canonicalIndexDigest: built.canonical_index_digest,
        sourceCommentSetDigest: built.source_comment_set_digest,
        summary: built.summary,
      },
    })

    expect(result.status).toBe('ok')
    expect(createdBodies).toHaveLength(1)
    const extraction = extractPayloadFromMarkdown(createdBodies[0], 'agent_retro_index/v1')
    expect(extraction.ok).toBe(true)
    expect(extraction.payload).toEqual(built.index)
  })

  it('GIVEN a tampered summary digest WHEN verifyRetroIndexArtifact runs THEN the source-set artifact catches the mismatch', async () => {
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
    const sourceSetPath = join(tempDir, 'agent-retro-index-source-set.json')
    const summaryPath = join(tempDir, 'agent-retro-index-summary.json')
    writeFileSync(artifactPath, `${JSON.stringify(result.index, null, 2)}\n`)
    writeFileSync(sourceSetPath, `${JSON.stringify(result.sourceCommentRefs, null, 2)}\n`)
    writeFileSync(summaryPath, `${JSON.stringify({
      ...result.summary,
      source_comment_set_digest: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    }, null, 2)}\n`)

    expect(() => verifyRetroIndexArtifact({
      artifactJsonPath: artifactPath,
      sourceSetJsonPath: sourceSetPath,
      summaryJsonPath: summaryPath,
    })).toThrow(/source-set artifact digest does not match the expected source-comment-set digest/)
  })

  it('GIVEN a blocked generation verdict WHEN updateRetroIndex runs THEN it fails closed before any upsert attempt', async () => {
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

    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createBlockedSourceComment()],
        prMetadataByNumber: new Map(),
        associatedPrByMergeSha: new Map(),
      },
    })

    expect(result.status).toBe('blocked')
    expect(result.action).toBeNull()
    expect(result.index.generation_verdict).toBe('blocked')
  })

  it('GIVEN parent child list variants WHEN parseChecklistIssueNumbers runs THEN checklist and URL bullets are all recognized', () => {
    expect(parseChecklistIssueNumbers([
      '- [ ] #123',
      '- [x] #456',
      '- #789',
      '- https://github.com/squne121/loop-protocol/issues/321',
    ].join('\n'))).toEqual([123, 456, 789, 321])
  })
})
