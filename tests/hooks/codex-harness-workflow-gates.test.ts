import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..', '..')
const adapter = readFileSync(resolve(root, 'scripts/session-recording/codex-hook-adapter.mjs'), 'utf8')

describe('quarantined workflow gates', () => {
  it('does not wire or implement legacy enforcing artifacts', () => {
    // Issue #2161: native Codex CLI retirement removed the hook config file
    // this test previously also inspected; the passive session-recording
    // adapter (still a live, provider-neutral producer) is the sole
    // remaining surface under test.
    const activeSurface = adapter
    for (const token of [
      'scope-rollup', 'contract snapshot', 'body SHA', 'launch ledger',
      'session-manifest', 'publish-context', 'controlled executor',
    ]) {
      expect(activeSurface.toLowerCase()).not.toContain(token.toLowerCase())
    }
  })
})
