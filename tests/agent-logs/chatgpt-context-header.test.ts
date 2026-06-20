import { describe, expect, it } from 'vitest'
import { writeFileSync, mkdtempSync, rmSync, readFileSync } from 'fs'
import { execFileSync } from 'child_process'
import { resolve } from 'path'
import { tmpdir } from 'os'

import { renderSafetyHeader } from '../../scripts/agent-logs/lib/chatgpt-context-renderer.mjs'

// Resolve from this test file's location — works both in worktree and main tree
const TESTS_DIR = resolve(__dirname)
const SCRIPTS_ROOT = resolve(TESTS_DIR, '..', '..', 'scripts')
const REPO_ROOT = resolve(TESTS_DIR, '..', '..')
const EXPORT_SCRIPT = resolve(SCRIPTS_ROOT, 'agent-logs', 'export-chatgpt-context.mjs')

function createTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'chatgpt-header-'))
}

function cleanupTempDir(dir: string) {
  rmSync(dir, { recursive: true, force: true })
}

function runExportScript(args: string[]) {
  try {
    const stdout = execFileSync(process.execPath, [EXPORT_SCRIPT, ...args], {
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

describe('chatgpt-context machine-readable bundle header (AC10)', () => {
  describe('renderSafetyHeader', () => {
    it('GIVEN header meta WHEN rendering THEN output contains chatgpt_context_bundle/v1 schema', () => {
      const result = renderSafetyHeader({
        generated_at: '2026-06-19T00:00:00.000Z',
        issue: '#939',
        parent_issue: '#928',
      })
      expect(result).toContain('chatgpt_context_bundle/v1')
    })

    it('GIVEN header meta WHEN rendering THEN output contains SECURITY_BOUNDARY comment', () => {
      const result = renderSafetyHeader({
        generated_at: '2026-06-19T00:00:00.000Z',
        issue: '#939',
        parent_issue: '#928',
      })
      expect(result).toContain('<!-- SECURITY_BOUNDARY')
    })

    it('GIVEN header meta WHEN rendering THEN output contains generated_at', () => {
      const result = renderSafetyHeader({
        generated_at: '2026-06-19T00:00:00.000Z',
        issue: '#939',
        parent_issue: '#928',
      })
      expect(result).toContain('generated_at: 2026-06-19T00:00:00.000Z')
    })

    it('GIVEN header meta WHEN rendering THEN output contains issue number', () => {
      const result = renderSafetyHeader({
        generated_at: '2026-06-19T00:00:00.000Z',
        issue: '#939',
        parent_issue: '#928',
      })
      expect(result).toContain('issue: #939')
    })

    it('GIVEN header meta WHEN rendering THEN output contains parent_issue', () => {
      const result = renderSafetyHeader({
        generated_at: '2026-06-19T00:00:00.000Z',
        issue: '#939',
        parent_issue: '#928',
      })
      expect(result).toContain('parent_issue: #928')
    })

    it('GIVEN header meta WHEN rendering THEN output is wrapped in yaml code fence', () => {
      const result = renderSafetyHeader({
        generated_at: '2026-06-19T00:00:00.000Z',
        issue: '#939',
        parent_issue: '#928',
      })
      expect(result).toContain('```yaml')
      expect(result).toContain('```')
    })

    it('GIVEN header meta WHEN rendering THEN schema_version is v1', () => {
      const result = renderSafetyHeader({
        generated_at: '2026-06-19T00:00:00.000Z',
        issue: '#939',
        parent_issue: '#928',
      })
      expect(result).toContain('schema_version: v1')
    })
  })

  describe('bundle file header (end-to-end)', () => {
    it('GIVEN valid inputs WHEN exporting THEN bundle starts with SECURITY_BOUNDARY comment', () => {
      const tempDir = createTempDir()
      try {
        const parentJson = resolve(tempDir, 'parent.json')
        const targetJson = resolve(tempDir, 'target.json')
        const retroJson = resolve(tempDir, 'retro.json')
        const sourceSetJson = resolve(tempDir, 'source-set.json')
        const outputPath = resolve(tempDir, 'bundle.md')
        const summaryPath = resolve(tempDir, 'summary.json')

        writeFileSync(parentJson, JSON.stringify({ number: 928, title: 'Parent' }))
        writeFileSync(targetJson, JSON.stringify({ number: 939, title: 'Target' }))
        writeFileSync(retroJson, JSON.stringify({ entries: [] }))
        writeFileSync(sourceSetJson, JSON.stringify({ sources: [] }))

        const result = runExportScript([
          '--parent-issue-json', parentJson,
          '--target-issue-json', targetJson,
          '--retro-index-json', retroJson,
          '--source-set-json', sourceSetJson,
          '--max-chars', '100000',
          '--max-sections', '20',
          '--generated-at', '2026-06-19T00:00:00.000Z',
          '--output', outputPath,
          '--summary-json-out', summaryPath,
        ])

        expect(result.exitCode).toBe(0)
        const content = readFileSync(outputPath, 'utf-8')
        expect(content.trimStart()).toMatch(/^<!-- SECURITY_BOUNDARY/)
      } finally {
        cleanupTempDir(tempDir)
      }
    })

    it('GIVEN valid inputs WHEN exporting THEN bundle contains yaml chatgpt_context_bundle/v1 block', () => {
      const tempDir = createTempDir()
      try {
        const parentJson = resolve(tempDir, 'parent.json')
        const targetJson = resolve(tempDir, 'target.json')
        const retroJson = resolve(tempDir, 'retro.json')
        const sourceSetJson = resolve(tempDir, 'source-set.json')
        const outputPath = resolve(tempDir, 'bundle.md')
        const summaryPath = resolve(tempDir, 'summary.json')

        writeFileSync(parentJson, JSON.stringify({ number: 928, title: 'Parent' }))
        writeFileSync(targetJson, JSON.stringify({ number: 939, title: 'Target' }))
        writeFileSync(retroJson, JSON.stringify({}))
        writeFileSync(sourceSetJson, JSON.stringify({}))

        runExportScript([
          '--parent-issue-json', parentJson,
          '--target-issue-json', targetJson,
          '--retro-index-json', retroJson,
          '--source-set-json', sourceSetJson,
          '--max-chars', '100000',
          '--max-sections', '20',
          '--generated-at', '2026-06-19T00:00:00.000Z',
          '--output', outputPath,
          '--summary-json-out', summaryPath,
        ])

        const content = readFileSync(outputPath, 'utf-8')
        expect(content).toContain('chatgpt_context_bundle/v1')
      } finally {
        cleanupTempDir(tempDir)
      }
    })
  })
})
