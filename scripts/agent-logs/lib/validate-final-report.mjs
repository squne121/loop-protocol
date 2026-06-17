import {
  renderPublicMarkdown,
  validateAgentRunReport,
  validateMarkdownCandidate,
} from '../../lib/agent-run-report-validation.mjs'
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

export function renderValidatedPublicMarkdown(report) {
  validateFinalReport(report)
  const markdown = renderPublicMarkdown(report)
  const validation = validateMarkdownCandidate(markdown, 'agent_run_report/v1')
  if (!validation.valid) {
    const summary = validation.errors
      .slice(0, 3)
      .map((error) => `${error.code} at ${error.path}`)
      .join('; ')
    throw runtimeError('report.markdown_validation_failed', summary || 'generated markdown failed validation')
  }
  return markdown
}
