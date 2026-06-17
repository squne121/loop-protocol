import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'

import {
  cleanupTempDir,
  createCommandSummaryJson,
  createDraftArgs,
  createFinalizeArgs,
  createTempDir,
  runNodeScript,
  FINALIZE_SCRIPT,
  START_SCRIPT,
} from './helpers'

const tempDirs: string[] = []

afterEach(() => {
  while (tempDirs.length > 0) {
    cleanupTempDir(tempDirs.pop() as string)
  }
})

describe('finalize-agent-run output contract', () => {
  it('GIVEN valid inputs WHEN finalized THEN stdout does not expose report json or paths', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)

    const result = runNodeScript(FINALIZE_SCRIPT, createFinalizeArgs(draftPath, reportPath))

    expect(result.exitCode).toBe(0)
    expect(result.stdout.trim()).toBe('agent-run:finalize: report written')
    expect(result.stdout).not.toContain('{')
    expect(result.stdout).not.toContain('agent_run_report/v1')
    expect(result.stdout).not.toContain(reportPath)
  })

  it('GIVEN a raw-output field in command summary input WHEN finalize-agent-run runs THEN it rejects the input without echoing payload values', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')
    const forbiddenPayload = 'super secret stdout dump'

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)

    const result = runNodeScript(
      FINALIZE_SCRIPT,
      createFinalizeArgs(draftPath, reportPath, [
        '--command-summary-json',
        createCommandSummaryJson({ stdout: forbiddenPayload }),
      ])
    )

    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('raw command output fields are not allowed')
    expect(result.stderr).not.toContain(forbiddenPayload)
    expect(result.stderr).not.toContain(reportPath)
  })

  it('GIVEN an existing report path WHEN finalize-agent-run runs THEN it fails closed without changing the file', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)
    expect(runNodeScript(FINALIZE_SCRIPT, createFinalizeArgs(draftPath, reportPath)).exitCode).toBe(0)

    const before = readFileSync(reportPath, 'utf-8')
    const second = runNodeScript(FINALIZE_SCRIPT, createFinalizeArgs(draftPath, reportPath))
    expect(second.exitCode).toBe(1)
    expect(second.stderr).toContain('output.exists')
    expect(second.stderr).not.toContain(reportPath)
    expect(readFileSync(reportPath, 'utf-8')).toBe(before)
  })
})
