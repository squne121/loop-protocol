import { describe, expect, it } from 'vitest'

import { createValidReport } from '../agent-run-report-test-helpers'
import {
  buildRetroIndex,
  detectSchemaMigrationRequirement,
  RETRO_INDEX_ALGORITHM,
} from '../../scripts/agent-logs/lib/retro-index-builder.mjs'
import { buildAgentRunReportCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown } from '../../scripts/agent-logs/lib/validate-final-report.mjs'

function createIssueCommentReport() {
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
    branchHint: null,
  }
}

describe('retro index builder', () => {
  it('GIVEN one valid report WHEN buildRetroIndex runs THEN it resolves complete canonical output without schema expansion', () => {
    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [createIssueCommentReport()],
      parentChildIssueNumbers: [935],
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
    })

    expect(result.algorithmVersion).toBe(RETRO_INDEX_ALGORITHM)
    expect(result.index.generation_verdict).toBe('complete')
    expect(result.index.entries).toHaveLength(1)
    expect(result.index.entries[0]).toMatchObject({
      issue: 935,
      pr: 955,
      merge_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    })
    expect(result.sourceCommentRefs).toHaveLength(1)
    expect(result.summary).toMatchObject({
      generation_verdict: 'complete',
      entry_count: 1,
      orphan_count: 0,
      ambiguous_count: 0,
    })
    expect(detectSchemaMigrationRequirement(result.index)).toBeNull()
  })

  it('GIVEN unresolved pull request metadata WHEN buildRetroIndex runs THEN the report becomes orphaned and verdict stays partial', () => {
    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [createIssueCommentReport()],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map(),
      associatedPrByMergeSha: new Map(),
    })

    expect(result.index.generation_verdict).toBe('partial')
    expect(result.index.entries).toHaveLength(0)
    expect(result.index.orphan_reports).toEqual([
      {
        report_digest: expect.stringMatching(/^sha256:[a-f0-9]{64}$/u),
        reason: 'pull request unresolved',
      },
    ])
  })

  it('GIVEN malformed report markdown WHEN buildRetroIndex runs THEN verdict becomes blocked instead of partial', () => {
    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122667',
        body: '<!-- agent_run_report:v1 repo=squne121/loop-protocol issue=935 pr=955 run_id=run-935 -->',
        linkedPrHints: [955],
        linkedIssueHints: [935],
      }],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map(),
      associatedPrByMergeSha: new Map(),
    })

    expect(result.index.generation_verdict).toBe('blocked')
    expect(result.blockedReasons).toEqual([
      {
        report_digest: 'sha256:malformed',
        reason: 'report_marker_malformed',
      },
    ])
  })
})
