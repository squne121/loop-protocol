import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
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

describe('finalize-agent-run transcript handling', () => {
  it('GIVEN an opaque transcript ref WHEN finalize-agent-run runs THEN it does not persist the token into the report', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')
    const transcriptRef = 'opaque-transcript-token-936'

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)

    const result = runNodeScript(
      FINALIZE_SCRIPT,
      createFinalizeArgs(draftPath, reportPath, ['--transcript-ref', transcriptRef])
    )

    expect(result.exitCode).toBe(0)
    expect(result.stdout.trim()).toBe('agent-run:finalize: report written')
    const reportRaw = readFileSync(reportPath, 'utf-8')
    expect(reportRaw).not.toContain(transcriptRef)
    expect(result.stdout).not.toContain(transcriptRef)
    expect(result.stderr).not.toContain(transcriptRef)
  })

  it('GIVEN a local transcript path passed as transcript-ref WHEN finalize-agent-run runs THEN it rejects the input without echoing the path', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')
    const transcriptPath = '/home/squne/secret-transcript.jsonl'

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)

    const result = runNodeScript(
      FINALIZE_SCRIPT,
      createFinalizeArgs(draftPath, reportPath, ['--transcript-ref', transcriptPath])
    )

    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('transcript_ref.path_like')
    expect(result.stderr).not.toContain(transcriptPath)
  })

  it('GIVEN adversarial transcript-ref forms WHEN finalize-agent-run runs THEN it rejects without echoing them', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)

    for (const transcriptRef of [
      'file:///tmp/secret.log',
      'C:/secret/transcript.log',
      '<!-- agent_run_report:v1 start -->',
    ]) {
      const result = runNodeScript(
        FINALIZE_SCRIPT,
        createFinalizeArgs(draftPath, reportPath, ['--transcript-ref', transcriptRef])
      )

      expect(result.exitCode).toBe(1)
      expect(result.stderr).not.toContain(transcriptRef)
      expect(result.stdout).not.toContain(transcriptRef)
    }
  })
})
