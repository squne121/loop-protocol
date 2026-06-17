import { describe, expect, it } from 'vitest'
import { createValidReport } from './agent-run-report-test-helpers'
import { validateAgentRunReport } from '../scripts/lib/agent-run-report-validation.mjs'

describe('manifest refs allowlist', () => {
  it('GIVEN a report with local_path inside manifest_refs WHEN validated THEN schema rejects the field', () => {
    const report = createValidReport() as { manifest_refs: Array<Record<string, unknown>> }
    report.manifest_refs[0].local_path = '/home/squne/projects/LOOP_PROTOCOL/artifacts/report.json'
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'schema.invalid')).toBe(true)
  })
})
