import {
  renderPublicMarkdown,
  validateAgentRunReport,
  validateMarkdownCandidate,
} from '../../lib/agent-run-report-validation.mjs'
import { runtimeError } from './args.mjs'

const ENTIRECLI_SAFETY_SCHEMA_VERSION = 'entirecli_safety_result/v1'
const ENTIRECLI_SAFE_VERDICTS = new Set(['safe', 'not_applicable'])
const OBSERVATION_METRIC_FIELDS = [
  'trace_count',
  'span_count',
  'prompt_tokens',
  'completion_tokens',
  'total_tokens',
]

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

function assertObservationSourcesRuntime(report) {
  if (report.public_surface_kind === 'none') {
    return
  }

  const observationSources = report?.public_safety?.observation_sources
  if (observationSources === undefined) {
    throw runtimeError('report.observation_sources_missing', 'public surface reports require public_safety.observation_sources')
  }
  if (!Array.isArray(observationSources)) {
    throw runtimeError('report.observation_sources_invalid', 'public_safety.observation_sources must be an array on public surface reports')
  }
  if (observationSources.length === 0) {
    throw runtimeError('report.observation_sources_empty', 'public_safety.observation_sources must not be empty on public surface reports')
  }

  const seenSourceKinds = new Set()
  const seenProjectionDigests = new Set()
  for (const source of observationSources) {
    const sourceKind = source?.source_kind
    if (typeof sourceKind !== 'string' || sourceKind.length === 0) {
      throw runtimeError('report.observation_sources_source_kind', 'public_safety.observation_sources entries must include source_kind')
    }
    if (seenSourceKinds.has(sourceKind)) {
      throw runtimeError('report.observation_sources_duplicate_source_kind', `duplicate public safety source_kind: ${sourceKind}`)
    }
    seenSourceKinds.add(sourceKind)

    const sourceProjectionDigest = source?.provenance?.source_projection_digest
    if (typeof sourceProjectionDigest !== 'string' || sourceProjectionDigest.length === 0) {
      throw runtimeError('report.observation_sources_projection_digest', 'public_safety.observation_sources entries must include provenance.source_projection_digest')
    }
    if (seenProjectionDigests.has(sourceProjectionDigest)) {
      throw runtimeError('report.observation_sources_duplicate_projection_digest', `duplicate source_projection_digest: ${sourceProjectionDigest}`)
    }
    seenProjectionDigests.add(sourceProjectionDigest)

    const safety = source?.safety
    if (safety?.raw_values_emitted === true) {
      throw runtimeError('report.observation_sources_raw_values_emitted', 'observation_source.safety.raw_values_emitted must be false on public surface')
    }

    if (source?.availability === 'unavailable') {
      for (const field of OBSERVATION_METRIC_FIELDS) {
        if (source?.metrics?.[field] !== null) {
          throw runtimeError('report.observation_sources_unavailable_metrics', `observation_source.metrics.${field} must be null when availability is unavailable`)
        }
      }
    }
  }
}

export function validateFinalReport(report) {
  assertObservationSourcesRuntime(report)
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
