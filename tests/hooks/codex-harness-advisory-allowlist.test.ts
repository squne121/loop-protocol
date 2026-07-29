import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..', '..')
const hooks = JSON.parse(readFileSync(resolve(root, '.codex/hooks.json'), 'utf8')).hooks

describe('Codex passive hook allowlist', () => {
  it('activates only SessionEnd and SubagentStop', () => {
    expect(Object.keys(hooks).sort()).toEqual(['SessionEnd', 'SubagentStop'])
  })

  it('bounds SessionEnd to three seconds', () => {
    expect(hooks.SessionEnd[0].hooks[0].timeout).toBeLessThanOrEqual(3)
  })
})
