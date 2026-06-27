import { describe, expect, it } from 'vitest'

import { createValidReport } from '../agent-run-report-test-helpers'
import {
  buildRetroIndex,
  detectSchemaMigrationRequirement,
  RETRO_INDEX_ALGORITHM,
} from '../../scripts/agent-logs/lib/retro-index-builder.mjs'
import { buildAgentRunReportCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderPublicMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
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

function createMultiPrReport() {
  const report = createValidReport()
  report.docs_read_refs = [
    {
      ref_kind: 'pull_request',
      ref: 'https://github.com/squne121/loop-protocol/pull/955',
      summary: 'Linked PR #955 validated',
    },
    {
      ref_kind: 'pull_request',
      ref: 'https://github.com/squne121/loop-protocol/pull/956',
      summary: 'Linked PR #956 fallback',
    },
    {
      ref_kind: 'issue',
      ref: 'https://github.com/squne121/loop-protocol/issues/935',
      summary: 'Closes #935',
    },
  ]
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122668',
    body: buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-002',
      },
      payloadMarkdown: renderValidatedPublicMarkdown(report),
    }).body,
    linkedPrHints: [955, 956],
    linkedIssueHints: [935],
    branchHint: 'worktree-issue-935-agent-run-report',
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

  it('GIVEN multiple PR refs WHEN one associated PR is authoritative by merge sha THEN buildRetroIndex prefers it over weaker machine refs', () => {
    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [createMultiPrReport()],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map([
        [955, {
          number: 955,
          body: 'Closes #935',
          mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          headRefName: 'worktree-issue-935-agent-run-report',
        }],
        [956, {
          number: 956,
          body: 'Refs #935',
          mergeSha: '',
          headRefName: 'worktree-issue-956-other',
        }],
      ]),
      associatedPrByMergeSha: new Map([
        ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
      ]),
    })

    expect(result.index.generation_verdict).toBe('complete')
    expect(result.index.entries[0]).toMatchObject({
      issue: 935,
      pr: 955,
    })
  })

  it('GIVEN multiple merge-sha associated PR candidates WHEN they disagree THEN buildRetroIndex records an ambiguous link', () => {
    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [createMultiPrReport()],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map([
        [955, {
          number: 955,
          body: 'Closes #935',
          mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          headRefName: 'worktree-issue-935-agent-run-report',
        }],
        [956, {
          number: 956,
          body: 'Refs #935',
          mergeSha: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          headRefName: 'worktree-issue-956-other',
        }],
      ]),
      associatedPrByMergeSha: new Map([
        ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
        ['bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 956],
      ]),
    })

    expect(result.index.generation_verdict).toBe('partial')
    expect(result.index.ambiguous_links).toEqual([
      {
        report_digest: expect.stringMatching(/^sha256:[a-f0-9]{64}$/u),
        reason: 'multiple pull request candidates matched',
      },
    ])
  })

  it('GIVEN a source comment with missing entirecli_safety WHEN buildRetroIndex runs THEN verdict becomes blocked', () => {
    // Create a report without entirecli_safety (bypasses validateFinalReport via renderPublicMarkdown)
    const reportWithoutEntireCLI = {
      ...createValidReport(),
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-15T22:57:00Z',
        verdict: 'pass',
        blocked_reasons: [],
        // no entirecli_safety
      },
      docs_read_refs: [
        {
          ref_kind: 'issue',
          ref: 'https://github.com/squne121/loop-protocol/issues/935',
          summary: 'no entirecli_safety present',
        },
      ],
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-missing-entirecli',
      },
      payloadMarkdown: renderPublicMarkdown(reportWithoutEntireCLI),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122669',
        body,
        linkedPrHints: [955],
        linkedIssueHints: [935],
        branchHint: null,
      }],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map(),
      associatedPrByMergeSha: new Map(),
    })

    expect(result.index.generation_verdict).toBe('blocked')
    expect(result.blockedReasons).toEqual([
      {
        report_digest: expect.stringMatching(/^sha256:[a-f0-9]{64}$/u),
        reason: 'report_entirecli_safety_missing',
      },
    ])
  })

  it('GIVEN embedded report with raw_values_emitted true WHEN buildRetroIndex runs THEN verdict becomes blocked with raw_values_emitted reason', () => {
    // Build a report bypassing validateFinalReport (which rejects raw_values_emitted)
    // by using renderPublicMarkdown directly, then embed it in a source comment body
    const reportWithRawValues = {
      ...createValidReport(),
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-15T22:57:00Z',
        verdict: 'pass',
        blocked_reasons: [],
        entirecli_safety: {
          schema_version: 'entirecli_safety_result/v1',
          verdict: 'not_applicable',
          reason_codes: ['entire_absent'],
          raw_values_emitted: true,
          checked_surfaces: {
            entire_binary: false,
            entire_version: null,
            entire_enable_help: false,
            entire_configure_help: false,
          },
        },
      },
      docs_read_refs: [
        {
          ref_kind: 'issue',
          ref: 'https://github.com/squne121/loop-protocol/issues/935',
          summary: 'raw_values_emitted test report',
        },
      ],
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-raw-values',
      },
      payloadMarkdown: renderPublicMarkdown(reportWithRawValues),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122671',
        body,
        linkedPrHints: [955],
        linkedIssueHints: [935],
        branchHint: null,
      }],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map(),
      associatedPrByMergeSha: new Map(),
    })

    expect(result.index.generation_verdict).toBe('blocked')
    expect(result.blockedReasons).toEqual([
      {
        report_digest: expect.stringMatching(/^sha256:[a-f0-9]{64}$/u),
        reason: 'report_entirecli_safety_raw_values_emitted',
      },
    ])
  })

  it('GIVEN embedded report with unknown schema_version WHEN buildRetroIndex runs THEN verdict becomes blocked with unknown_schema_version reason', () => {
    const reportWithUnknownSchema = {
      ...createValidReport(),
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-15T22:57:00Z',
        verdict: 'pass',
        blocked_reasons: [],
        entirecli_safety: {
          schema_version: 'entirecli_safety_result/v0-legacy',
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
      docs_read_refs: [
        {
          ref_kind: 'issue',
          ref: 'https://github.com/squne121/loop-protocol/issues/935',
          summary: 'unknown schema_version test report',
        },
      ],
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-unknown-schema',
      },
      payloadMarkdown: renderPublicMarkdown(reportWithUnknownSchema),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122672',
        body,
        linkedPrHints: [955],
        linkedIssueHints: [935],
        branchHint: null,
      }],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map(),
      associatedPrByMergeSha: new Map(),
    })

    expect(result.index.generation_verdict).toBe('blocked')
    expect(result.blockedReasons).toEqual([
      {
        report_digest: expect.stringMatching(/^sha256:[a-f0-9]{64}$/u),
        reason: 'report_entirecli_safety_unknown_schema_version',
      },
    ])
  })

  it('GIVEN embedded report with public_surface_kind none and no entirecli_safety WHEN buildRetroIndex runs THEN report is not blocked for missing entirecli_safety', () => {
    // public_surface_kind none: entirecli_safety is not required
    const noneReport = {
      ...createValidReport(),
      public_surface_kind: 'none',
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-15T22:57:00Z',
        verdict: 'pass',
        blocked_reasons: [],
        // no entirecli_safety — valid for none surface
      },
      docs_read_refs: [
        {
          ref_kind: 'issue',
          ref: 'https://github.com/squne121/loop-protocol/issues/935',
          summary: 'none surface report without entirecli_safety',
        },
        {
          ref_kind: 'pull_request',
          ref: 'https://github.com/squne121/loop-protocol/pull/955',
          summary: 'Closes #935',
        },
      ],
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-none-surface',
      },
      payloadMarkdown: renderPublicMarkdown(noneReport),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122673',
        body,
        linkedPrHints: [955],
        linkedIssueHints: [935],
        branchHint: null,
      }],
      parentChildIssueNumbers: [935],
      prMetadataByNumber: new Map([
        [955, {
          number: 955,
          body: 'Closes #935',
          mergeSha: 'cccccccccccccccccccccccccccccccccccccccc',
          headRefName: 'worktree-issue-935-agent-run-report',
        }],
      ]),
      associatedPrByMergeSha: new Map([
        ['cccccccccccccccccccccccccccccccccccccccc', 955],
      ]),
    })

    // none surface report should not be blocked for missing entirecli_safety
    expect(result.blockedReasons).toHaveLength(0)
    expect(result.index.entries).toHaveLength(1)
    expect(result.index.generation_verdict).toBe('complete')
  })
})