import { validateAgentRunReport } from '../../lib/agent-run-report-validation.mjs'
import { runtimeError } from './args.mjs'

export function validateFinalReport(report) {
  const result = validateAgentRunReport(report)
  if (!result.valid) {
    const summary = result.errors
      .slice(0, 3)
      .map((error) => `${error.code} at ${error.path}`)
      .join('; ')
    throw runtimeError('report.validation_failed', summary || 'generated report failed validation')
  }
}
