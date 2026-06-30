import { readFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(__dirname, '../..')

let Ajv2020, ajvFormats

try {
  const ajv2020Module = await import('ajv/dist/2020.js')
  Ajv2020 = ajv2020Module.default
  const formatsModule = await import('ajv-formats')
  ajvFormats = formatsModule.default
} catch (err) {
  console.error('Error: ajv and ajv-formats must be installed as devDependencies')
  process.exit(1)
}

const REPORT_START_MARKER = '<!-- agent_run_report:v1 start -->'
const REPORT_END_MARKER = '<!-- agent_run_report:v1 end -->'
const RETRO_START_MARKER = '<!-- agent_retro_index:v1 start -->'
const RETRO_END_MARKER = '<!-- agent_retro_index:v1 end -->'
const CHATGPT_RETRO_CONTEXT_START_MARKER = '<!-- CHATGPT_RETRO_CONTEXT_V1 start -->'
const CHATGPT_RETRO_CONTEXT_END_MARKER = '<!-- CHATGPT_RETRO_CONTEXT_V1 end -->'
const FORBIDDEN_KEYS = new Set([
  'raw_transcript',
  'transcript_excerpt',
  'local_path',
  'absolute_path',
  'secret_value',
  'full_command_output',
  'full_prompt',
  'artifact_path',
  'transcript_path',
  'raw_manifest_json',
])

const SECRET_PATTERNS = [
  { code: 'secret.ghp', regex: /\bghp_[A-Za-z0-9]{8,}\b/ },
  { code: 'secret.github_pat', regex: /\bgithub_pat_[A-Za-z0-9_]{8,}\b/ },
  { code: 'secret.openai', regex: /\bsk-[A-Za-z0-9]{8,}\b/ },
  { code: 'secret.aws_access_key', regex: /\bAKIA[0-9A-Z]{16}\b/ },
  { code: 'secret.private_key', regex: /-----BEGIN [A-Z ]*PRIVATE KEY-----/ },
  { code: 'secret.vite_sensitive', regex: /\bVITE_[A-Z0-9_]*(?:SECRET|TOKEN|KEY|PASSWORD|PRIVATE)[A-Z0-9_]*\b/ },
]

const PATH_PATTERNS = [
  { code: 'path.unix_absolute', regex: /(^|[^A-Za-z0-9._-])\/home\/[^\s"']+/ },
  { code: 'path.macos_absolute', regex: /(^|[^A-Za-z0-9._-])\/Users\/[^\s"']+/ },
  { code: 'path.windows_absolute', regex: /\b[A-Za-z]:(?:\\|\/)[^ \n\r\t"'`]+/ },
  { code: 'path.file_url', regex: /file:\/\// },
  { code: 'path.dotenv_local', regex: /\.env\.local\b/ },
  { code: 'path.claude_settings_local', regex: /\.claude\/settings\.local\.json\b/ },
]

const MARKER_PATTERNS = [
  { code: 'markdown.marker_injection', regex: /<!--\s*agent_run_report:v1(?:\s+start|\s+end)?\s*-->/ },
  { code: 'markdown.marker_injection', regex: /<!--\s*agent_retro_index:v1(?:\s+start|\s+end)?\s*-->/ },
]
const INLINE_REPORT_COPY_PATTERNS = [
  { code: 'semantic.inline_report_copy', regex: /\bagent_run_report\/v1\b/i },
  { code: 'semantic.inline_report_copy', regex: /\bagent_retro_index\/v1\b/i },
  { code: 'semantic.inline_report_copy', regex: /\bagent_session_manifest\b/i },
  { code: 'semantic.inline_report_copy', regex: /\b(raw_transcript|full_command_output|raw_manifest_json|transcript_excerpt|stdout|stderr|full_prompt)\b/i },
  { code: 'semantic.inline_report_copy', regex: /"schema"\s*:\s*"agent_(?:run_report|retro_index|session_manifest)\/v1"/i },
]

function createAjv() {
  const ajv = new Ajv2020({
    strict: true,
    allErrors: true,
  })
  ajvFormats(ajv)
  return ajv
}

function loadSchema(schemaFile) {
  const schemaPath = resolve(__dirname, '../../docs/schemas', schemaFile)
  return JSON.parse(readFileSync(schemaPath, 'utf-8'))
}

function formatAjvErrors(errors) {
  return (errors || []).map((error) => ({
    path: error.instancePath || 'root',
    code: 'schema.invalid',
    message: error.message || 'schema validation failed',
  }))
}

function validateSchemaObject(schemaFile, json) {
  try {
    const schema = loadSchema(schemaFile)
    const ajv = createAjv()
    const validate = ajv.compile(schema)
    const valid = validate(json)
    return {
      valid: Boolean(valid),
      errors: valid ? [] : formatAjvErrors(validate.errors),
    }
  } catch (err) {
    return {
      valid: false,
      errors: [{
        path: 'schema',
        code: 'schema.compile_error',
        message: err instanceof Error ? err.message : String(err),
      }],
    }
  }
}

function joinPath(parts) {
  return parts
    .map((part, index) => typeof part === 'number'
      ? `[${part}]`
      : index === 0
        ? String(part)
        : `.${part}`)
    .join('')
}

function normalizeScanValue(value) {
  if (typeof value !== 'string') {
    return ''
  }
  let normalized = value.normalize('NFKC')
  for (let i = 0; i < 3; i += 1) {
    try {
      const decoded = decodeURIComponent(normalized)
      if (decoded === normalized) {
        break
      }
      normalized = decoded
    } catch {
      break
    }
  }
  return normalized.replace(/\\\\/g, '/').replace(/\\/g, '/')
}

function isAllowedHexPath(path) {
  return (
    path.endsWith('merge_sha') ||
    path.endsWith('report_digest') ||
    path.endsWith('artifact_digest') ||
    path.endsWith('.digest') ||
    path.endsWith('].digest')
  )
}

function scanStringValue(path, value) {
  const errors = []
  const normalized = normalizeScanValue(value)

  for (const pattern of SECRET_PATTERNS) {
    if (pattern.regex.test(normalized)) {
      errors.push({
        path,
        code: pattern.code,
        message: `forbidden secret-like value detected at ${path}`,
      })
    }
  }

  for (const pattern of PATH_PATTERNS) {
    if (pattern.regex.test(normalized)) {
      errors.push({
        path,
        code: pattern.code,
        message: `forbidden path-like value detected at ${path}`,
      })
    }
  }

  for (const pattern of MARKER_PATTERNS) {
    if (pattern.regex.test(normalized)) {
      errors.push({
        path,
        code: pattern.code,
        message: `forbidden marker text detected at ${path}`,
      })
    }
  }

  if (/`{3,}/.test(normalized)) {
    errors.push({
      path,
      code: 'markdown.fence_breakout',
      message: `forbidden fence breakout sequence detected at ${path}`,
    })
  }

  if (!isAllowedHexPath(path) && /\b[a-f0-9]{40}\b/i.test(normalized)) {
    errors.push({
      path,
      code: 'secret.token_like_hex40',
      message: `forbidden 40-hex token-like value detected at ${path}`,
    })
  }

  return errors
}

function isDeterministicOpaqueRef(ref) {
  if (!ref || typeof ref !== 'object') {
    return false
  }

  switch (ref.kind) {
    case 'manifest_digest':
      return typeof ref.digest === 'string' && /^sha256:[0-9a-f]{64}$/i.test(ref.digest)
    case 'github_actions_artifact':
      return typeof ref.artifact_id === 'string'
        && /^[0-9]{1,20}$/.test(ref.artifact_id)
        && typeof ref.artifact_digest === 'string'
        && /^sha256:[0-9a-f]{64}$/i.test(ref.artifact_digest)
        && typeof ref.workflow_run_url === 'string'
    case 'workflow_run':
      return typeof ref.ref === 'string'
        && /^https:\/\/github\.com\/squne121\/loop-protocol\/actions\/runs\/[0-9]+$/i.test(ref.ref)
        && typeof ref.digest === 'string'
        && /^sha256:[0-9a-f]{64}$/i.test(ref.digest)
    case 'github_comment':
      return typeof ref.ref === 'string'
        && /^https:\/\/github\.com\/squne121\/loop-protocol\/(issues|pull)\/[0-9]+#issuecomment-[0-9]+$/i.test(ref.ref)
        && typeof ref.digest === 'string'
        && /^sha256:[0-9a-f]{64}$/i.test(ref.digest)
    case 'schema_ref':
      return typeof ref.schema_ref === 'string' && ref.schema_ref.length > 0
    default:
      return false
  }
}

function validateOpaqueReference(ref, path, { requirePassVerdict = false } = {}) {
  const errors = []

  if (!isDeterministicOpaqueRef(ref)) {
    errors.push({
      path,
      code: 'semantic.opaque_ref_not_deterministic',
      message: `${path} must be a deterministic opaque reference for its kind`,
    })
  }

  if (requirePassVerdict && ref?.validation_verdict !== 'pass') {
    errors.push({
      path: `${path}.validation_verdict`,
      code: 'semantic.public_ref_validation_verdict',
      message: 'public surface references must carry validation_verdict = "pass"',
    })
  }

  return errors
}

function collectInlineReportCopyErrors(value, basePath = 'root') {
  const errors = []

  if (Array.isArray(value)) {
    value.forEach((entry, index) => {
      errors.push(...collectInlineReportCopyErrors(entry, `${basePath}[${index}]`))
    })
    return errors
  }

  if (value && typeof value === 'object') {
    for (const [key, entry] of Object.entries(value)) {
      const nextPath = basePath === 'root' ? key : `${basePath}.${key}`
      errors.push(...collectInlineReportCopyErrors(entry, nextPath))
    }
    return errors
  }

  if (typeof value !== 'string') {
    return errors
  }

  const normalized = normalizeScanValue(value)
  for (const pattern of INLINE_REPORT_COPY_PATTERNS) {
    if (pattern.regex.test(normalized)) {
      errors.push({
        path: basePath,
        code: pattern.code,
        message: `${basePath} must not inline report/transcript payload-like text`,
      })
      break
    }
  }

  return errors
}

function traversePublicSurface(value, parts = []) {
  const errors = []
  const currentPath = joinPath(parts)

  if (Array.isArray(value)) {
    value.forEach((entry, index) => {
      errors.push(...traversePublicSurface(entry, [...parts, index]))
    })
    return errors
  }

  if (value && typeof value === 'object') {
    for (const [key, entry] of Object.entries(value)) {
      const keyPath = joinPath([...parts, key])
      if (FORBIDDEN_KEYS.has(key)) {
        errors.push({
          path: keyPath,
          code: `forbidden_key.${key}`,
          message: `forbidden key "${key}" is not allowed on public surfaces`,
        })
      }
      errors.push(...scanStringValue(keyPath, key))
      errors.push(...traversePublicSurface(entry, [...parts, key]))
    }
    return errors
  }

  if (typeof value === 'string') {
    errors.push(...scanStringValue(currentPath || 'root', value))
  }

  return errors
}

export function scanPublicSafety(json) {
  const errors = traversePublicSurface(json)
  return {
    valid: errors.length === 0,
    errors,
  }
}

export function validateReportAgainstSchema(json) {
  return validateSchemaObject('agent-run-report.schema.json', json)
}

export function validateRetroIndexAgainstSchema(json) {
  return validateSchemaObject('agent-retro-index.schema.json', json)
}

export function validateChatgptRetroContextMarkerAgainstSchema(json) {
  return validateSchemaObject('chatgpt-retro-context-marker.schema.json', json)
}

export function validateChatgptRetrospectiveResultAgainstSchema(json) {
  return validateSchemaObject('chatgpt-retrospective-result.schema.json', json)
}

export function validateReportSemantics(report) {
  const errors = []

  if (report.public_surface_kind !== 'none') {
    if (report.public_safety?.redaction_status !== 'clean') {
      errors.push({
        path: 'public_safety.redaction_status',
        code: 'semantic.public_surface_redaction_status',
        message: 'public surfaces must declare public_safety.redaction_status = "clean"',
      })
    }
    if (report.public_safety?.verdict !== 'pass') {
      errors.push({
        path: 'public_safety.verdict',
        code: 'semantic.public_surface_verdict',
        message: 'public surfaces must declare public_safety.verdict = "pass"',
      })
    }
    if ((report.public_safety?.blocked_reasons?.length ?? 0) !== 0) {
      errors.push({
        path: 'public_safety.blocked_reasons',
        code: 'semantic.public_surface_blocked_reasons',
        message: 'public surfaces must not carry blocked reasons',
      })
    }
  }

  if (report.actor?.type === 'ai_agent') {
    if (report.authority?.level !== 'non_authoritative') {
      errors.push({
        path: 'authority.level',
        code: 'semantic.ai_authority_level',
        message: 'actor.type ai_agent must use authority.level = "non_authoritative"',
      })
    }
    if (report.authority?.basis !== 'ai_self_report') {
      errors.push({
        path: 'authority.basis',
        code: 'semantic.ai_authority_basis',
        message: 'actor.type ai_agent must use authority.basis = "ai_self_report"',
      })
    }
  }

  if (report.authority?.level !== 'non_authoritative' && (report.authority?.evidence_refs?.length ?? 0) === 0) {
    errors.push({
      path: 'authority.evidence_refs',
      code: 'semantic.authority_evidence_refs_required',
      message: 'authority levels above non_authoritative require deterministic evidence refs',
    })
  }

  for (const [index, ref] of (report.authority?.evidence_refs ?? []).entries()) {
    errors.push(...validateOpaqueReference(ref, `authority.evidence_refs[${index}]`, {
      requirePassVerdict: report.public_surface_kind !== 'none',
    }))
  }

  for (const [index, ref] of (report.manifest_refs ?? []).entries()) {
    errors.push(...validateOpaqueReference(ref, `manifest_refs[${index}]`, {
      requirePassVerdict: report.public_surface_kind !== 'none',
    }))
  }

  for (const [index, ref] of (report.evidence_refs ?? []).entries()) {
    errors.push(...validateOpaqueReference(ref, `evidence_refs[${index}]`, {
      requirePassVerdict: report.public_surface_kind !== 'none',
    }))
  }

  if (report.token_usage?.availability === 'unavailable') {
    if (report.token_usage.source !== 'none') {
      errors.push({
        path: 'token_usage.source',
        code: 'semantic.token_usage_source',
        message: 'token_usage.source must be "none" when availability is "unavailable"',
      })
    }
    for (const field of ['prompt', 'completion', 'total']) {
      if (report.token_usage[field] !== null) {
        errors.push({
          path: `token_usage.${field}`,
          code: 'semantic.token_usage_unavailable_requires_null',
          message: `token_usage.${field} must be null when availability is "unavailable"`,
        })
      }
    }
  }

  return {
    valid: errors.length === 0,
    errors,
  }
}

export function validateRetroIndexSemantics(index) {
  const errors = []

  for (let i = 0; i < (index.entries || []).length; i += 1) {
    const entry = index.entries[i]
    errors.push(...collectInlineReportCopyErrors(entry, `entries[${i}]`))
  }

  for (let i = 0; i < (index.orphan_reports || []).length; i += 1) {
    errors.push(...collectInlineReportCopyErrors(index.orphan_reports[i], `orphan_reports[${i}]`))
  }

  for (let i = 0; i < (index.ambiguous_links || []).length; i += 1) {
    errors.push(...collectInlineReportCopyErrors(index.ambiguous_links[i], `ambiguous_links[${i}]`))
  }

  if (index.generation_verdict === 'complete') {
    if ((index.entries?.length ?? 0) === 0) {
      errors.push({
        path: 'entries',
        code: 'semantic.complete_generation_requires_entries',
        message: 'generation_verdict complete requires at least one entry',
      })
    }
    if ((index.orphan_reports?.length ?? 0) > 0) {
      errors.push({
        path: 'orphan_reports',
        code: 'semantic.complete_generation_disallows_orphans',
        message: 'generation_verdict complete cannot include orphan reports',
      })
    }
    if ((index.ambiguous_links?.length ?? 0) > 0) {
      errors.push({
        path: 'ambiguous_links',
        code: 'semantic.complete_generation_disallows_ambiguous_links',
        message: 'generation_verdict complete cannot include ambiguous links',
      })
    }
  }

  return {
    valid: errors.length === 0,
    errors,
  }
}

export function validateAgentRunReport(report) {
  const schemaResult = validateReportAgainstSchema(report)
  const semanticResult = validateReportSemantics(report)
  const scanResult = scanPublicSafety(report)
  const errors = [
    ...schemaResult.errors,
    ...semanticResult.errors,
    ...scanResult.errors,
  ]
  return {
    valid: errors.length === 0,
    errors,
  }
}

export function validateAgentRetroIndex(index) {
  const schemaResult = validateRetroIndexAgainstSchema(index)
  const semanticResult = validateRetroIndexSemantics(index)
  const scanResult = scanPublicSafety(index)
  const errors = [
    ...schemaResult.errors,
    ...semanticResult.errors,
    ...scanResult.errors,
  ]
  return {
    valid: errors.length === 0,
    errors,
  }
}

export function validateChatgptRetroContextMarker(marker) {
  const schemaResult = validateChatgptRetroContextMarkerAgainstSchema(marker)
  const scanResult = scanPublicSafety(marker)
  const errors = [
    ...schemaResult.errors,
    ...scanResult.errors,
  ]
  return {
    valid: errors.length === 0,
    errors,
  }
}

function getMarkersForSchema(schemaName) {
  if (schemaName === 'agent_retro_index/v1') {
    return {
      start: RETRO_START_MARKER,
      end: RETRO_END_MARKER,
    }
  }
  if (schemaName === 'chatgpt_retro_context_marker/v1') {
    return {
      start: CHATGPT_RETRO_CONTEXT_START_MARKER,
      end: CHATGPT_RETRO_CONTEXT_END_MARKER,
    }
  }
  return {
    start: REPORT_START_MARKER,
    end: REPORT_END_MARKER,
  }
}

function detectSchemaNameFromMarkdown(markdown) {
  const hasReport = markdown.includes(REPORT_START_MARKER) || markdown.includes(REPORT_END_MARKER)
  const hasRetro = markdown.includes(RETRO_START_MARKER) || markdown.includes(RETRO_END_MARKER)
  const hasChatgptRetroContext = markdown.includes(CHATGPT_RETRO_CONTEXT_START_MARKER) || markdown.includes(CHATGPT_RETRO_CONTEXT_END_MARKER)

  const detectedCount = [hasReport, hasRetro, hasChatgptRetroContext].filter(Boolean).length
  if (detectedCount > 1) {
    return {
      ok: false,
      error: {
        path: 'markdown',
        code: 'markdown.multiple_schema_markers',
        message: 'markdown cannot mix public marker schemas',
      },
    }
  }

  if (hasChatgptRetroContext) {
    return { ok: true, schemaName: 'chatgpt_retro_context_marker/v1' }
  }
  if (hasRetro) {
    return { ok: true, schemaName: 'agent_retro_index/v1' }
  }

  return { ok: true, schemaName: 'agent_run_report/v1' }
}

export function extractPayloadFromMarkdown(markdown, expectedSchemaName = null) {
  const detected = detectSchemaNameFromMarkdown(markdown)
  if (!detected.ok) {
    return detected
  }

  const schemaName = expectedSchemaName || detected.schemaName
  const { start, end } = getMarkersForSchema(schemaName)
  const lines = markdown.split('\n')

  let startCount = 0
  let endCount = 0
  let startLine = -1
  let endLine = -1

  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].includes(start)) {
      startCount += 1
      startLine = i
    }
    if (lines[i].includes(end)) {
      endCount += 1
      endLine = i
    }
  }

  if (startCount !== 1) {
    return {
      ok: false,
      error: {
        path: 'markdown.start_marker',
        code: 'markdown.duplicate_start_marker',
        message: `start marker appears ${startCount} times`,
      },
    }
  }
  if (endCount !== 1) {
    return {
      ok: false,
      error: {
        path: 'markdown.end_marker',
        code: 'markdown.duplicate_end_marker',
        message: `end marker appears ${endCount} times`,
      },
    }
  }
  if (startLine >= endLine) {
    return {
      ok: false,
      error: {
        path: 'markdown.marker_order',
        code: 'markdown.marker_order',
        message: 'start marker must appear before end marker',
      },
    }
  }

  let openingFenceLine = -1
  let fenceLength = 0
  for (let i = startLine + 1; i < endLine; i += 1) {
    const match = lines[i].match(/^(`{4,})json\s*$/)
    if (match) {
      openingFenceLine = i
      fenceLength = match[1].length
      break
    }
  }

  if (openingFenceLine === -1) {
    return {
      ok: false,
      error: {
        path: 'markdown.fence',
        code: 'markdown.opening_fence_missing',
        message: 'opening fence not found',
      },
    }
  }

  let closingFenceLine = -1
  for (let i = openingFenceLine + 1; i < endLine; i += 1) {
    if (lines[i] === '`'.repeat(fenceLength)) {
      closingFenceLine = i
      break
    }
  }

  if (closingFenceLine === -1) {
    return {
      ok: false,
      error: {
        path: 'markdown.fence',
        code: 'markdown.fence_mismatch',
        message: `closing fence not found for ${fenceLength} backticks`,
      },
    }
  }

  const jsonText = lines.slice(openingFenceLine + 1, closingFenceLine).join('\n')
  try {
    const payload = JSON.parse(jsonText)
    return {
      ok: true,
      schemaName,
      payload,
    }
  } catch (err) {
    return {
      ok: false,
      error: {
        path: 'markdown.json',
        code: 'markdown.json_parse',
        message: err instanceof Error ? err.message : String(err),
      },
    }
  }
}

export function renderPublicMarkdown(payload) {
  const schemaName = payload?.schema
  const { start, end } = getMarkersForSchema(schemaName)
  const jsonText = JSON.stringify(payload, null, 2)
  const longestBacktickRun = Math.max(...Array.from(jsonText.matchAll(/`+/g), (match) => match[0].length), 0)
  const fenceLength = Math.max(longestBacktickRun + 1, 4)
  const fence = '`'.repeat(fenceLength)
  return [
    start,
    `${fence}json`,
    jsonText,
    fence,
    end,
  ].join('\n')
}

export function validateMarkdownCandidate(markdown, expectedSchemaName = null) {
  const extraction = extractPayloadFromMarkdown(markdown, expectedSchemaName)
  if (!extraction.ok) {
    return {
      valid: false,
      errors: [extraction.error],
    }
  }

  let validation
  if (extraction.schemaName === 'agent_retro_index/v1') {
    validation = validateAgentRetroIndex(extraction.payload)
  } else if (extraction.schemaName === 'chatgpt_retro_context_marker/v1') {
    validation = validateChatgptRetroContextMarker(extraction.payload)
  } else {
    validation = validateAgentRunReport(extraction.payload)
  }

  if (!validation.valid) {
    return validation
  }

  const canonicalMarkdown = renderPublicMarkdown(extraction.payload)
  if (markdown.trim() !== canonicalMarkdown.trim()) {
    return {
      valid: false,
      errors: [{
        path: 'markdown',
        code: 'markdown.round_trip_mismatch',
        message: 'markdown candidate must match canonical renderPublicMarkdown output',
      }],
    }
  }

  return validation
}

export function loadJsonFile(filePath) {
  return JSON.parse(readFileSync(filePath, 'utf-8'))
}

export function getDefaultCheckPatterns() {
  return [
    'tests/fixtures/agent-run-report/*.{json,md}',
    'tests/fixtures/agent-retro-index/*.{json,md}',
    'artifacts/agent-run-report*.json',
    'artifacts/agent-run-report*.md',
    'artifacts/agent-retro-index*.json',
    'artifacts/agent-retro-index*.md',
  ]
}

export { REPO_ROOT }
