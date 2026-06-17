import { describe, expect, it } from 'vitest'
import { createValidReport } from './agent-run-report-test-helpers'
import { validateAgentRunReport } from '../scripts/lib/agent-run-report-validation.mjs'

describe('token usage semantics', () => {
  it('GIVEN token_usage availability unavailable and numeric zero WHEN validated THEN report is rejected', () => {
    const report = createValidReport()
    report.token_usage.prompt = 0
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'semantic.token_usage_unavailable_requires_null')).toBe(true)
  })
})
