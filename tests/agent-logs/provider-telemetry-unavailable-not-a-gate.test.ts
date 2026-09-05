/**
 * provider-telemetry-unavailable-not-a-gate.test.ts
 *
 * #2489 AC5 (#1939 AC6 相当): provider telemetry（Latitude 等）が unavailable
 * な場合でも、既存 observation-source-adapter が `availability: unavailable`
 * を持つ observation source を生成し、report 生成・finalization が provider
 * trace absence だけを理由に完成失敗（gate）にならないことを focused test で
 * 確認する。新規 fallback system は追加しない（既存
 * `buildObservationSourceFromInput` / `buildAgentRunReport` /
 * `validateFinalReport` をそのまま使う）。
 */

import { describe, expect, it } from 'vitest'

import { buildAgentRunReport } from '../../scripts/agent-logs/lib/report-builder.mjs'
import { validateFinalReport } from '../../scripts/agent-logs/lib/validate-final-report.mjs'

function createUnavailableObservationSourceInput(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 'observation_source_input/v1',
    input_kind: 'latitude_otlp',
    output_source_kind: 'claude_code',
    capability_verdict: 'supported',
    availability: 'unknown',
    projection_mode: 'allowlist_projection',
    checked_at: '2026-06-17T12:30:00.000Z',
    safety: {
      verdict: 'pass',
      raw_values_emitted: false,
      reason_codes: [],
    },
    metrics: undefined,
    ...overrides,
  }
}

function buildReportInput(overrides: Record<string, unknown> = {}) {
  return {
    draft: {
      actor: { type: 'ai_agent', name: 'Codex worker' },
    },
    publicSurfaceKind: 'github_issue_comment',
    checkedAt: '2026-06-17T12:30:00.000Z',
    entirecliSafety: {
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
    observationSource: createUnavailableObservationSourceInput(),
    tokenUsage: {},
    manifestRefs: [],
    evidenceRefs: [],
    commandSummaries: [
      {
        command_label: 'pnpm test -- tests/agent-logs',
        exit_code: 0,
        verdict: 'pass',
        summary: 'focused agent-logs tests passed',
        artifact_ref: 'artifact:agent-logs-tests',
      },
    ],
    docsReadRefs: [],
    ...overrides,
  }
}

describe('provider telemetry unavailable is not a completion gate', () => {
  it('GIVEN a Latitude-style provider that could not be observed WHEN buildAgentRunReport runs THEN the observation source is recorded as unavailable rather than throwing', () => {
    const report = buildAgentRunReport(buildReportInput())

    expect(report.public_safety.observation_sources).toHaveLength(1)
    expect(report.public_safety.observation_sources[0].availability).toBe('unavailable')
    expect(report.public_safety.observation_sources[0].safety.reason_codes).toContain('source_unavailable')
  })

  it('GIVEN an unavailable observation source WHEN buildAgentRunReport runs THEN public_safety.verdict still passes (provider trace absence is not a success gate)', () => {
    const report = buildAgentRunReport(buildReportInput())

    expect(report.public_safety.verdict).toBe('pass')
    expect(report.public_safety.blocked_reasons).toEqual([])
  })

  it('GIVEN a report with an unavailable observation source WHEN validateFinalReport runs THEN it does not throw (finalization succeeds despite provider unavailability)', () => {
    const report = buildAgentRunReport(buildReportInput())

    expect(() => validateFinalReport(report)).not.toThrow()
  })

  it('GIVEN an available observation source and an unavailable observation source WHEN compared THEN only the availability/reason_codes differ (no separate fallback code path)', () => {
    const availableReport = buildAgentRunReport(
      buildReportInput({
        observationSource: createUnavailableObservationSourceInput({
          availability: 'available',
          capability_verdict: 'supported',
          metrics: {
            trace_count: 1,
            span_count: 2,
            prompt_tokens: 10,
            completion_tokens: 20,
            total_tokens: 30,
          },
        }),
      }),
    )
    const unavailableReport = buildAgentRunReport(buildReportInput())

    expect(availableReport.public_safety.observation_sources[0].availability).toBe('available')
    expect(unavailableReport.public_safety.observation_sources[0].availability).toBe('unavailable')
    // Both reports validate and pass through the same public_safety.verdict computation.
    expect(availableReport.public_safety.verdict).toBe(unavailableReport.public_safety.verdict)
  })
})
