import { describe, expect, it } from 'vitest'
import { createValidReport, createValidRetroIndex } from './agent-run-report-test-helpers'
import {
  validateAgentRetroIndex,
  validateAgentRunReport,
} from '../scripts/lib/agent-run-report-validation.mjs'

describe('forbidden public fields', () => {
  it('GIVEN a report with raw_transcript WHEN validated THEN forbidden key is rejected', () => {
    const report = createValidReport() as Record<string, unknown>
    report.raw_transcript = 'secret'
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'forbidden_key.raw_transcript')).toBe(true)
  })

  it('GIVEN a retro index with report_body WHEN validated THEN inline report copy is rejected by schema', () => {
    const retro = createValidRetroIndex() as { entries: Array<Record<string, unknown>> }
    retro.entries[0].report_body = 'agent_run_report/v1'
    const result = validateAgentRetroIndex(retro)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'schema.invalid')).toBe(true)
  })
})
