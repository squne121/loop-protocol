import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'
import {
  canonicalizeCloudPilotSuccessResultPublicProjection,
  computeCloudPilotSuccessResultDigest,
  validateCloudPilotSuccessResultMarkdown,
} from '../scripts/check-cloud-pilot-success-result.mjs'

const FIXTURES_DIR = resolve(__dirname, 'fixtures/cloud-pilot-success-result')

function readFixture(name: string): string {
  return readFileSync(resolve(FIXTURES_DIR, name), 'utf-8')
}

function validateFixture(name: string) {
  return validateCloudPilotSuccessResultMarkdown(readFixture(name))
}

describe('cloud_pilot_success_result/v1 checker: valid fixtures (AC4, AC11, AC14)', () => {
  const validFixtures = [
    'valid-fixture-only.md',
    'valid-key-order-permuted.md',
    'valid-unicode-nfc-nfd-equivalent.md',
  ]

  for (const fixture of validFixtures) {
    it(`GIVEN ${fixture} WHEN validated THEN checker returns exit 0 (valid)`, () => {
      const result = validateFixture(fixture)
      expect(result.errors).toEqual([])
      expect(result.valid).toBe(true)
    })
  }

  it('GIVEN valid-fixture-only.md THEN decision_ready is false and decision is not adopt_cloud (fixture-only evidence, AC11)', () => {
    const markdown = readFixture('valid-fixture-only.md')
    const jsonMatch = /```json\n([\s\S]*?)\n```/.exec(markdown)
    expect(jsonMatch).not.toBeNull()
    const payload = JSON.parse(jsonMatch![1])
    expect(payload.decision_ready).toBe(false)
    expect(payload.decision).not.toBe('adopt_cloud')
    expect(payload.cloud_adoption_allowed_now).not.toBe(true)
  })
})

// AC20: each negative fixture name maps 1:1 to a stable error code.
const invalidFixtureTable: Array<{ fixture: string; code: string }> = [
  { fixture: 'invalid-missing-required-key.md', code: 'schema.required' },
  { fixture: 'invalid-gate-ref-evidence-mismatch.md', code: 'gate_ref.evidence_mismatch' },
  { fixture: 'invalid-target-marker-mismatch.md', code: 'target.marker_mismatch' },
  { fixture: 'invalid-digest-canonicalization.md', code: 'digest.mismatch' },
  { fixture: 'invalid-forbidden-field-tracestate.md', code: 'forbidden_field' },
  { fixture: 'invalid-adoption-ready-field-injected.md', code: 'evidence_mode.violation' },
  { fixture: 'invalid-gate-1326-open-real-target.md', code: 'gate_ref.not_completed' },
  { fixture: 'invalid-unevaluated-property.md', code: 'schema.unevaluated_property' },
  { fixture: 'invalid-optional-null-vs-absent.md', code: 'digest.mismatch' },
  { fixture: 'invalid-timestamp-offset-drift.md', code: 'digest.mismatch' },
  { fixture: 'invalid-number-exponent.md', code: 'digest.mismatch' },
  { fixture: 'invalid-digest-input-includes-marker.md', code: 'digest.mismatch' },
  { fixture: 'invalid-yaml-payload-rejected.md', code: 'payload.non_json_rejected' },
  { fixture: 'invalid-outer-marker-duplicate.md', code: 'marker.constraint_violation' },
  { fixture: 'invalid-fenced-block-count.md', code: 'marker.constraint_violation' },
  { fixture: 'invalid-marker-injection-in-payload.md', code: 'marker.constraint_violation' },
  { fixture: 'invalid-duplicate-marker-comment.md', code: 'marker.duplicate_comment' },
  { fixture: 'invalid-stale-digest.md', code: 'digest.stale' },
  { fixture: 'invalid-target-kind-mismatch.md', code: 'target.kind_mismatch' },
  { fixture: 'invalid-forbidden-dotted-key.md', code: 'forbidden_field' },
  { fixture: 'invalid-forbidden-camelcase.md', code: 'forbidden_field' },
  { fixture: 'invalid-forbidden-nested.md', code: 'forbidden_field' },
  { fixture: 'invalid-forbidden-array-span-links.md', code: 'forbidden_field' },
  { fixture: 'invalid-raw-trace-id-in-string.md', code: 'trace_id.raw_forbidden' },
  { fixture: 'invalid-all-zero-trace-id.md', code: 'trace_id.raw_forbidden' },
  { fixture: 'invalid-public-correlation-id-is-raw-trace-id.md', code: 'trace_id.raw_forbidden' },
  // fix_delta iteration 2 (OWNER Blockers 1-8): adversarial mutation fixtures,
  // each computed with a genuinely fresh digest so a mismatched/stale digest
  // is never the reason the fixture fails.
  { fixture: 'invalid-fixture-only-adoption-ready-bypass.md', code: 'evidence_mode.violation' },
  { fixture: 'invalid-gate-identity-drift.md', code: 'gate_ref.identity_mismatch' },
  { fixture: 'invalid-outer-parent-issue-mismatch.md', code: 'marker.binding_mismatch' },
  { fixture: 'invalid-outer-result-id-mismatch.md', code: 'marker.binding_mismatch' },
  { fixture: 'invalid-target-marker-value-kind-mismatch.md', code: 'target.marker_value_mismatch' },
  { fixture: 'invalid-upsert-probe-stale-freshness-fresh-digest.md', code: 'digest.stale' },
  { fixture: 'invalid-generated-at-utc-offset.md', code: 'schema.invalid' },
  { fixture: 'invalid-numeric-exponent-fresh-digest.md', code: 'digest.canonicalization_violation' },
  { fixture: 'invalid-diagnostic-context-unknown-field.md', code: 'schema.unevaluated_property' },
]

