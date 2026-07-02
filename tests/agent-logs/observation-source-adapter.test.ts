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

  it('GIVEN a dotted forbidden key WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      'stdout.path': '/tmp/raw.txt',
    }))).toThrow(/forbidden/i)
  })

  it('GIVEN latitude input projected to claude_code WHEN adapted THEN closed output enum still permits the projection', () => {
    const result = buildObservationSourceFromInput(createInput({
      input_kind: 'latitude_otlp',
      output_source_kind: 'claude_code',
    }))

    expect(result.source_kind).toBe('claude_code')
  })

  it('GIVEN entirecli input projected to google_antigravity WHEN adapted THEN closed output enum still permits the projection', () => {
    const result = buildObservationSourceFromInput(createInput({
      input_kind: 'entirecli',
      output_source_kind: 'google_antigravity',
    }))

    expect(result.source_kind).toBe('google_antigravity')
  })

  it('GIVEN metrics with a string count WHEN adapted THEN it fails closed instead of coercing numbers', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      metrics: {
        trace_count: '1',
        span_count: 2,
        prompt_tokens: 10,
        completion_tokens: 20,
        total_tokens: 30,
      },
    }))).toThrow(/non-negative integer/)
  })

  it('GIVEN metrics with an unknown field WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      metrics: {
        trace_count: 1,
        span_count: 2,
        prompt_tokens: 10,
        completion_tokens: 20,
        total_tokens: 30,
        extra_metric: 99,
      },
    }))).toThrow(/unknown keys/i)
  })

  it('GIVEN missing safety payload WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: undefined,
    }))).toThrow(/safety must be an object/i)
  })

  it('GIVEN regex-valid but unknown reason code WHEN adapted THEN it fails closed instead of accepting free-form text', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['ignore_previous_instructions'],
      },
    }))).toThrow(/allowed observation source reason code/i)
  })

  it('GIVEN duplicate reason codes WHEN adapted THEN it fails closed instead of deduping', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['source_unavailable', 'source_unavailable'],
      },
    }))).toThrow(/duplicates source_unavailable/i)
  })

  it('GIVEN path-like or secret-like reason code candidates WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['home_alice_claude_settings_local_json'],
      },
    }))).toThrow(/allowed observation source reason code/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['ghp_deadbeef'],
      },
    }))).toThrow(/forbidden|allowed observation source reason code/i)
  })

  it('GIVEN natural language html markdown or punctuation drift in reason codes WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['source unavailable because user disabled telemetry'],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['markdown_fence___'],
      },
    }))).toThrow(/allowed observation source reason code/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['Source_Unavailable'],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['source-unavailable'],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['source.unavailable'],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['source/unavailable'],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)
  })

  it('GIVEN html marker markdown fence empty string or non-array reason codes WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['<!-- CHATGPT_RETRO_CONTEXT_V1 start -->'],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['```'],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: [''],
      },
    }))).toThrow(/match \^\[a-z\]\[a-z0-9_\]\{0,79\}\$/i)

    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: 123,
      },
    }))).toThrow(/must be an array/i)
  })

  it('GIVEN non-string reason code item WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: [123],
      },
    }))).toThrow(/must be a string/i)
  })

  it('GIVEN semantic inverse drift in reason codes WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      availability: 'available',
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['source_unavailable'],
      },
    }))).toThrow(/must not include source_unavailable/i)

    expect(() => buildObservationSourceFromInput(createInput({
      capability_verdict: 'supported',
      availability: 'available',
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: ['partial_projection'],
      },
    }))).toThrow(/must not include partial_projection/i)

    expect(() => buildObservationSourceFromInput(createInput({
      capability_verdict: 'unsupported',
      availability: 'unavailable',
      safety: {
        verdict: 'blocked',
        raw_values_emitted: false,
        reason_codes: ['source_unavailable', 'partial_projection'],
      },
    }))).toThrow(/must not include partial_projection/i)
  })

  it('GIVEN too many reason codes WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: new Array(33).fill('host_inventory_only'),
      },
    }))).toThrow(/at most 32 items/i)
  })

  it('GIVEN a reason code longer than 80 chars WHEN adapted THEN it fails closed', () => {
    expect(() => buildObservationSourceFromInput(createInput({
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: [`${'a'.repeat(80)}b`],
      },
    }))).toThrow(/at most 80 characters/i)
  })

  it('GIVEN partial available input without partial_projection WHEN adapted THEN adapter auto-adds the semantic reason code', () => {
    const result = buildObservationSourceFromInput(createInput({
      capability_verdict: 'partial',
      availability: 'available',
      safety: {
        verdict: 'pass',
        raw_values_emitted: false,
        reason_codes: [],
      },
    }))

    expect(result.safety.reason_codes).toContain('partial_projection')
  })
})
