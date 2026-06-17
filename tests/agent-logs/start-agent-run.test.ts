import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'

import {
  cleanupTempDir,
  createDraftArgs,
  createTempDir,
  readJson,
  runNodeScript,
  START_SCRIPT,
} from './helpers'

const tempDirs: string[] = []

afterEach(() => {
  while (tempDirs.length > 0) {
    cleanupTempDir(tempDirs.pop() as string)
  }
})

describe('start-agent-run', () => {
  it('GIVEN required lifecycle metadata WHEN start-agent-run runs THEN it writes a local draft atomically', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')

    const result = runNodeScript(START_SCRIPT, createDraftArgs(draftPath))

    expect(result.exitCode).toBe(0)
    expect(result.stdout.trim()).toBe('agent-run:start: draft written')
    expect(result.stderr).toBe('')

    const draft = readJson(draftPath)
    expect(draft).toEqual({
      schema: 'agent_run_draft/v1',
      run_id: 'run-936-001',
      target: {
        kind: 'issue',
        id: 936,
      },
      phase: 'implementation',
      actor: {
        type: 'ai_agent',
        name: 'Codex worker',
      },
      started_at: '2026-06-17T12:00:00.000Z',
    })
  })

  it('GIVEN an existing output path WHEN start-agent-run runs again THEN it fails closed without leaking the path', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')

    const first = runNodeScript(START_SCRIPT, createDraftArgs(draftPath))
    expect(first.exitCode).toBe(0)
    const before = readFileSync(draftPath, 'utf-8')

    const second = runNodeScript(START_SCRIPT, createDraftArgs(draftPath))
    expect(second.exitCode).toBe(1)
    expect(second.stderr).toContain('output.exists')
    expect(second.stderr).not.toContain(draftPath)
    expect(readFileSync(draftPath, 'utf-8')).toBe(before)
  })
})
