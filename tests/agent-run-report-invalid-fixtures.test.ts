import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'
import {
  REPORT_FIXTURES_DIR,
  RETRO_FIXTURES_DIR,
} from './agent-run-report-test-helpers'
import {
  validateAgentRetroIndex,
  validateAgentRunReport,
} from '../scripts/lib/agent-run-report-validation.mjs'

describe('invalid fixtures', () => {
  const reportFixtures = [
    'invalid-forbidden-key.json',
    'invalid-local-path.json',
    'invalid-file-url.json',
    'invalid-ghp-token.json',
    'invalid-github-pat.json',
    'invalid-openai-key.json',
    'invalid-aws-key.json',
    'invalid-private-key.json',
    'invalid-vite-secret.json',
    'invalid-hex-token.json',
    'invalid-token-usage-zero.json',
  ]

  for (const fixture of reportFixtures) {
    it(`GIVEN ${fixture} WHEN validated THEN it fails`, () => {
      const payload = JSON.parse(readFileSync(resolve(REPORT_FIXTURES_DIR, fixture), 'utf-8'))
      const result = validateAgentRunReport(payload)
      expect(result.valid).toBe(false)
    })
  }

  it('GIVEN invalid retro fixture with inline report copy WHEN validated THEN it fails', () => {
    const payload = JSON.parse(readFileSync(resolve(RETRO_FIXTURES_DIR, 'invalid-inline-copy.json'), 'utf-8'))
    const result = validateAgentRetroIndex(payload)
    expect(result.valid).toBe(false)
  })

  it('GIVEN public report with empty manifest_digest ref WHEN validated THEN it fails', () => {
    const payload = JSON.parse(readFileSync(resolve(REPORT_FIXTURES_DIR, 'valid-basic.json'), 'utf-8'))
    payload.manifest_refs = [{
      kind: 'manifest_digest',
      artifact_id: null,
      artifact_digest: null,
      workflow_run_url: null,
      schema_ref: null,
      ref: null,
      digest: null,
      validation_verdict: 'pass',
    }]
    const result = validateAgentRunReport(payload)
    expect(result.valid).toBe(false)
  })

  it('GIVEN double-url-encoded local path WHEN validated THEN it fails', () => {
    const payload = JSON.parse(readFileSync(resolve(REPORT_FIXTURES_DIR, 'valid-basic.json'), 'utf-8'))
    payload.commands_summary[0].summary = '%252Fhome%252Frunner%252Fsecret'
    const result = validateAgentRunReport(payload)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'path.unix_absolute')).toBe(true)
  })
})
