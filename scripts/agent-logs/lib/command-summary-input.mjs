import { readFile } from 'fs/promises'

const ALLOWED_KEYS = new Set([
  'command_label',
  'exit_code',
  'verdict',
  'summary',
  'artifact_ref',
])

const FORBIDDEN_KEYS = new Set([
  'stdout',
  'stderr',
  'output',
  'log',
  'full_command',
  'full_command_output',
  'env',
  'raw_output',
])

function normalizeEntries(payload) {
  return Array.isArray(payload) ? payload : [payload]
}

function validateEntry(entry) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
    throw new Error('invalid_command_summary_entry')
  }

  for (const key of Object.keys(entry)) {
    if (FORBIDDEN_KEYS.has(key) || !ALLOWED_KEYS.has(key)) {
      throw new Error('invalid_command_summary_key')
    }
  }

  if (
    typeof entry.command_label !== 'string'
    || entry.command_label.length === 0
    || entry.command_label.length > 80
    || !Number.isInteger(entry.exit_code)
    || entry.exit_code < 0
    || !['pass', 'fail', 'skip'].includes(entry.verdict)
    || typeof entry.summary !== 'string'
    || entry.summary.length === 0
    || entry.summary.length > 280
    || !(
      entry.artifact_ref === null
      || (typeof entry.artifact_ref === 'string' && entry.artifact_ref.length > 0 && entry.artifact_ref.length <= 200)
    )
  ) {
    throw new Error('invalid_command_summary_shape')
  }

  return entry
}

export async function loadCommandSummaries(filePaths) {
  const summaries = []
  for (const filePath of filePaths) {
    let raw
    try {
      raw = await readFile(filePath, 'utf-8')
    } catch {
      throw new Error('command_summary_read_failed')
    }
    let payload
    try {
      payload = JSON.parse(raw)
    } catch {
      throw new Error('command_summary_parse_failed')
    }
    for (const entry of normalizeEntries(payload)) {
      summaries.push(validateEntry(entry))
    }
  }
  if (summaries.length === 0) {
    throw new Error('missing_command_summaries')
  }
  return summaries
}
