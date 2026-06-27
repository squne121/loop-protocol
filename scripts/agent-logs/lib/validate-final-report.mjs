import {
  renderPublicMarkdown,
  validateAgentRunReport,
  validateMarkdownCandidate,
} from '../../lib/agent-run-report-validation.mjs'
import { runtimeError } from './args.mjs'

const ENTIRECLI_SAFETY_SCHEMA_VERSION = 'entirecli_safety_result/v1'
const ENTIRECLI_SAFE_VERDICTS = new Set(['safe', 'not_applicable'])

/**
 * Runtime enforcement gate for entirecli_safety on public surface reports.
 *
 * Only checker-produced values (from check-entirecli-safety.mjs) are accepted.
 * not_applicable auto-synthesis is prohibited — the field must be present and
 * carry a checker-produced schema_version.
 *
 * Conditions that fail-closed:
 *   - entirecli_safety missing from public_safety
 *   - schema_version !== 'entirecli_safety_result/v1'
 *   - raw_values_emitted === true
 *   - verdict not in { safe, not_applicable }
 */
function assertEntireCLISafetyRuntime(report) {
  if (report.public_surface_kind === 'none') {
    return
  }
  const safety = report?.public_safety?.entirecli_safety
  if (!safety) {
    throw runtimeError(
      'report.entirecli_safety_missing',
      'entirecli_safety is required in public_safety for public surface reports; checker-produced value from check-entirecli-safety.mjs must be supplied'
    )
  }
  if (safety.schema_version !== ENTIRECLI_SAFETY_SCHEMA_VERSION) {
    throw runtimeError(
      'report.entirecli_safety_unknown_schema_version',
      `entirecli_safety has unknown schema_version: ${safety.schema_version}`
    )
  }
  if (safety.raw_values_emitted === true) {
    throw runtimeError(
      'report.entirecli_safety_raw_values_emitted',
      'entirecli_safety raw_values_emitted must not be true on public surface reports'
    )
  }
  if (!ENTIRECLI_SAFE_VERDICTS.has(safety.verdict)) {
    throw runtimeError(
      'report.entirecli_safety_blocked',
      `entirecli_safety verdict "${safety.verdict}" is not allowed for public surface reports`
    )
  }
}

export function validateFinalReport(report) {
  assertEntireCLISafetyRuntime(report)
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
