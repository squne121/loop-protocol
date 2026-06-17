import { mkdtempSync, readFileSync, writeFileSync } from 'fs'
import { execFileSync } from 'child_process'
import { resolve } from 'path'
import { tmpdir } from 'os'
import { validateAgentRunReport } from '../../scripts/lib/agent-run-report-validation.mjs'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..', '..')

describe('generated agent run report', () => {
  it('GIVEN finalize-agent-run output WHEN validated THEN report passes current validator', () => {
    const dir = mkdtempSync(resolve(tmpdir(), 'agent-run-validated-'))
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

    execFileSync(process.execPath, [
      resolve(REPO_ROOT, 'scripts/agent-logs/finalize-agent-run.mjs'),
      '--draft', draft,
      '--output', output,
      '--command-summary-file', commandSummary,
    ], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
    })

    const report = JSON.parse(readFileSync(output, 'utf-8'))
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(true)
  })
})
