import { describe, expect, it } from 'vitest'

import { createValidReport } from './report-test-fixtures'
import {
  buildRetroIndex,
  detectSchemaMigrationRequirement,
  RETRO_INDEX_ALGORITHM,
} from '../../scripts/agent-logs/lib/retro-index-builder.mjs'
import { buildAgentRunReportCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderPublicMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
import { renderValidatedPublicMarkdown } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import { computeObservationSourceProjectionDigest } from '../../scripts/agent-logs/lib/observation-source-adapter.mjs'

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

function createObservationSource(overrides = {}) {
  const projection = {
    schema_version: 'observation_source_result/v1',
    source_kind: 'claude_code',
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
  const digest = computeObservationSourceProjectionDigest({
    ...projection,
    ...overrides,
  })
  const base = {
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
      validator_id: 'agent-run-report-schema',
      validator_policy_digest: 'sha256:11111111111111111111111111111111111111111111111111111111111111111111',
      evidence_mode: 'synthetic_only',
      checked_at: '2026-06-29T12:00:00Z',
    },
  }
  return { ...base, ...overrides }
}

function refreshObservationSourceDigest(source) {
  const digest = computeObservationSourceProjectionDigest(source)
  return {
    ...source,
    provenance: {
      ...source.provenance,
      ref: {
        ...source.provenance.ref,
        digest,
      },
      source_projection_digest: digest,
    },
  }
}

function withObservationSource(report, overrides = {}) {
  const sourceOverrides = overrides.source || {}
  return {
    ...createValidReport(),
    ...report,
    public_safety: {
      ...createValidReport().public_safety,
      ...report.public_safety,
      observation_sources: report.public_safety?.observation_sources ?? [createObservationSource(sourceOverrides)],
    },
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

  it('GIVEN embedded report missing observation_sources WHEN buildRetroIndex runs THEN verdict becomes blocked with missing reason', () => {
    const reportWithoutObservationSource = withObservationSource({
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
          raw_values_emitted: false,
          checked_surfaces: {
            entire_binary: false,
            entire_version: null,
            entire_enable_help: false,
            entire_configure_help: false,
          },
        },
      },
    })
    reportWithoutObservationSource.public_safety.observation_sources = []
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-empty',
      },
      payloadMarkdown: renderPublicMarkdown(reportWithoutObservationSource),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122674',
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
        reason: 'report_observation_sources_empty',
      },
    ])
  })

  it('GIVEN embedded report with regex-valid but unknown observation reason code WHEN buildRetroIndex runs THEN verdict becomes blocked with reason-code-specific reason', () => {
    const report = createValidReport()
    report.public_safety.observation_sources = [
      refreshObservationSourceDigest({
        ...report.public_safety.observation_sources[0],
        safety: {
          ...report.public_safety.observation_sources[0].safety,
          reason_codes: ['ignore_previous_instructions'],
        },
      }),
    ]
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-reason-code',
      },
      payloadMarkdown: renderPublicMarkdown(report),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122674',
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
        reason: 'report_observation_sources_reason_codes_invalid',
      },
    ])
  })

  it('GIVEN embedded report with duplicate observation source_kind WHEN buildRetroIndex runs THEN verdict becomes blocked with duplicate source_kind reason', () => {
    const duplicateKinds = {
      ...withObservationSource({}),
      public_safety: {
        ...withObservationSource({}).public_safety,
        observation_sources: [
          createObservationSource(),
          createObservationSource(),
        ],
      },
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-dup-kind',
      },
      payloadMarkdown: renderPublicMarkdown(duplicateKinds),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122675',
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
        reason: 'report_observation_sources_duplicate_source_kind',
      },
    ])
  })

  it('GIVEN embedded report with duplicate observation source_projection_digest WHEN buildRetroIndex runs THEN verdict becomes blocked with duplicate digest reason', () => {
    const firstSource = createObservationSource()
    const secondSource = refreshObservationSourceDigest(createObservationSource({ source_kind: 'google_antigravity' }))
    const duplicateProjectionDigest = {
      ...withObservationSource({}),
      public_safety: {
        ...withObservationSource({}).public_safety,
        observation_sources: [
          firstSource,
          {
            ...secondSource,
            provenance: {
              ...secondSource.provenance,
              ref: {
                ...secondSource.provenance.ref,
                digest: firstSource.provenance.ref.digest,
              },
              source_projection_digest: firstSource.provenance.source_projection_digest,
            },
          },
        ],
      },
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-dup-proj',
      },
      payloadMarkdown: renderPublicMarkdown(duplicateProjectionDigest),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122676',
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
        reason: 'report_observation_sources_duplicate_projection_digest',
      },
    ])
  })

  it('GIVEN embedded report with mismatched observation source canonical digest WHEN buildRetroIndex runs THEN verdict becomes blocked with digest mismatch reason', () => {
    const withMismatchedDigest = {
      ...withObservationSource({}),
      public_safety: {
        ...withObservationSource({}).public_safety,
        observation_sources: [
          {
            ...createObservationSource(),
            provenance: {
              ...createObservationSource().provenance,
              source_projection_digest: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
            },
          },
        ],
      },
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-digest-mismatch',
      },
      payloadMarkdown: renderPublicMarkdown(withMismatchedDigest),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122676',
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
        reason: 'report_observation_sources_projection_digest_mismatch',
      },
    ])
  })

  it('GIVEN embedded report with real_pilot_verified observation source evidence WHEN buildRetroIndex runs THEN verdict becomes blocked with evidence-mode reason', () => {
    const withRealPilotEvidence = {
      ...withObservationSource({}),
      public_safety: {
        ...withObservationSource({}).public_safety,
        observation_sources: [
          {
            ...createObservationSource(),
            provenance: {
              ...createObservationSource().provenance,
              evidence_mode: 'real_pilot_verified',
            },
          },
        ],
      },
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-real-pilot',
      },
      payloadMarkdown: renderPublicMarkdown(withRealPilotEvidence),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122676',
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
        reason: 'report_observation_sources_evidence_mode',
      },
    ])
  })

  it('GIVEN embedded report with raw_values_emitted true WHEN buildRetroIndex runs THEN verdict becomes blocked with raw_values_emitted reason', () => {
    const rawValuesSource = refreshObservationSourceDigest({
      ...createObservationSource(),
      safety: {
        verdict: 'pass',
        raw_values_emitted: true,
        forbidden_field_scan: 'pass',
        reason_codes: [],
      },
      provenance: structuredClone(createObservationSource().provenance),
    })
    const withRawValues = {
      ...withObservationSource({}),
      public_safety: {
        ...withObservationSource({}).public_safety,
        observation_sources: [rawValuesSource],
      },
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-raw-values',
      },
      payloadMarkdown: renderPublicMarkdown(withRawValues),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122677',
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
        reason: 'report_observation_sources_raw_values_emitted',
      },
    ])
  })

  it('GIVEN embedded report with availability unavailable and non-null metrics WHEN buildRetroIndex runs THEN verdict becomes blocked with unavailable metrics reason', () => {
    const unavailableMetricsSource = refreshObservationSourceDigest({
      ...createObservationSource(),
      availability: 'unavailable',
      safety: {
        ...createObservationSource().safety,
        verdict: 'blocked',
        reason_codes: ['source_unavailable'],
      },
      metrics: {
        trace_count: 1,
        span_count: null,
        prompt_tokens: null,
        completion_tokens: null,
        total_tokens: null,
      },
      provenance: structuredClone(createObservationSource().provenance),
    })
    const withUnavailableMetrics = {
      ...withObservationSource({}),
      public_safety: {
        ...withObservationSource({}).public_safety,
        observation_sources: [unavailableMetricsSource],
      },
    }
    const body = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-obs-unavailable-metrics',
      },
      payloadMarkdown: renderPublicMarkdown(withUnavailableMetrics),
    }).body

    const result = buildRetroIndex({
      parentIssue: 928,
      sourceComments: [{
        html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122678',
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
        reason: 'report_observation_sources_unavailable_metrics',
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
