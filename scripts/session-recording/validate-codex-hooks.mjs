#!/usr/bin/env node

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

function fail(message) {
  process.stderr.write(`${message}\n`)
  process.exit(1)
}

function assert(condition, message, failures) {
  if (!condition) {
    failures.push(message)
  }
}

function loadJson(jsonPath) {
  return JSON.parse(readFileSync(resolve(jsonPath), 'utf8'))
}

function findCommands(entries) {
  return (entries ?? []).flatMap((entry) => (entry?.hooks ?? []).map((hook) => hook?.command ?? ''))
}

function validateDocs(repoRoot, failures) {
  const docs = [
    'docs/dev/session-recording-policy.md',
    'docs/dev/onboarding/session-recording-tool.md',
  ].map((path) => readFileSync(resolve(repoRoot, path), 'utf8'))

  const combined = docs.join('\n')
  assert(combined.includes('runtime active hook'), 'docs: must mention runtime active hook state boundary', failures)
  assert(combined.includes('trust state'), 'docs: must mention trust state boundary', failures)
  assert(combined.includes('[features].hooks'), 'docs: must mention [features].hooks as canonical key', failures)
  assert(combined.includes('codex_hooks'), 'docs: must mention codex_hooks alias', failures)
  assert(combined.includes('dangerously-bypass-hook-trust'), 'docs: must mention dangerously-bypass-hook-trust caveat', failures)
}

function main() {
  const jsonPath = process.argv[2]
  if (!jsonPath) {
    fail('Usage: validate-codex-hooks.mjs <hooks.json>')
  }

  const repoRoot = resolve(process.cwd())
  const parsed = loadJson(jsonPath)
  const failures = []
  const hooks = parsed?.hooks ?? {}

  assert(Array.isArray(hooks.SubagentStart), 'hooks.SubagentStart must exist', failures)
  assert(Array.isArray(hooks.PreToolUse), 'hooks.PreToolUse must exist', failures)
  assert(Array.isArray(hooks.Stop), 'hooks.Stop must exist', failures)
  assert(Array.isArray(hooks.SubagentStop), 'hooks.SubagentStop must exist', failures)
  assert(Array.isArray(hooks.PermissionRequest), 'hooks.PermissionRequest must exist', failures)

  const subagentStartCommands = findCommands(hooks.SubagentStart)
  const preToolCommands = findCommands(hooks.PreToolUse)
  const stopCommands = findCommands(hooks.Stop)
  const subagentStopCommands = findCommands(hooks.SubagentStop)
  const permissionCommands = findCommands(hooks.PermissionRequest)

  assert(
    subagentStartCommands.some((command) => command.includes('scripts/check-codex-agents.mjs')),
    'SubagentStart must keep existing check-codex-agents.mjs guardrail',
    failures,
  )
  assert(
    preToolCommands.some((command) => command.includes('scripts/check-codex-agents.mjs')),
    'PreToolUse must keep existing check-codex-agents.mjs guardrail',
    failures,
  )
  assert(
    stopCommands.some((command) => command.includes('.codex/hooks/session-recording-composite.mjs') && command.includes('--event Stop')),
    'Stop must use the session-recording composite handler',
    failures,
  )
  assert(
    subagentStopCommands.some((command) => command.includes('.codex/hooks/session-recording-composite.mjs') && command.includes('--event SubagentStop')),
    'SubagentStop must use the session-recording composite handler',
    failures,
  )
  assert(
    permissionCommands.some((command) => command.includes('--event PermissionRequest')),
    'PermissionRequest must route through the Codex session-recording adapter',
    failures,
  )
  assert(
    permissionCommands.every((command) => command.includes('.codex/hooks/session-recording-composite.mjs')),
    'PermissionRequest hooks must use the composite wrapper',
    failures,
  )

  validateDocs(repoRoot, failures)

  if (failures.length > 0) {
    fail(failures.join('\n'))
  }
}

main()
