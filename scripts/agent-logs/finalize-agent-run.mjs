#!/usr/bin/env node

import { readFileSync } from 'fs'

import { parseArgs, printCliError, runtimeError } from './lib/args.mjs'
import { writeJsonAtomic } from './lib/atomic-json.mjs'
import { parseCommandSummaryJson, parseJsonList } from './lib/command-summary-input.mjs'
import { loadDraft } from './lib/draft.mjs'
import { buildAgentRunReport, validateTranscriptRefs } from './lib/report-builder.mjs'

const OPTION_SPEC = {
  '--draft': { key: 'draftPath', required: true },
  '--output': { key: 'outputPath', required: true },
  '--public-surface-kind': { key: 'publicSurfaceKind', defaultValue: 'none' },
  '--checked-at': { key: 'checkedAt', defaultValue: new Date().toISOString() },
  '--command-summary-json': { key: 'commandSummaryJson', required: true, multiple: true },
  '--manifest-ref-json': { key: 'manifestRefJson', multiple: true },
  '--evidence-ref-json': { key: 'evidenceRefJson', multiple: true },
  '--doc-read-ref-json': { key: 'docReadRefJson', multiple: true },
  '--transcript-ref': { key: 'transcriptRefs', multiple: true },
  '--token-usage-source': { key: 'tokenUsageSource' },
  '--token-prompt': { key: 'tokenPrompt' },
  '--token-completion': { key: 'tokenCompletion' },
  '--token-total': { key: 'tokenTotal' },
  '--entirecli-safety-json': { key: 'entirecliSafetyJson' },
  '--entirecli-safety-file': { key: 'entirecliSafetyFile' },
}

/**
 * Loads the entirecli_safety checker result from CLI options.
 *
 * - If publicSurfaceKind !== 'none', one of --entirecli-safety-json or
 *   --entirecli-safety-file is required (fail-closed: exit 1 if missing).
 * - JSON parse failure is fail-closed (exit 1, report not written).
 */
function loadEntireCLISafetyFromOptions(options) {
  const publicSurfaceKind = options.publicSurfaceKind
  const hasJson = options.entirecliSafetyJson !== undefined
  const hasFile = options.entirecliSafetyFile !== undefined

  if (publicSurfaceKind !== 'none' && !hasJson && !hasFile) {
    throw runtimeError(
      'finalize.entirecli_safety_required',
      'public surface reports require --entirecli-safety-json or --entirecli-safety-file; ' +
      'supply the checker result from check-entirecli-safety.mjs'
    )
  }

  if (!hasJson && !hasFile) {
    return undefined
  }

  let rawJson
  if (hasFile) {
    try {
      rawJson = readFileSync(options.entirecliSafetyFile, 'utf-8')
    } catch {
      throw runtimeError(
        'finalize.entirecli_safety_file_read_failed',
        `could not read entirecli safety file: ${options.entirecliSafetyFile}`
      )
    }
  } else {
    rawJson = options.entirecliSafetyJson
  }

  let parsed
  try {
    parsed = JSON.parse(rawJson)
  } catch {
    throw runtimeError(
      'finalize.entirecli_safety_json_parse_failed',
      'entirecli safety JSON could not be parsed: malformed JSON'
    )
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw runtimeError(
      'finalize.entirecli_safety_json_invalid',
      'entirecli safety JSON must be a non-array object'
    )
  }

  return parsed
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  validateTranscriptRefs(options.transcriptRefs ?? [])

  const entirecliSafety = loadEntireCLISafetyFromOptions(options)
  const draft = await loadDraft(options.draftPath)
  const report = buildAgentRunReport({
    draft,
    publicSurfaceKind: options.publicSurfaceKind,
    checkedAt: options.checkedAt,
    manifestRefs: parseJsonList(options.manifestRefJson, 'manifest_ref'),
    evidenceRefs: parseJsonList(options.evidenceRefJson, 'evidence_ref'),
    docsReadRefs: parseJsonList(options.docReadRefJson, 'doc_read_ref'),
    commandSummaries: parseCommandSummaryJson(options.commandSummaryJson),
    tokenUsage: {
      source: options.tokenUsageSource,
      prompt: options.tokenPrompt,
      completion: options.tokenCompletion,
      total: options.tokenTotal,
    },
    entirecliSafety,
  })

  await writeJsonAtomic(options.outputPath, report)
  console.log('agent-run:finalize: report written')
}

main().catch((error) => {
  process.exit(printCliError('agent-run:finalize', error))
})
