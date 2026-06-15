import { describe, expect, it } from 'vitest'
import { createValidReport } from './agent-run-report-test-helpers'
import { validateAgentRunReport } from '../scripts/lib/agent-run-report-validation.mjs'

describe('agent_run_report public surface contract', () => {
  it('GIVEN a valid public report WHEN validated THEN required public surface fields pass', () => {
    const result = validateAgentRunReport(createValidReport())
    expect(result.valid).toBe(true)
  })

  it('GIVEN public_surface_kind github_issue_comment and redaction_status blocked WHEN validated THEN report is rejected', () => {
    const report = createValidReport()
    report.public_safety.redaction_status = 'blocked'
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'semantic.public_surface_redaction_status')).toBe(true)
  })
})
