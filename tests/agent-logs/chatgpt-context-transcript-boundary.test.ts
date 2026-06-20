import { describe, expect, it } from 'vitest'
import { writeFileSync } from 'fs'
import { resolve } from 'path'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'

import { loadSources } from '../../scripts/agent-logs/lib/chatgpt-context-source-loader.mjs'

function createTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'chatgpt-transcript-'))
}

function cleanupTempDir(dir: string) {
  rmSync(dir, { recursive: true, force: true })
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
})
