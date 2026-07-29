import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..', '..')
const hooks = readFileSync(resolve(root, '.codex/hooks.json'), 'utf8')
const adapter = readFileSync(resolve(root, 'scripts/session-recording/codex-hook-adapter.mjs'), 'utf8')

describe('quarantined workflow gates', () => {
  it('does not wire or implement legacy enforcing artifacts', () => {
    const activeSurface = `${hooks}\n${adapter}`
    for (const token of [
      'scope-rollup', 'contract snapshot', 'body SHA', 'launch ledger',
      'session-manifest', 'publish-context', 'controlled executor',
    ]) {
      expect(activeSurface.toLowerCase()).not.toContain(token.toLowerCase())
    }
  })
})
