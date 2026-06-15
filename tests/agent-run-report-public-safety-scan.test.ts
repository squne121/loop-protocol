import { describe, expect, it } from 'vitest'
import { createValidReport } from './agent-run-report-test-helpers'
import { scanPublicSafety } from '../scripts/lib/agent-run-report-validation.mjs'

describe('public safety scanner', () => {
  it('GIVEN a local path embedded in summary WHEN scanned THEN path.windows_absolute is reported', () => {
    const report = createValidReport()
    report.commands_summary[0].summary = 'captured from C:\\Users\\runner\\secret.txt'
    const result = scanPublicSafety(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'path.windows_absolute')).toBe(true)
  })

  it('GIVEN a GitHub marker embedded in docs summary WHEN scanned THEN marker injection is reported', () => {
    const report = createValidReport()
    report.docs_read_refs[0].summary = '<!-- agent_run_report:v1 start -->'
    const result = scanPublicSafety(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'markdown.marker_injection')).toBe(true)
  })
})
