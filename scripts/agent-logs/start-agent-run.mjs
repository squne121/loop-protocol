#!/usr/bin/env node

import { parseArgs, ensureEnum, ensureIsoTimestamp, ensureLength } from './lib/args.mjs'
import { createDraft } from './lib/draft.mjs'
import { writeJsonAtomic } from './lib/atomic-json.mjs'

async function main() {
  try {
    const args = parseArgs(process.argv.slice(2), {
      output: { required: true },
      'run-id': { required: true },
      target: { required: true },
      phase: { required: true },
      'actor-name': { required: true },
      'actor-type': { defaultValue: 'ai_agent' },
      'started-at': { defaultValue: new Date().toISOString() },
    })

    const draft = createDraft({
      runId: ensureLength(args['run-id'], 120, 'run-id'),
      target: ensureLength(args.target, 240, 'target'),
      phase: ensureLength(args.phase, 120, 'phase'),
      actorName: ensureLength(args['actor-name'], 120, 'actor-name'),
      actorType: ensureEnum(args['actor-type'], ['ai_agent', 'github_action', 'human'], 'actor-type'),
      startedAt: ensureIsoTimestamp(args['started-at'], 'started-at'),
    })

    await writeJsonAtomic(args.output, draft)
    process.stdout.write('agent-run:start: ok\n')
  } catch (error) {
    const message = error?.message && /^[a-z0-9_:-]+$/i.test(error.message)
      ? error.message
      : 'agent_run_start_failed'
    process.stderr.write(`${message}\n`)
    process.exit(error.exitCode || 1)
  }
}

main()
