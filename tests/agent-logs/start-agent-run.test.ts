import { execFileSync } from 'child_process'
import { mkdtempSync, readFileSync } from 'fs'
import { resolve } from 'path'
import { tmpdir } from 'os'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..', '..')

function runStart(args: string[]) {
  return execFileSync(process.execPath, [resolve(REPO_ROOT, 'scripts/agent-logs/start-agent-run.mjs'), ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  })
}

describe('start-agent-run', () => {
  it('GIVEN required args WHEN invoked THEN it atomically writes a local draft', () => {
    const dir = mkdtempSync(resolve(tmpdir(), 'agent-run-start-'))
    const output = resolve(dir, 'draft.json')
    const stdout = runStart([
      '--output', output,
      '--run-id', 'run-936',
      '--target', 'issue#936',
      '--phase', 'implementation',
      '--actor-name', 'Codex',
      '--started-at', '2026-06-17T11:40:00Z',
    ])

    expect(stdout).toBe('agent-run:start: ok\n')
    expect(readFileSync(output, 'utf-8')).toContain('"schema": "agent_run_draft/v1"')
  })
})
