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

function findEntryByMatcher(entries, matcher) {
  return (entries ?? []).find((entry) => entry?.matcher === matcher) ?? null
}

function assertExactCommandHook(eventName, expectedMatcher, hook, expected, index, failures) {
  const label = `${eventName} ${expectedMatcher} hook ${index}`
  const expectedKeys = ['command', 'statusMessage', 'timeout', 'type']
  const actualKeys = hook && typeof hook === 'object' && !Array.isArray(hook) ? Object.keys(hook).sort() : []
  assert(
    JSON.stringify(actualKeys) === JSON.stringify(expectedKeys),
    `${label} keys must be exactly ${JSON.stringify(expectedKeys)}, got ${JSON.stringify(actualKeys)}`,
    failures,
  )
  assert(hook?.type === 'command', `${label} type must be command`, failures)
  assert(hook?.command === expected.command, `${label} command must exactly match expected handler`, failures)
  assert(hook?.timeout === expected.timeout, `${label} timeout must be ${expected.timeout}`, failures)
  assert(
    hook?.statusMessage === expected.statusMessage,
    `${label} statusMessage must be ${expected.statusMessage}`,
    failures,
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
  assertExactCommandHook(
    eventName,
    expectedMatcher,
    hook,
    { command: expectedCommand, timeout: 30, statusMessage: expectedStatusMessages[eventName] },
    0,
    failures,
  )
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
    assertExactCommandHook(eventName, expectedMatcher, hook, expected, index, failures)
  }
}

const expectedStatusMessages = {
  SubagentStart: 'Loading LOOP_PROTOCOL subagent guardrail',
  Stop: 'Writing Codex session-recording Stop manifest',
  SubagentStop: 'Writing Codex session-recording SubagentStop manifest',
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
  const rootKeys = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? Object.keys(parsed).sort() : []
  assert(
    JSON.stringify(rootKeys) === JSON.stringify(['hooks']),
    `hooks.json root keys must be exactly ["hooks"], got ${JSON.stringify(rootKeys)}`,
    failures,
  )
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
    const bashEntry = findEntryByMatcher(hooks.PreToolUse, '^Bash$')
    const patchEntry = findEntryByMatcher(hooks.PreToolUse, '^(apply_patch|Edit|Write)$')
    assertEntryHookShape(
      'PreToolUse',
      bashEntry,
      '^Bash$',
      [
        {
          command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/local_main_branch_guard.sh"',
          timeout: 10,
          statusMessage: 'Checking local root branch policy',
        },
        {
          command: 'python3 "$(git rev-parse --show-toplevel)/scripts/agent-guards/worktree_scope_guard.py"',
          timeout: 20,
          statusMessage: 'Checking worktree cleanup scope policy (shared core)',
        },
        {
          command: `${checkCodexAgentsBase} --hook-pretool`,
          timeout: 30,
          statusMessage: 'Checking LOOP_PROTOCOL Bash guardrail',
        },
        {
          command: `${compositeBase} --event PreToolUse`,
          timeout: 30,
          statusMessage: 'Checking Codex session-recording PreToolUse guard',
        },
        {
          command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
          timeout: 10,
          statusMessage: 'Checking CI/test-lane path advisory',
        },
        {
          command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
          timeout: 10,
          statusMessage: 'Checking root temporary residue advisory',
        },
      ],
      failures,
    )
    assertEntryHookShape(
      'PreToolUse',
      patchEntry,
      '^(apply_patch|Edit|Write)$',
      [
        {
          command: 'python3 "$(git rev-parse --show-toplevel)/scripts/agent-guards/codex_apply_patch_adapter.py"',
          timeout: 20,
          statusMessage: 'Checking worktree containment for apply_patch/Edit/Write (shared core)',
        },
        {
          command: `${checkCodexAgentsBase} --hook-pretool`,
          timeout: 30,
          statusMessage: 'Checking LOOP_PROTOCOL patch guardrail',
        },
        {
          command: `${compositeBase} --event PreToolUse`,
          timeout: 30,
          statusMessage: 'Checking Codex session-recording patch guard',
        },
        {
          command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
          timeout: 10,
          statusMessage: 'Checking CI/test-lane path advisory',
        },
        {
          command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
          timeout: 10,
          statusMessage: 'Checking root temporary residue advisory',
        },
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
    const bashEntry = findEntryByMatcher(hooks.PermissionRequest, '^Bash$')
    const patchEntry = findEntryByMatcher(hooks.PermissionRequest, '^(apply_patch|Edit|Write)$')
    assertEntryHookShape(
      'PermissionRequest',
      bashEntry,
      '^Bash$',
      [
        {
          command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/local_main_branch_guard.sh"',
          timeout: 10,
          statusMessage: 'Checking local root branch policy',
        },
        {
          command: `${compositeBase} --event PermissionRequest`,
          timeout: 30,
          statusMessage: 'Checking Codex session-recording PermissionRequest guard',
        },
      ],
      failures,
    )
    assertEntryHookShape(
      'PermissionRequest',
      patchEntry,
      '^(apply_patch|Edit|Write)$',
      [
        {
          command: `${compositeBase} --event PermissionRequest`,
          timeout: 30,
          statusMessage: 'Checking Codex session-recording PermissionRequest guard',
        },
      ],
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
