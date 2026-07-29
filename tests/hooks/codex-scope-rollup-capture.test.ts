import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..', '..')
const hooks = JSON.parse(readFileSync(resolve(root, '.codex/hooks.json'), 'utf8')).hooks

describe('旧 scope-rollup capture の隔離', () => {
  it('active Codex hook から旧 capture producer へ到達できない', () => {
    const activeCommands = Object.values(hooks)
      .flatMap((entries) => entries as Array<{ hooks: Array<{ command: string }> }>)
      .flatMap((entry) => entry.hooks)
      .map((hook) => hook.command)

    expect(activeCommands).not.toContain(
      'python3 .claude/hooks/capture_scope_rollup_final_response.py',
    )
    expect(activeCommands.join('\n')).not.toContain('CODEX_SCOPE_ROLLUP_CAPTURE_SCRIPT')
  })
})
