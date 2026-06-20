import { describe, expect, it } from 'vitest'
import { writeFileSync, readFileSync } from 'fs'
import { resolve } from 'path'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { execFileSync } from 'child_process'

// Resolve from this test file's location — works both in worktree and main tree
const TESTS_DIR = resolve(__dirname)
const SCRIPTS_ROOT = resolve(TESTS_DIR, '..', '..', 'scripts')
const REPO_ROOT = resolve(TESTS_DIR, '..', '..')
const EXPORT_SCRIPT = resolve(SCRIPTS_ROOT, 'agent-logs', 'export-chatgpt-context.mjs')

function createTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'chatgpt-rendered-'))
}

function cleanupTempDir(dir: string) {
  rmSync(dir, { recursive: true, force: true })
}

function runExportScript(args: string[]) {
  try {
    const stdout = execFileSync(process.execPath, [EXPORT_SCRIPT, ...args], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { exitCode: 0, stdout, stderr: '' }
  } catch (error) {
    const err = error as { status?: number; stdout?: string; stderr?: string }
    return {
      exitCode: err.status ?? 1,
      stdout: err.stdout ?? '',
      stderr: err.stderr ?? '',
    }
  }
}

function makeValidBundle(tempDir: string) {
  const parentIssue = { number: 928, title: 'Parent Issue' }
  const targetIssue = { number: 939, title: 'Target Issue' }
  const retroIndex = {
    schema: 'agent_retro_index/v1',
    entries: [],
    friction_signals: [],
    context_pollution_signals: [],
    ci_review_loops: [],
    human_intervention: null,
    follow_up_candidates: [],
  }
  const sourceSet = { schema: 'source_set/v1', sources: [] }

  const paths = {
    parent: resolve(tempDir, 'parent.json'),
    target: resolve(tempDir, 'target.json'),
    retro: resolve(tempDir, 'retro.json'),
    sourceSet: resolve(tempDir, 'source-set.json'),
    output: resolve(tempDir, 'chatgpt-context.md'),
    summary: resolve(tempDir, 'chatgpt-context-summary.json'),
  }

  writeFileSync(paths.parent, JSON.stringify(parentIssue))
  writeFileSync(paths.target, JSON.stringify(targetIssue))
  writeFileSync(paths.retro, JSON.stringify(retroIndex))
  writeFileSync(paths.sourceSet, JSON.stringify(sourceSet))

  return paths
}

describe('chatgpt-context rendered markdown scan (AC9)', () => {
  it('GIVEN valid clean inputs WHEN running export THEN exits with code 0', () => {
    const tempDir = createTempDir()
    try {
      const paths = makeValidBundle(tempDir)
      const result = runExportScript([
        '--parent-issue-json', paths.parent,
        '--target-issue-json', paths.target,
        '--retro-index-json', paths.retro,
        '--source-set-json', paths.sourceSet,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', paths.output,
        '--summary-json-out', paths.summary,
      ])
      expect(result.exitCode).toBe(0)
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN valid clean inputs WHEN running export THEN stdout contains bundle written message', () => {
    const tempDir = createTempDir()
    try {
      const paths = makeValidBundle(tempDir)
      const result = runExportScript([
        '--parent-issue-json', paths.parent,
        '--target-issue-json', paths.target,
        '--retro-index-json', paths.retro,
        '--source-set-json', paths.sourceSet,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', paths.output,
        '--summary-json-out', paths.summary,
      ])
      expect(result.stdout).toContain('chatgpt-context: bundle written')
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN valid clean inputs WHEN running export THEN output file contains SECURITY_BOUNDARY', () => {
    const tempDir = createTempDir()
    try {
      const paths = makeValidBundle(tempDir)
      runExportScript([
        '--parent-issue-json', paths.parent,
        '--target-issue-json', paths.target,
        '--retro-index-json', paths.retro,
        '--source-set-json', paths.sourceSet,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', paths.output,
        '--summary-json-out', paths.summary,
      ])
      const content = readFileSync(paths.output, 'utf-8')
      expect(content).toContain('SECURITY_BOUNDARY')
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN valid clean inputs WHEN running export THEN stdout does not contain absolute paths', () => {
    const tempDir = createTempDir()
    try {
      const paths = makeValidBundle(tempDir)
      const result = runExportScript([
        '--parent-issue-json', paths.parent,
        '--target-issue-json', paths.target,
        '--retro-index-json', paths.retro,
        '--source-set-json', paths.sourceSet,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', paths.output,
        '--summary-json-out', paths.summary,
      ])
      // stdout should not contain absolute paths per spec
      expect(result.stdout).not.toMatch(/\/home\//)
      expect(result.stdout).not.toMatch(/\/tmp\//)
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN source with forbidden field WHEN running export THEN exits with non-zero code', () => {
    const tempDir = createTempDir()
    try {
      const paths = makeValidBundle(tempDir)
      // Overwrite parent with contaminated data
      writeFileSync(paths.parent, JSON.stringify({ number: 928, title: 'test', raw_transcript: 'data' }))

      const result = runExportScript([
        '--parent-issue-json', paths.parent,
        '--target-issue-json', paths.target,
        '--retro-index-json', paths.retro,
        '--source-set-json', paths.sourceSet,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', paths.output,
        '--summary-json-out', paths.summary,
      ])
      expect(result.exitCode).not.toBe(0)
    } finally {
      cleanupTempDir(tempDir)
    }
  })
})
