import { describe, expect, it } from 'vitest'

import { buildObservationSourceFromInput } from '../../scripts/agent-logs/lib/observation-source-adapter.mjs'

function createInput(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 'observation_source_input/v1',
    input_kind: 'entirecli',
    output_source_kind: 'codex_cli',
    capability_verdict: 'supported',
    availability: 'available',
    projection_mode: 'allowlist_projection',
    checked_at: '2026-06-17T12:30:00.000Z',
    safety: {
      verdict: 'pass',
      raw_values_emitted: false,
      reason_codes: [],
    },
    metrics: {
      trace_count: 1,
      span_count: 2,
      prompt_tokens: 10,
      completion_tokens: 20,
      total_tokens: 30,
    },
    ...overrides,
  }
}

describe('observation-source adapter', () => {
  it('GIVEN supported allowlist input WHEN adapted THEN it emits observation_source_result/v1 with canonical digest provenance', () => {
    const result = buildObservationSourceFromInput(createInput())

    expect(result).toMatchObject({
      schema_version: 'observation_source_result/v1',
      source_kind: 'codex_cli',
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
      provenance: {
        schema_version: 'observation_source_provenance/v1',
        validator_id: 'observation-source-adapter',
        evidence_mode: 'synthetic_only',
        checked_at: '2026-06-17T12:30:00.000Z',
      },
    })
    expect(result.provenance.ref.digest).toMatch(/^sha256:[a-f0-9]{64}$/u)
    expect(result.provenance.source_projection_digest).toBe(result.provenance.ref.digest)
  })

  it('GIVEN unknown availability input WHEN adapted THEN it normalizes to unavailable with null metrics', () => {
    const result = buildObservationSourceFromInput(createInput({
      availability: 'unknown',
      metrics: undefined,
    }))

    expect(result.availability).toBe('unavailable')
    expect(result.projection_mode).toBe('not_projected')
    expect(result.metrics).toEqual({
      trace_count: null,
      span_count: null,
      prompt_tokens: null,
      completion_tokens: null,
      total_tokens: null,
    })
    expect(result.safety.reason_codes).toContain('source_unavailable')
  })

  it('GIVEN unsupported capability WHEN adapted THEN it stays unavailable and blocked without numeric metrics', () => {
    const result = buildObservationSourceFromInput(createInput({
      capability_verdict: 'unsupported',
      availability: 'available',
      projection_mode: 'allowlist_projection',
    }))

    expect(result.capability_verdict).toBe('unsupported')
    expect(result.availability).toBe('unavailable')
    expect(result.projection_mode).toBe('not_projected')
    expect(result.safety.verdict).toBe('blocked')
  })

  it('GIVEN forbidden raw fields WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      metrics: {
        trace_count: 1,
        span_count: 2,
        prompt_tokens: 10,
        completion_tokens: 20,
        total_tokens: 30,
        stdout: 'raw',
      },
    }))).toThrow(/forbidden/i)
  })
})
