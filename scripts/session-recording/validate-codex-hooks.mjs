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

function collectHooks(entries) {
  return (entries ?? []).flatMap((entry) =>
    (entry?.hooks ?? []).map((hook) => ({
      matcher: entry?.matcher ?? null,
      hook,
    })),
  )
}

function assertExactCompositeHandler(eventName, entries, expectedMatcher, expectedCommand, failures) {
  assert(Array.isArray(entries), `hooks.${eventName} must exist`, failures)
  if (!Array.isArray(entries)) {
    return
  }

  assert(entries.length === 1, `${eventName} must have exactly one matcher entry`, failures)
  const flattened = collectHooks(entries)
  assert(flattened.length === 1, `${eventName} must have exactly one command hook`, failures)
  if (entries.length !== 1 || flattened.length !== 1) {
    return
  }

  const [{ matcher, hook }] = flattened
  assert(matcher === expectedMatcher, `${eventName} matcher must be exactly ${expectedMatcher}`, failures)
  assert(hook?.type === 'command', `${eventName} hook type must be command`, failures)
  assert(hook?.timeout === 30, `${eventName} timeout must be 30`, failures)
  assert(hook?.command === expectedCommand, `${eventName} command must exactly match the composite handler`, failures)
}

function assertEntryHookShape(eventName, entry, expectedMatcher, expectedCommands, failures) {
  assert(entry?.matcher === expectedMatcher, `${eventName} matcher must be exactly ${expectedMatcher}`, failures)
  const hooks = entry?.hooks
  assert(Array.isArray(hooks), `${eventName} ${expectedMatcher} hooks must be an array`, failures)
  if (!Array.isArray(hooks)) {
    return
  }

  assert(
    hooks.length === expectedCommands.length,
    `${eventName} ${expectedMatcher} must have exactly ${expectedCommands.length} hooks`,
    failures,
  )
  if (hooks.length !== expectedCommands.length) {
    return
  }

  for (const [index, expected] of expectedCommands.entries()) {
    const hook = hooks[index]
    assert(hook?.type === 'command', `${eventName} ${expectedMatcher} hook ${index} type must be command`, failures)
    assert(hook?.timeout === 30, `${eventName} ${expectedMatcher} hook ${index} timeout must be 30`, failures)
    assert(
      hook?.command === expected.command,
      `${eventName} ${expectedMatcher} hook ${index} command must exactly match expected handler`,
      failures,
    )
  }
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
  const compositeBase =
    'rtk pnpm exec node "$(git rev-parse --show-toplevel)/.codex/hooks/session-recording-composite.mjs"'
  const checkCodexAgentsBase =
    'rtk pnpm exec node "$(git rev-parse --show-toplevel)/scripts/check-codex-agents.mjs"'

  assert(Array.isArray(hooks.SubagentStart), 'hooks.SubagentStart must exist', failures)
  assert(Array.isArray(hooks.PreToolUse), 'hooks.PreToolUse must exist', failures)
  assert(Array.isArray(hooks.Stop), 'hooks.Stop must exist', failures)
  assert(Array.isArray(hooks.SubagentStop), 'hooks.SubagentStop must exist', failures)
  assert(Array.isArray(hooks.PermissionRequest), 'hooks.PermissionRequest must exist', failures)

  const subagentStartCommands = collectHooks(hooks.SubagentStart).map(({ hook }) => hook?.command ?? '')

  assert(
    subagentStartCommands.some((command) => command.includes('scripts/check-codex-agents.mjs')),
    'SubagentStart must keep existing check-codex-agents.mjs guardrail',
    failures,
  )

  assert(
    Array.isArray(hooks.PreToolUse) && hooks.PreToolUse.length === 2,
    'PreToolUse must have exactly two matcher entries',
    failures,
  )
  if (Array.isArray(hooks.PreToolUse) && hooks.PreToolUse.length === 2) {
    assertEntryHookShape(
      'PreToolUse',
      hooks.PreToolUse[0],
      '^Bash$',
      [
        { command: `${checkCodexAgentsBase} --hook-pretool` },
        { command: `${compositeBase} --event PreToolUse` },
      ],
      failures,
    )
    assertEntryHookShape(
      'PreToolUse',
      hooks.PreToolUse[1],
      '^(apply_patch|Edit|Write)$',
      [
        { command: `${checkCodexAgentsBase} --hook-pretool` },
        { command: `${compositeBase} --event PreToolUse` },
      ],
      failures,
    )
  }

  assert(
    Array.isArray(hooks.PermissionRequest) && hooks.PermissionRequest.length === 2,
    'PermissionRequest must have exactly two matcher entries',
    failures,
  )
  if (Array.isArray(hooks.PermissionRequest) && hooks.PermissionRequest.length === 2) {
    assertEntryHookShape(
      'PermissionRequest',
      hooks.PermissionRequest[0],
      '^Bash$',
      [{ command: `${compositeBase} --event PermissionRequest` }],
      failures,
    )
    assertEntryHookShape(
      'PermissionRequest',
      hooks.PermissionRequest[1],
      '^(apply_patch|Edit|Write)$',
      [{ command: `${compositeBase} --event PermissionRequest` }],
      failures,
    )
  }

  assertExactCompositeHandler('Stop', hooks.Stop, '.*', `${compositeBase} --event Stop`, failures)
  assertExactCompositeHandler(
    'SubagentStop',
    hooks.SubagentStop,
    '.*',
    `${compositeBase} --event SubagentStop`,
    failures,
  )

  validateDocs(repoRoot, failures)

  if (failures.length > 0) {
    fail(failures.join('\n'))
  }
}

main()
