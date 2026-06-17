import { mkdtempSync, writeFileSync } from 'fs'
import { spawnSync } from 'child_process'
import { resolve } from 'path'
import { tmpdir } from 'os'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..', '..')

describe('command summary input guard', () => {
  it('GIVEN raw output fields WHEN finalize-agent-run parses command summaries THEN it fails closed', () => {
    const dir = mkdtempSync(resolve(tmpdir(), 'agent-run-command-summary-'))
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
      stdout: 'forbidden',
    }]))

    const result = spawnSync(process.execPath, [
      resolve(REPO_ROOT, 'scripts/agent-logs/finalize-agent-run.mjs'),
      '--draft', draft,
      '--output', output,
      '--command-summary-file', commandSummary,
    ], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
    })

    expect(result.status).toBe(1)
    expect(result.stderr).toContain('invalid_command_summary_key')
  })
})
