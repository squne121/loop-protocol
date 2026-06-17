import { assertNonEmptyString, runtimeError } from './args.mjs'

const ALLOWED_COMMAND_SUMMARY_KEYS = new Set([
  'command_label',
  'exit_code',
  'verdict',
  'summary',
  'artifact_ref',
])

const FORBIDDEN_COMMAND_INPUT_KEYS = new Set([
  'stdout',
  'stderr',
  'output',
  'log',
  'full_command_output',
  'full_command',
  'raw_output',
  'env',
  'command',
])

function parseJsonObject(raw, code) {
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw runtimeError(code, 'input must be valid JSON')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw runtimeError(code, 'input must be a JSON object')
  }
  return parsed
}

function assertAllowedKeys(record, allowedKeys, code) {
  for (const key of Object.keys(record)) {
    if (FORBIDDEN_COMMAND_INPUT_KEYS.has(key)) {
      throw runtimeError(code, 'raw command output fields are not allowed')
    }
    if (!allowedKeys.has(key)) {
      throw runtimeError(code, 'unexpected fields are not allowed')
    }
  }
}

export function parseCommandSummaryJson(rawEntries) {
  if (!Array.isArray(rawEntries) || rawEntries.length === 0) {
    throw runtimeError('command_summary.required', 'at least one command summary is required')
  }

  return rawEntries.map((rawEntry, index) => {
    const code = `command_summary[${index}]`
    const record = parseJsonObject(rawEntry, code)
    assertAllowedKeys(record, ALLOWED_COMMAND_SUMMARY_KEYS, code)

    const commandLabel = assertNonEmptyString(
      record.command_label,
      `${code}.command_label`,
      'command_label must be a non-empty string',
      { maxLength: 80 }
    )
    if (!Number.isInteger(record.exit_code) || record.exit_code < 0) {
      throw runtimeError(`${code}.exit_code`, 'exit_code must be a non-negative integer')
    }
    if (!['pass', 'fail', 'skip'].includes(record.verdict)) {
      throw runtimeError(`${code}.verdict`, 'verdict must be pass, fail, or skip')
    }
    const summary = assertNonEmptyString(
      record.summary,
      `${code}.summary`,
      'summary must be a non-empty string',
      { maxLength: 280 }
    )

    if (!(record.artifact_ref === null || typeof record.artifact_ref === 'string')) {
      throw runtimeError(`${code}.artifact_ref`, 'artifact_ref must be a string or null')
    }
    if (typeof record.artifact_ref === 'string' && record.artifact_ref.length > 200) {
      throw runtimeError(`${code}.artifact_ref`, 'artifact_ref must be 200 characters or fewer')
    }

    return {
      command_label: commandLabel,
      exit_code: record.exit_code,
      verdict: record.verdict,
      summary,
      artifact_ref: record.artifact_ref,
    }
  })
}

export function parseJsonList(rawEntries, optionName) {
  if (!Array.isArray(rawEntries) || rawEntries.length === 0) {
    return []
  }
  return rawEntries.map((rawEntry, index) => parseJsonObject(rawEntry, `${optionName}[${index}]`))
}
