import { createHash } from 'crypto'

import { runtimeError } from './args.mjs'

const INPUT_SCHEMA_VERSION = 'observation_source_input/v1'
const OUTPUT_SCHEMA_VERSION = 'observation_source_result/v1'
const PROVENANCE_SCHEMA_VERSION = 'observation_source_provenance/v1'
const ADAPTER_ID = 'observation-source-adapter'
const POLICY_ID = 'observation-source-adapter@1'
const POLICY_DIGEST = `sha256:${createHash('sha256').update(POLICY_ID).digest('hex')}`

const INPUT_KINDS = Object.freeze(new Set(['latitude_otlp', 'entirecli']))
const OUTPUT_KINDS = Object.freeze(new Set(['claude_code', 'codex_cli', 'google_antigravity']))
const INPUT_AVAILABILITIES = Object.freeze(new Set(['available', 'unavailable', 'unknown']))
const CAPABILITY_VERDICTS = Object.freeze(new Set(['supported', 'partial', 'unsupported', 'unverified']))
const PROJECTION_MODES = Object.freeze(new Set(['allowlist_projection', 'not_projected']))

const INPUT_ROOT_KEYS = Object.freeze(new Set([
  'schema_version',
  'input_kind',
  'output_source_kind',
  'capability_verdict',
  'availability',
  'projection_mode',
  'checked_at',
  'safety',
  'metrics',
]))

const SAFETY_KEYS = Object.freeze(new Set([
  'verdict',
  'raw_values_emitted',
  'reason_codes',
]))

const INPUT_SCAN_KEY_PATTERNS = [
  /^raw_prompt$/i,
  /^raw_response$/i,
  /^response$/i,
  /^messages$/i,
  /^tool[_-]?io$/i,
  /^tool[_-]?(io|input|output)$/i,
  /^request[_-]?body$/i,
  /^stdout$/i,
  /^stderr$/i,
  /^local[_-]?path$/i,
  /^env$/i,
  /^token$/i,
  /^secret$/i,
  /^authorization$/i,
]