describe('cloud_pilot_success_result/v1 checker: negative fixtures (AC5-AC9, AC12-AC13, AC15-AC19, AC20)', () => {
  it.each(invalidFixtureTable)('GIVEN $fixture WHEN validated THEN it fails with code $code', ({ fixture, code }) => {
    const result = validateFixture(fixture)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === code)).toBe(true)
  })

  it('GIVEN all invalid fixtures THEN each fixture name maps to exactly one expected error code (AC20 1:1 mapping)', () => {
    const fixtureNames = invalidFixtureTable.map((entry) => entry.fixture)
    expect(new Set(fixtureNames).size).toBe(fixtureNames.length)
  })
})

describe('cloud_pilot_success_result/v1 canonicalization primitives (OWNER Blocker 3)', () => {
  it('GIVEN key-order-permuted payloads WHEN canonicalized THEN they produce identical digests', () => {
    const a = { b: 1, a: 2 }
    const b = { a: 2, b: 1 }
    expect(computeCloudPilotSuccessResultDigest(a)).toBe(computeCloudPilotSuccessResultDigest(b))
  })

  it('GIVEN NFC and NFD forms of the same string WHEN canonicalized THEN they normalize to the same value', () => {
    const nfc = 'café'
    const nfd = 'café'
    expect(nfc).not.toBe(nfd)
    const canonNfc = canonicalizeCloudPilotSuccessResultPublicProjection({ note: nfc })
    const canonNfd = canonicalizeCloudPilotSuccessResultPublicProjection({ note: nfd })
    expect(canonNfc.note).toBe(canonNfd.note)
  })

  it('GIVEN explicit null vs an absent key WHEN canonicalized THEN they are NOT treated as equivalent', () => {
    const withNull = { a: 1, b: null }
    const withoutKey = { a: 1 }
    expect(computeCloudPilotSuccessResultDigest(withNull)).not.toBe(computeCloudPilotSuccessResultDigest(withoutKey))
  })

  it('GIVEN digest output THEN it is prefixed with sha256: (digest_prefix policy)', () => {
    expect(computeCloudPilotSuccessResultDigest({ a: 1 })).toMatch(/^sha256:[0-9a-f]{64}$/)
  })
})

describe('cloud_pilot_success_result/v1 checker: markdown structural constraints (AC17)', () => {
  it('GIVEN markdown with no outer marker WHEN validated THEN it fails with marker.constraint_violation', () => {
    const markdown = [
      '```json',
      '{}',
      '```',
      '<!-- CLOUD_PILOT_SUCCESS_RESULT_DIGEST_V1 sha256=' + '0'.repeat(64) + ' -->',
    ].join('\n')
    const result = validateCloudPilotSuccessResultMarkdown(markdown)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'marker.constraint_violation')).toBe(true)
  })
})
