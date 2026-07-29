import { describe, expect, it } from 'vitest'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { spawnSync } from 'node:child_process'

const root = resolve(import.meta.dirname, '..', '..')
const adapter = resolve(root, 'scripts/session-recording/codex-hook-adapter.mjs')

export function invoke(event: string, input: string, directory?: string) {
  return spawnSync(process.execPath, [adapter, '--event', event], {
    cwd: root,
    input,
    encoding: 'utf8',
    timeout: 3000,
    env: { ...process.env, CODEX_PASSIVE_RECORDING_DIR: directory ?? '/dev/null' },
  })
}

describe('Codex passive recorder', () => {
  it('writes allowlisted metadata and SessionEnd emits no stdout', () => {
    const directory = mkdtempSync(resolve(tmpdir(), 'codex-passive-'))
    try {
      const result = invoke('SessionEnd', JSON.stringify({
        session_id: 'session-1',
        transcript: 'never persist this',
        decision: 'deny',
      }), directory)
      expect(result.status).toBe(0)
      expect(result.stdout).toBe('')
      const record = readFileSync(resolve(directory, 'passive-events.jsonl'), 'utf8')
      expect(record).toContain('"session_id":"session-1"')
      expect(record).not.toContain('never persist this')
      expect(record).not.toContain('"decision"')
    } finally {
      rmSync(directory, { recursive: true, force: true })
    }
  })
})