const INPUT_SCAN_VALUE_PATTERNS = [
  /(?:^|[^A-Za-z0-9._-])\/home\/[^\s"'`]+/u,
  /(?:^|[^A-Za-z0-9._-])\/Users\/[^\s"'`]+/u,
  /\b[A-Za-z]:(?:\\|\/)[^ \n\r\t"'`]+/u,
  /\bghp_[A-Za-z0-9]{8,}\b/u,
  /\bgithub_pat_[A-Za-z0-9_]{8,}\b/u,
  /\bsk-[A-Za-z0-9]{8,}\b/u,
  /\bsk-proj-[A-Za-z0-9_-]{8,}\b/u,
  /\bAKIA[0-9A-Z]{16}\b/u,
]

const SAFETY_VERDICTS = Object.freeze(new Set(['pass', 'blocked']))
const OBSERVATION_SOURCE_EVIDENCE_MODES = Object.freeze(new Set(['synthetic_only']))
const OBSERVATION_REASON_CODE_PATTERN = /^[a-z][a-z0-9_]{0,79}$/u
const OBSERVATION_REASON_CODE_MAX_ITEMS = 32
const OBSERVATION_REASON_CODE_MAX_LENGTH = 80
const OBSERVATION_REASON_CODES = Object.freeze([
  'host_inventory_only',
  'partial_projection',
  'source_unavailable',
  'synthetic_only_evidence',
])
const OBSERVATION_REASON_CODE_SET = Object.freeze(new Set(OBSERVATION_REASON_CODES))

function sha256Hex(text) {
  return createHash('sha256').update(text, 'utf-8').digest('hex')
}

function sha256Digest(text) {
  return `sha256:${sha256Hex(text)}`
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function assertObject(value, path, code, message) {
  if (!isObject(value)) {
    throw runtimeError(code, `${path} must be an object`)
  }
  return value
}

function assertStringEnum(value, allowedValues, code, message) {
  if (typeof value !== 'string' || !allowedValues.has(value)) {
    throw runtimeError(code, message)
  }
  return value
}

function assertInteger(value, path, code, message) {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw runtimeError(code, message)
  }
  return value
}

function assertBoolean(value, path, code, message) {
  if (typeof value !== 'boolean') {
    throw runtimeError(code, message)
  }
  return value
}

function assertIsoTimestamp(value, path) {
  if (typeof value !== 'string') {
    throw runtimeError(
      'observation_source.timestamp',
      `observation source input field "${path}" must be an ISO-8601 string`
    )
  }
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString() !== value) {
    throw runtimeError(
      'observation_source.timestamp',
      `observation source input field "${path}" must be a canonical ISO-8601 string`
    )
  }
  return value
}

function canonicalizeJson(value) {
  if (Array.isArray(value)) {
    return value.map(canonicalizeJson)
  }
  if (!isObject(value)) {
    return value
  }
  const canonical = {}
  for (const key of Object.keys(value).sort()) {
    canonical[key] = canonicalizeJson(value[key])
  }
  return canonical
}

function normalizeObservationReasonCodes(value, path) {
  if (!Array.isArray(value)) {
    throw runtimeError('observation_source.reason_codes_type', `${path} must be an array`)
  }
  if (value.length > OBSERVATION_REASON_CODE_MAX_ITEMS) {
    throw runtimeError(
      'observation_source.reason_codes_too_many',
      `${path} must contain at most ${OBSERVATION_REASON_CODE_MAX_ITEMS} items`
    )
  }

  const seen = new Set()
  const normalized = []
  value.forEach((code, index) => {
    const itemPath = `${path}[${index}]`
    if (typeof code !== 'string') {
      throw runtimeError('observation_source.reason_code_type', `${itemPath} must be a string`)
    }
    if (code.length > OBSERVATION_REASON_CODE_MAX_LENGTH) {
      throw runtimeError(
        'observation_source.reason_code_too_long',
        `${itemPath} must be at most ${OBSERVATION_REASON_CODE_MAX_LENGTH} characters`
      )
    }
    if (!OBSERVATION_REASON_CODE_PATTERN.test(code)) {
      throw runtimeError(
        'observation_source.reason_code_pattern',
        `${itemPath} must match ^[a-z][a-z0-9_]{0,79}$`
      )
    }
    if (!OBSERVATION_REASON_CODE_SET.has(code)) {
      throw runtimeError(
        'observation_source.reason_code_unknown',
        `${itemPath} is not an allowed observation source reason code`
      )
    }
    if (seen.has(code)) {
      throw runtimeError('observation_source.reason_code_duplicate', `${itemPath} duplicates ${code}`)
    }
    seen.add(code)
    normalized.push(code)
  })

  return normalized.sort()
}

function assertObservationReasonCodeSemantics(reasonCodes, availability, capabilityVerdict, path) {
  if (availability === 'unavailable' && !reasonCodes.includes('source_unavailable')) {
    throw runtimeError(
      'observation_source.reason_code_required',
      `${path} must include source_unavailable when availability is unavailable`
    )
  }
  if (availability === 'available' && reasonCodes.includes('source_unavailable')) {
    throw runtimeError(
      'observation_source.reason_code_semantics',
      `${path} must not include source_unavailable when availability is available`
    )
  }
  if (capabilityVerdict === 'partial' && availability === 'available' && !reasonCodes.includes('partial_projection')) {
    throw runtimeError(
      'observation_source.reason_code_required',
      `${path} must include partial_projection when capability_verdict is partial and availability is available`
    )
  }
  if ((capabilityVerdict === 'supported' || capabilityVerdict === 'unsupported' || capabilityVerdict === 'unverified')
    && reasonCodes.includes('partial_projection')) {
    throw runtimeError(
      'observation_source.reason_code_semantics',
      `${path} must not include partial_projection when capability_verdict is ${capabilityVerdict}`
    )
  }
}

function assertObjectClosed(value, path, code, allowedKeys) {
  const unknownKeys = Object.keys(value).filter((key) => !allowedKeys.has(key))
  if (unknownKeys.length > 0) {
    throw runtimeError(
      code,
      `${path} contains unknown keys: ${unknownKeys.sort().join(', ')}`
    )
  }
}

function scanForForbidden(value, path, violations) {
  if (value === null || value === undefined) {
    return
  }
  if (Array.isArray(value)) {
    value.forEach((entry, index) => {
      scanForForbidden(entry, `${path}[${index}]`, violations)
    })
    return
  }

  if (typeof value === 'string') {
    for (const pattern of INPUT_SCAN_VALUE_PATTERNS) {
      if (pattern.test(value)) {
        violations.push(path)
        break
      }
    }
    return
  }

  if (isObject(value)) {
    for (const [key, child] of Object.entries(value)) {
      const keyCandidates = [key, ...key.split('.').filter(Boolean)]
      if (keyCandidates.some((candidate) => INPUT_SCAN_KEY_PATTERNS.some((pattern) => pattern.test(candidate)))) {
        violations.push(`${path}.${key}`)
      }
      scanForForbidden(child, `${path}.${key}`, violations)
    }
  }
}

function normalizeOutputKind(outputSourceKind) {
  return assertStringEnum(
    outputSourceKind,
    OUTPUT_KINDS,
    'observation_source.output_source_kind',
    'observation source input output_source_kind must be claude_code|codex_cli|google_antigravity'
  )
}

function normalizeCheckedAt(inputCheckedAt, optionsCheckedAt) {
  const checkedAt = inputCheckedAt ?? optionsCheckedAt
  return assertIsoTimestamp(checkedAt, 'checked_at')
}

function normalizeAvailability(inputAvailability) {
  if (inputAvailability === undefined) {
    return 'unavailable'
  }
  if (inputAvailability === 'unknown') {
    return 'unavailable'
  }
  return assertStringEnum(
    inputAvailability,
    INPUT_AVAILABILITIES,
    'observation_source.availability',
    `observation source input availability must be available|unavailable|unknown`
  ) === 'available' ? 'available' : 'unavailable'
}

function normalizeProjectionMode(inputProjectionMode, availability) {
  if (availability === 'unavailable') {
    return 'not_projected'
  }
  if (inputProjectionMode === undefined) {
    return 'allowlist_projection'
  }
  return assertStringEnum(
    inputProjectionMode,
    PROJECTION_MODES,
    'observation_source.projection_mode',
    `observation_source.projection_mode must be allowlist_projection or not_projected`
  )
}

function normalizeMetrics(inputMetrics, availability) {
  if (availability === 'unavailable') {
    return {
      trace_count: null,
      span_count: null,
      prompt_tokens: null,
      completion_tokens: null,
      total_tokens: null,
    }
  }
  const metrics = assertObject(inputMetrics, '$.metrics', 'observation_source.metrics', 'observation source metrics must be an object when availability is available')
  assertObjectClosed(metrics, '$.metrics', 'observation_source.metrics', new Set([
    'trace_count',
    'span_count',
    'prompt_tokens',
    'completion_tokens',
    'total_tokens',
  ]))
  const normalized = {
    trace_count: assertInteger(metrics.trace_count, '$.metrics.trace_count', 'observation_source.metrics', '$.metrics.trace_count must be a non-negative integer'),
    span_count: assertInteger(metrics.span_count, '$.metrics.span_count', 'observation_source.metrics', '$.metrics.span_count must be a non-negative integer'),
    prompt_tokens: assertInteger(metrics.prompt_tokens, '$.metrics.prompt_tokens', 'observation_source.metrics', '$.metrics.prompt_tokens must be a non-negative integer'),
    completion_tokens: assertInteger(metrics.completion_tokens, '$.metrics.completion_tokens', 'observation_source.metrics', '$.metrics.completion_tokens must be a non-negative integer'),
    total_tokens: assertInteger(metrics.total_tokens, '$.metrics.total_tokens', 'observation_source.metrics', '$.metrics.total_tokens must be a non-negative integer'),
  }
  return normalized
}

function normalizeSafety(inputSafety, path, availability) {
  const safety = assertObject(
    inputSafety,
    `${path}.safety`,
    'observation_source.safety',
    'observation source safety must be an object'
  )
  assertObjectClosed(safety, `${path}.safety`, 'observation_source.safety', SAFETY_KEYS, 'observation source safety contains unknown fields')

  const reasonCodes = normalizeObservationReasonCodes(safety.reason_codes, `${path}.safety.reason_codes`)
  const verdict = assertStringEnum(
    safety.verdict,
    SAFETY_VERDICTS,
    'observation_source.safety_verdict',
    `${path}.safety.verdict must be pass|blocked`
  )
  const rawValuesEmitted = assertBoolean(
    safety.raw_values_emitted,
    `${path}.safety.raw_values_emitted`,
    'observation_source.safety_raw_values_emitted',
    `${path}.safety.raw_values_emitted must be boolean`
  )

  return {
    verdict,
    raw_values_emitted: rawValuesEmitted,
    forbidden_field_scan: 'pass',
    reason_codes: reasonCodes,
  }
}

function normalizeCapabilityVerdict(inputVerdict) {
  return assertStringEnum(
    inputVerdict,
    CAPABILITY_VERDICTS,
    'observation_source.capability_verdict',
    'observation source capability_verdict must be supported|partial|unsupported|unverified'
  )
}

function buildDigestProjection(normalizedProjection) {
  return {
    schema_version: normalizedProjection.schema_version,
    source_kind: normalizedProjection.source_kind,
    capability_verdict: normalizedProjection.capability_verdict,
    availability: normalizedProjection.availability,
    projection_mode: normalizedProjection.projection_mode,
    safety: normalizedProjection.safety,
    metrics: normalizedProjection.metrics,
  }
}

export function computeObservationSourceProjectionDigest(normalizedProjection) {
  return sha256Digest(JSON.stringify(canonicalizeJson(buildDigestProjection(normalizedProjection))))
}

export function validateObservationSourceProjection(source) {
  const normalizedSource = assertObject(
    source,
    '$.public_safety.observation_sources[]',
    'observation_source.invalid_projection',
    'observation source projection must be an object'
  )
  const digestProjection = buildDigestProjection(normalizedSource)
  const canonicalDigest = computeObservationSourceProjectionDigest(digestProjection)
  const provenance = assertObject(
    normalizedSource.provenance,
    '$.public_safety.observation_sources[].provenance',
    'observation_source.provenance',
    'observation source provenance must be an object'
  )
  const ref = assertObject(
    provenance.ref,
    '$.public_safety.observation_sources[].provenance.ref',
    'observation_source.provenance_ref',
    'observation source provenance.ref must be an object'
  )

  if (ref.kind !== 'observation_projection_digest') {
    throw runtimeError(
      'observation_source.ref_kind',
      'observation source provenance.ref.kind must be observation_projection_digest'
    )
  }
  if (provenance.evidence_mode !== 'synthetic_only' || !OBSERVATION_SOURCE_EVIDENCE_MODES.has(provenance.evidence_mode)) {
    throw runtimeError(
      'observation_source.evidence_mode',
      'observation source provenance.evidence_mode must remain synthetic_only until real pilot is approved'
    )
  }
  if (provenance.source_projection_digest !== canonicalDigest) {
    throw runtimeError(
      'observation_source.projection_digest_mismatch',
      'observation source provenance.source_projection_digest must match canonical public projection digest'
    )
  }
  if (ref.digest !== canonicalDigest) {
    throw runtimeError(
      'observation_source.ref_digest_mismatch',
      'observation source provenance.ref.digest must match canonical public projection digest'
    )
  }

  const safety = assertObject(
    normalizedSource.safety,
    '$.public_safety.observation_sources[].safety',
    'observation_source.safety',
    'observation source safety must be an object'
  )
  const reasonCodes = normalizeObservationReasonCodes(
    safety.reason_codes,
    '$.public_safety.observation_sources[].safety.reason_codes'
  )
  assertObservationReasonCodeSemantics(
    reasonCodes,
    normalizedSource.availability,
    normalizedSource.capability_verdict,
    '$.public_safety.observation_sources[].safety.reason_codes'
  )

  return canonicalDigest
}

function normalizeOutput(input, options = {}) {
  const sourceInput = assertObject(input, '$', 'observation_source.input_not_object', 'observation source input must be a JSON object')

  const inputKeys = Object.keys(sourceInput)
  if (inputKeys.length === 0) {
    throw runtimeError('observation_source.empty_input', 'observation source input must not be empty')
  }

  const violations = []
  scanForForbidden(sourceInput, '$', violations)
  if (violations.length > 0) {
    throw runtimeError(
      'observation_source.forbidden_fields',
      `observation source input contains forbidden keys/values: ${violations.join(', ')}`
    )
  }
  if (inputKeys.some((key) => !INPUT_ROOT_KEYS.has(key))) {
    const unknownKeys = inputKeys
      .filter((key) => !INPUT_ROOT_KEYS.has(key))
      .sort()
    throw runtimeError('observation_source.unknown_key', `observation source input contains unknown key(s): ${unknownKeys.join(', ')}`)
  }

  assertStringEnum(
    sourceInput.schema_version,
    new Set([INPUT_SCHEMA_VERSION]),
    'observation_source.schema_version',
    `observation source input schema_version must be ${INPUT_SCHEMA_VERSION}`
  )
  const checkedAt = normalizeCheckedAt(sourceInput.checked_at, options.checkedAt)
  const inputKind = assertStringEnum(
    sourceInput.input_kind,
    INPUT_KINDS,
    'observation_source.input_kind',
    `observation source input input_kind must be one of ${[...INPUT_KINDS].join(', ')}`
  )
  const outputKind = normalizeOutputKind(sourceInput.output_source_kind)

  const capabilityVerdict = normalizeCapabilityVerdict(sourceInput.capability_verdict)
  let availability = normalizeAvailability(sourceInput.availability)

  if (capabilityVerdict === 'unsupported' || capabilityVerdict === 'unverified') {
    availability = 'unavailable'
  }

  if (availability === 'unavailable' && capabilityVerdict === 'partial') {
    throw runtimeError('observation_source.inconsistent_availability', 'partial capability with unavailable data is not allowed')
  }

  const projectionMode = normalizeProjectionMode(sourceInput.projection_mode, availability)
  const safety = normalizeSafety(
    sourceInput.safety,
    '$',
    availability,
  )

  if (availability === 'unavailable' && !safety.reason_codes.includes('source_unavailable')) {
    safety.reason_codes = [...safety.reason_codes, 'source_unavailable'].sort()
  }
  if (capabilityVerdict === 'partial' && availability === 'available' && !safety.reason_codes.includes('partial_projection')) {
    safety.reason_codes = [...safety.reason_codes, 'partial_projection'].sort()
  }
  assertObservationReasonCodeSemantics(
    safety.reason_codes,
    availability,
    capabilityVerdict,
    '$.safety.reason_codes'
  )

  if (capabilityVerdict === 'unsupported' || capabilityVerdict === 'unverified') {
    safety.verdict = 'blocked'
  }

  const metrics = normalizeMetrics(sourceInput.metrics, availability)

  const normalizedProjection = {
    schema_version: OUTPUT_SCHEMA_VERSION,
    source_kind: outputKind,
    capability_verdict: capabilityVerdict,
    availability,
    projection_mode: projectionMode,
    safety,
    metrics,
  }

  const sourceProjectionDigest = computeObservationSourceProjectionDigest(normalizedProjection)

  if (safety.raw_values_emitted) {
    throw runtimeError(
      'observation_source.raw_values_emitted',
      'observation source safety raw_values_emitted must be false'
    )
  }

  return {
    schema_version: OUTPUT_SCHEMA_VERSION,
    source_kind: outputKind,
    capability_verdict: normalizedProjection.capability_verdict,
    availability,
    projection_mode: projectionMode,
    safety: {
      verdict: safety.verdict,
      raw_values_emitted: safety.raw_values_emitted,
      forbidden_field_scan: safety.forbidden_field_scan,
      reason_codes: safety.reason_codes,
    },
    metrics,
    provenance: {
      schema_version: PROVENANCE_SCHEMA_VERSION,
      ref: {
        kind: 'observation_projection_digest',
        artifact_id: null,
        artifact_digest: null,
        workflow_run_url: null,
        schema_ref: null,
        ref: null,
        digest: sourceProjectionDigest,
        validation_verdict: 'pass',
      },
      source_projection_digest: sourceProjectionDigest,
      validator_id: ADAPTER_ID,
      validator_policy_digest: POLICY_DIGEST,
      evidence_mode: 'synthetic_only',
      checked_at: checkedAt,
    },
  }
}

export function buildObservationSourceFromInput(input, options = {}) {
  return normalizeOutput(input, {
    checkedAt: assertIsoTimestamp(options.checkedAt ?? new Date().toISOString(), 'checked_at'),
  })
}
