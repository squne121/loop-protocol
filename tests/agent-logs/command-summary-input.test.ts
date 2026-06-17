import { afterEach, describe, expect, it } from 'vitest'
import { resolve } from 'path'

import {
  cleanupTempDir,
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

describe('command summary input guard', () => {
  it('GIVEN raw output fields WHEN finalize-agent-run parses command summaries THEN it fails closed', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)

    const result = runNodeScript(
      FINALIZE_SCRIPT,
      createFinalizeArgs(draftPath, reportPath, [
        '--command-summary-json',
        JSON.stringify({
          command_label: 'pnpm test -- tests/agent-logs',
          exit_code: 0,
          verdict: 'pass',
          summary: 'focused tests passed',
          artifact_ref: null,
          stdout: 'forbidden',
        }),
      ])
    )

    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('raw command output fields are not allowed')
  })
})
