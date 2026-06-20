import { describe, expect, it } from 'vitest'
import { writeFileSync } from 'fs'
import { resolve } from 'path'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { execFileSync } from 'child_process'

import { loadSources } from '../../scripts/agent-logs/lib/chatgpt-context-source-loader.mjs'

// Resolve from this test file's location — works both in worktree and main tree
const TESTS_DIR = resolve(__dirname)
const SCRIPTS_ROOT = resolve(TESTS_DIR, '..', '..', 'scripts')
const EXPORT_SCRIPT = resolve(SCRIPTS_ROOT, 'agent-logs', 'export-chatgpt-context.mjs')

function createTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'chatgpt-transcript-'))
}

function cleanupTempDir(dir: string) {
  rmSync(dir, { recursive: true, force: true })
}

function runExportScript(args: string[]) {
  try {
    const stdout = execFileSync(process.execPath, [EXPORT_SCRIPT, ...args], {
      cwd: SCRIPTS_ROOT,
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

describe('chatgpt-context transcript boundary (AC7)', () => {
  it('GIVEN run report with raw_transcript WHEN loading sources THEN throws forbidden field error', async () => {
    const tempDir = createTempDir()
    try {
      const contaminated = {
        schema: 'agent_run_report/v1',
        run_id: 'run-001',
        raw_transcript: 'this is the full session transcript...',
        public_safety: { redaction_status: 'clean' },
      }
      const reportPath = resolve(tempDir, 'report.json')
      writeFileSync(reportPath, JSON.stringify(contaminated))

      const minimalJson = resolve(tempDir, 'minimal.json')
      writeFileSync(minimalJson, JSON.stringify({ number: 939, title: 'test' }))

      await expect(loadSources({
        parentIssueJson: minimalJson,
        targetIssueJson: minimalJson,
        retroIndexJson: minimalJson,
        sourceSetJson: minimalJson,
        runReportJson: [reportPath],
        evidenceRefJson: [],
      })).rejects.toMatchObject({ code: 'source.forbidden_field' })
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN run report with transcript_excerpt WHEN loading sources THEN throws forbidden field error', async () => {
    const tempDir = createTempDir()
    try {
      const contaminated = {
        schema: 'agent_run_report/v1',
        run_id: 'run-001',
        transcript_excerpt: 'partial transcript content',
        public_safety: { redaction_status: 'clean' },
      }
      const reportPath = resolve(tempDir, 'report.json')
      writeFileSync(reportPath, JSON.stringify(contaminated))

      const minimalJson = resolve(tempDir, 'minimal.json')
      writeFileSync(minimalJson, JSON.stringify({ number: 939, title: 'test' }))

      await expect(loadSources({
        parentIssueJson: minimalJson,
        targetIssueJson: minimalJson,
        retroIndexJson: minimalJson,
        sourceSetJson: minimalJson,
        runReportJson: [reportPath],
        evidenceRefJson: [],
      })).rejects.toMatchObject({ code: 'source.forbidden_field' })
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN clean run report without transcript WHEN loading THEN succeeds', async () => {
    const tempDir = createTempDir()
    try {
      const clean = {
        schema: 'agent_run_report/v1',
        run_id: 'run-001',
        transcript_hotspot_summary: 'public-safe aggregate only',
        evidence_digest: 'sha256:' + 'a'.repeat(64),
        public_safety: { redaction_status: 'clean' },
        actor: { type: 'ai_agent' },
        commands_summary: [],
      }
      const reportPath = resolve(tempDir, 'report.json')
      writeFileSync(reportPath, JSON.stringify(clean))

      const minimalJson = resolve(tempDir, 'minimal.json')
      writeFileSync(minimalJson, JSON.stringify({ number: 939, title: 'test' }))

      const { sources } = await loadSources({
        parentIssueJson: minimalJson,
        targetIssueJson: minimalJson,
        retroIndexJson: minimalJson,
        sourceSetJson: minimalJson,
        runReportJson: [reportPath],
        evidenceRefJson: [],
      })

      expect(sources.run_reports).toHaveLength(1)
      // transcript_hotspot_summary is allowed (not in forbidden fields)
      expect(sources.run_reports[0].transcript_hotspot_summary).toBe('public-safe aggregate only')
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN run report with full_command_output WHEN loading THEN throws forbidden field error', async () => {
    const tempDir = createTempDir()
    try {
      const contaminated = {
        schema: 'agent_run_report/v1',
        run_id: 'run-001',
        full_command_output: 'npm install\n...\n200 packages added',
        public_safety: { redaction_status: 'clean' },
      }
      const reportPath = resolve(tempDir, 'report.json')
      writeFileSync(reportPath, JSON.stringify(contaminated))

      const minimalJson = resolve(tempDir, 'minimal.json')
      writeFileSync(minimalJson, JSON.stringify({ number: 939, title: 'test' }))

      await expect(loadSources({
        parentIssueJson: minimalJson,
        targetIssueJson: minimalJson,
        retroIndexJson: minimalJson,
        sourceSetJson: minimalJson,
        runReportJson: [reportPath],
        evidenceRefJson: [],
      })).rejects.toMatchObject({ code: 'source.forbidden_field' })
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  // Blocker 7: evidence_digest validation
  it('GIVEN run report with transcript_hotspot_summary but missing evidence_digest WHEN exporting THEN fails with ac7 error', () => {
    const tempDir = createTempDir()
    try {
      const reportWithHotspot = {
        schema: 'agent_run_report/v1',
        run_id: 'run-001',
        transcript_hotspot_summary: 'some hotspot data',
        // evidence_digest intentionally missing
        public_safety: { redaction_status: 'clean' },
        actor: { type: 'ai_agent' },
        commands_summary: [],
      }
      const reportPath = resolve(tempDir, 'report.json')
      const minimalJson = resolve(tempDir, 'minimal.json')
      const outputPath = resolve(tempDir, 'bundle.md')
      const summaryPath = resolve(tempDir, 'summary.json')

      writeFileSync(reportPath, JSON.stringify(reportWithHotspot))
      writeFileSync(minimalJson, JSON.stringify({ number: 939, title: 'test' }))

      const result = runExportScript([
        '--parent-issue-json', minimalJson,
        '--target-issue-json', minimalJson,
        '--retro-index-json', minimalJson,
        '--source-set-json', minimalJson,
        '--run-report-json', reportPath,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', outputPath,
        '--summary-json-out', summaryPath,
      ])

      expect(result.exitCode).not.toBe(0)
      expect(result.stderr).toContain('ac7.transcript_hotspot_missing_evidence_digest')
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN run report with transcript_hotspot_summary and malformed evidence_digest WHEN exporting THEN fails with ac7 error', () => {
    const tempDir = createTempDir()
    try {
      const reportWithHotspot = {
        schema: 'agent_run_report/v1',
        run_id: 'run-001',
        transcript_hotspot_summary: 'some hotspot data',
        evidence_digest: 'not-a-valid-digest',
        public_safety: { redaction_status: 'clean' },
        actor: { type: 'ai_agent' },
        commands_summary: [],
      }
      const reportPath = resolve(tempDir, 'report.json')
      const minimalJson = resolve(tempDir, 'minimal.json')
      const outputPath = resolve(tempDir, 'bundle.md')
      const summaryPath = resolve(tempDir, 'summary.json')

      writeFileSync(reportPath, JSON.stringify(reportWithHotspot))
      writeFileSync(minimalJson, JSON.stringify({ number: 939, title: 'test' }))

      const result = runExportScript([
        '--parent-issue-json', minimalJson,
        '--target-issue-json', minimalJson,
        '--retro-index-json', minimalJson,
        '--source-set-json', minimalJson,
        '--run-report-json', reportPath,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', outputPath,
        '--summary-json-out', summaryPath,
      ])

      expect(result.exitCode).not.toBe(0)
      expect(result.stderr).toContain('ac7.transcript_hotspot_invalid_evidence_digest')
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN run report with transcript_hotspot_summary and valid evidence_digest WHEN exporting THEN succeeds', () => {
    const tempDir = createTempDir()
    try {
      const reportWithHotspot = {
        schema: 'agent_run_report/v1',
        run_id: 'run-001',
        transcript_hotspot_summary: 'some hotspot data',
        evidence_digest: 'sha256:' + 'a'.repeat(64),
        public_safety: { redaction_status: 'clean' },
        actor: { type: 'ai_agent' },
        commands_summary: [],
      }
      const reportPath = resolve(tempDir, 'report.json')
      const minimalJson = resolve(tempDir, 'minimal.json')
      const outputPath = resolve(tempDir, 'bundle.md')
      const summaryPath = resolve(tempDir, 'summary.json')

      writeFileSync(reportPath, JSON.stringify(reportWithHotspot))
      writeFileSync(minimalJson, JSON.stringify({ number: 939, title: 'test' }))

      const result = runExportScript([
        '--parent-issue-json', minimalJson,
        '--target-issue-json', minimalJson,
        '--retro-index-json', minimalJson,
        '--source-set-json', minimalJson,
        '--run-report-json', reportPath,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-06-19T00:00:00.000Z',
        '--output', outputPath,
        '--summary-json-out', summaryPath,
      ])

      expect(result.exitCode).toBe(0)
    } finally {
      cleanupTempDir(tempDir)
    }
  })
})
