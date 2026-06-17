import { execFileSync } from 'child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'fs'
import { resolve } from 'path'
import { tmpdir } from 'os'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..', '..')

function runFinalize(args: string[]) {
  return execFileSync(process.execPath, [resolve(REPO_ROOT, 'scripts/agent-logs/finalize-agent-run.mjs'), ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  })
}

describe('finalize-agent-run transcript handling', () => {
  it('GIVEN transcript ref input WHEN report is generated THEN transcript path-like strings are not persisted', () => {
    const dir = mkdtempSync(resolve(tmpdir(), 'agent-run-finalize-'))
    const draft = resolve(dir, 'draft.json')
    const commandSummary = resolve(dir, 'commands.json')
    const output = resolve(dir, 'report.json')

    writeFileSync(draft, JSON.stringify({
      schema: 'agent_run_draft/v1',
      run_id: 'run-936',
      target: 'issue#936',
      phase: 'implementation',
      actor: { type: 'ai_agent', name: 'Codex' },
      started_at: '2026-06-17T11:40:00Z',
    }))
    writeFileSync(commandSummary, JSON.stringify([{
      command_label: 'pnpm test agent-logs',
      exit_code: 0,
      verdict: 'pass',
      summary: 'focused tests passed',
      artifact_ref: null,
    }]))

    runFinalize([
      '--draft', draft,
      '--output', output,
      '--command-summary-file', commandSummary,
      '--transcript-ref', 'opaque-transcript-ref',
    ])

    const content = readFileSync(output, 'utf-8')
    expect(content).not.toContain('transcript')
    expect(content).not.toContain('/home/')
  })
})
