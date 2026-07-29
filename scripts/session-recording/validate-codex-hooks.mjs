#!/usr/bin/env node

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const expected = {
  SessionEnd: {
    command: 'node .codex/hooks/session-recording-composite.mjs --event SessionEnd',
    timeout: 3,
    statusMessage: 'Recording advisory Codex session metadata',
  },
  SubagentStop: {
    command: 'node .codex/hooks/session-recording-composite.mjs --event SubagentStop',
    timeout: 3,
    statusMessage: 'Recording advisory Codex subagent metadata',
  },
}

function fail(message) {
  process.stderr.write(`${message}\n`)
  process.exit(1)
}

function main() {
  const input = process.argv[2]
  if (!input) fail('Usage: validate-codex-hooks.mjs <hooks.json>')

  let document
  try {
    document = JSON.parse(readFileSync(resolve(input), 'utf8'))
  } catch {
    fail('hooks.json must be valid JSON')
  }

  if (JSON.stringify(Object.keys(document).sort()) !== '["hooks"]') {
    fail('hooks.json root keys must be exactly ["hooks"]')
  }
  const hooks = document.hooks
  if (!hooks || JSON.stringify(Object.keys(hooks).sort()) !== '["SessionEnd","SubagentStop"]') {
    fail('active hooks must be exactly SessionEnd and SubagentStop')
  }

  for (const [event, contract] of Object.entries(expected)) {
    const entries = hooks[event]
    if (!Array.isArray(entries) || entries.length !== 1 || entries[0]?.matcher !== '.*') {
      fail(`${event} must have exactly one catch-all matcher`)
    }
    const commands = entries[0]?.hooks
    if (!Array.isArray(commands) || commands.length !== 1) {
      fail(`${event} must have exactly one command hook`)
    }
    const hook = commands[0]
    if (JSON.stringify(Object.keys(hook).sort()) !== '["command","statusMessage","timeout","type"]') {
      fail(`${event} hook fields are not closed`)
    }
    if (hook.type !== 'command' || hook.command !== contract.command
      || hook.timeout !== contract.timeout || hook.statusMessage !== contract.statusMessage) {
      fail(`${event} hook does not match the passive recorder contract`)
    }
  }
}

main()
