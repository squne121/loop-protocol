#!/usr/bin/env node

import { parseArgs, printCliError } from './lib/args.mjs'
import { createDraft } from './lib/draft.mjs'
import { writeJsonAtomic } from './lib/atomic-json.mjs'

const OPTION_SPEC = {
  '--output': { key: 'output', required: true },
  '--run-id': { key: 'runId', required: true },
  '--target-kind': { key: 'targetKind', required: true },
  '--target-id': { key: 'targetId', required: true },
  '--phase': { key: 'phase', required: true },
  '--actor-type': { key: 'actorType', required: true },
  '--actor-name': { key: 'actorName', required: true },
  '--started-at': { key: 'startedAt', defaultValue: new Date().toISOString() },
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  const draft = createDraft(options)
  await writeJsonAtomic(options.output, draft)
  console.log('agent-run:start: draft written')
}

main().catch((error) => {
  process.exit(printCliError('agent-run:start', error))
})
