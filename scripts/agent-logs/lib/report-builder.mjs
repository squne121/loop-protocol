import { validateAgentRunReport } from '../../lib/agent-run-report-validation.mjs'
import {
  assertEnum,
  assertIntegerString,
  assertIsoTimestamp,
  assertNonEmptyString,
  runtimeError,
} from './args.mjs'

const OPAQUE_REF_PATH_PATTERNS = [
  /(^|[^A-Za-z0-9._-])\/home\/[^\s"']+/,
  /(^|[^A-Za-z0-9._-])\/Users\/[^\s"']+/,
  /\b[A-Za-z]:(?:\\|\/)[^ \n\r\t"'`]+/,
  /file:\/\//,
]
const OPAQUE_REF_FORBIDDEN_PATTERNS = [
  /<!--\s*agent_run_report:v1(?:\s+start|\s+end)?\s*-->/i,
  /<!--\s*agent_retro_index:v1(?:\s+start|\s+end)?\s*-->/i,
  /`{3,}/,
]

const VALIDATOR_VERSION = '1.0.0'
const CHECK_COMMAND = 'pnpm agent-run-report:check'
const ENTIRECLI_SAFETY_SCHEMA_VERSION = 'entirecli_safety_result/v1'
const ENTIRECLI_SAFE_VERDICTS = new Set(['safe', 'not_applicable'])

function buildPublicSafety(publicSurfaceKind, checkedAt) {
  const isPublicSurface = publicSurfaceKind !== 'none'
  return {
    redaction_status: isPublicSurface ? 'clean' : 'clean',
    checked_by: CHECK_COMMAND,
    validator_version: VALIDATOR_VERSION,
    checked_at: checkedAt,
    verdict: isPublicSurface ? 'pass' : 'pass',
    blocked_reasons: [],
  }
}

function buildTokenUsage(input) {
  const hasAnyTokenField = input.prompt !== undefined || input.completion !== undefined || input.total !== undefined
  if (!hasAnyTokenField) {
    return {
      availability: 'unavailable',
      source: 'none',
      prompt: null,
      completion: null,
      total: null,
    }
  }

  const source = assertEnum(
    input.source ?? 'unknown',
    ['openai_api', 'entire_cli', 'unknown'],
    'token_usage.source',
    'token usage source must be openai_api, entire_cli, or unknown when usage is available'
  )

  return {
    availability: 'available',
    source,
    prompt: assertIntegerString(String(input.prompt ?? ''), 'token_usage.prompt', 'token_usage.prompt must be a non-negative integer'),
    completion: assertIntegerString(String(input.completion ?? ''), 'token_usage.completion', 'token_usage.completion must be a non-negative integer'),
    total: assertIntegerString(String(input.total ?? ''), 'token_usage.total', 'token_usage.total must be a non-negative integer'),
  }
}

function buildAuthority(actorType, evidenceRefs) {
  if (actorType === 'github_action') {
    return {
      level: 'derived',
      basis: 'github_action_check',
      evidence_refs: evidenceRefs,
    }
  }

  if (actorType === 'human') {
    return {
      level: 'authoritative',
      basis: 'human_attestation',
      evidence_refs: evidenceRefs,
    }
  }

  return {
    level: 'non_authoritative',
    basis: 'ai_self_report',
    evidence_refs: [],
  }
}

/**
 * Validates entirecli_safety input for public surface reports.
 *
 * For public_surface_kind === 'none', entirecli_safety is not required and
 * undefined is returned. For public surfaces, the input must be a
 * checker-produced value from check-entirecli-safety.mjs.
 * not_applicable auto-synthesis is prohibited.
 *
 * @param {unknown} entirecliSafety
 * @param {string} publicSurfaceKind
 * @returns {unknown}
 */
function validateEntireCLISafetyInput(entirecliSafety, publicSurfaceKind) {
  if (publicSurfaceKind === 'none') {
    return undefined
  }
  if (!entirecliSafety) {
    throw runtimeError(
      'report.entirecli_safety_missing',
      'entirecli_safety is required for public surface reports; supply checker-produced output from check-entirecli-safety.mjs'
    )
  }
  const safety = /** @type {Record<string, unknown>} */ (entirecliSafety)
  if (safety.schema_version !== ENTIRECLI_SAFETY_SCHEMA_VERSION) {
    throw runtimeError(
      'report.entirecli_safety_unknown_schema_version',
      `entirecli_safety has unknown schema_version: ${safety.schema_version}`
    )
  }
  if (safety.raw_values_emitted === true) {
    throw runtimeError(
      'report.entirecli_safety_raw_values_emitted',
      'entirecli_safety raw_values_emitted must not be true'
    )
  }
  if (!ENTIRECLI_SAFE_VERDICTS.has(String(safety.verdict))) {
    throw runtimeError(
      'report.entirecli_safety_blocked',
      `entirecli_safety verdict "${safety.verdict}" is not allowed for public surface reports`
    )
  }
  return entirecliSafety
}

export function validateTranscriptRefs(rawRefs) {
  for (const rawRef of rawRefs) {
    const transcriptRef = assertNonEmptyString(
      rawRef,
      'transcript_ref.invalid',
      'transcript_ref must be a non-empty opaque token',
      { maxLength: 240 }
    )
    if (OPAQUE_REF_PATH_PATTERNS.some((pattern) => pattern.test(transcriptRef))) {
      throw runtimeError('transcript_ref.path_like', 'transcript_ref must not be a local path or file URL')
    }
    if (OPAQUE_REF_FORBIDDEN_PATTERNS.some((pattern) => pattern.test(transcriptRef))) {
      throw runtimeError('transcript_ref.invalid', 'transcript_ref must not contain markers or fence sequences')
    }
  }
}

export function buildAgentRunReport(input) {
  const checkedAt = assertIsoTimestamp(input.checkedAt, 'public_safety.checked_at', 'checked_at must be an ISO-8601 timestamp')
  const publicSurfaceKind = assertEnum(
    input.publicSurfaceKind,
    ['none', 'github_issue_comment', 'github_pr_comment'],
    'public_surface_kind',
    'public_surface_kind is invalid'
  )

  const validatedEntireCLISafety = validateEntireCLISafetyInput(input.entirecliSafety, publicSurfaceKind)
  const publicSafety = {
    ...buildPublicSafety(publicSurfaceKind, checkedAt),
    ...(validatedEntireCLISafety !== undefined ? { entirecli_safety: validatedEntireCLISafety } : {}),
  }

  const report = {
    schema: 'agent_run_report/v1',
    public_surface_kind: publicSurfaceKind,
    public_safety: publicSafety,
    actor: {
      type: input.draft.actor.type,
      name: input.draft.actor.name,
    },
    authority: buildAuthority(input.draft.actor.type, input.evidenceRefs),
    token_usage: buildTokenUsage(input.tokenUsage),
    manifest_refs: input.manifestRefs,
    evidence_refs: input.evidenceRefs,
    commands_summary: input.commandSummaries,
    docs_read_refs: input.docsReadRefs,
  }

  const validation = validateAgentRunReport(report)
  if (!validation.valid) {
    throw runtimeError('report.validation_failed', formatValidationSummary(validation.errors))
  }

  return report
}

function formatValidationSummary(errors) {
  if (!Array.isArray(errors) || errors.length === 0) {
    return 'generated report failed validation'
  }
  return errors
    .slice(0, 3)
    .map((error) => `${error.code} at ${error.path}`)
    .join('; ')
}
