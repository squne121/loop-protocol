#!/usr/bin/env node

import { parseArgs, printCliError } from './lib/args.mjs'
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
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  validateTranscriptRefs(options.transcriptRefs ?? [])

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
  })

  await writeJsonAtomic(options.outputPath, report)
  console.log('agent-run:finalize: report written')
}

main().catch((error) => {
  process.exit(printCliError('agent-run:finalize', error))
})
