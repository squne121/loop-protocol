import { validateAgentRunReport } from '../../lib/agent-run-report-validation.mjs'

export function validateFinalReport(report) {
  const result = validateAgentRunReport(report)
  if (!result.valid) {
    const error = new Error('report_validation_failed')
    error.validationErrors = result.errors
    throw error
  }
}
