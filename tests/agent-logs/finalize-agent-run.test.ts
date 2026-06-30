import { spawnSync } from 'child_process'
import { mkdtempSync, writeFileSync, existsSync, readFileSync, rmSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import { describe, expect, it } from 'vitest'

import { validateFinalReport } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import { REPO_ROOT } from '../agent-run-report-test-helpers'

const FINALIZE_SCRIPT = join(REPO_ROOT, 'scripts', 'agent-logs', 'finalize-agent-run.mjs')

const VALID_DRAFT = {
  schema: 'agent_run_draft/v1',
  run_id: 'test-finalize-run-001',
  target: { kind: 'issue', id: 1181 },
  phase: 'test-phase',
  actor: { type: 'ai_agent', name: 'TestAgent' },
  started_at: '2026-06-27T00:00:00.000Z',
}

const VALID_COMMAND_SUMMARY = JSON.stringify({
  command_label: 'pnpm test',
  exit_code: 0,
  verdict: 'pass',
  summary: 'tests passed in finalize integration test',
  artifact_ref: null,
})

const VALID_ENTIRECLI_SAFETY = {
  schema_version: 'entirecli_safety_result/v1',
  verdict: 'not_applicable',
  reason_codes: ['entire_absent'],
  raw_values_emitted: false,
  checked_surfaces: {
    entire_binary: false,
    entire_version: null,
    entire_enable_help: false,
    entire_configure_help: false,
  },
}

const VALID_OBSERVATION_SOURCE = {
  schema_version: 'observation_source_input/v1',
  input_kind: 'entirecli',
  output_source_kind: 'codex_cli',
  capability_verdict: 'supported',
  availability: 'available',
  projection_mode: 'allowlist_projection',
  checked_at: '2026-06-18T12:00:00.000Z',
  safety: {
    verdict: 'pass',
    raw_values_emitted: false,
    reason_codes: [],
  },
  metrics: {
    trace_count: 1,
    span_count: 2,
    prompt_tokens: 5,
    completion_tokens: 7,
    total_tokens: 12,
  },
}

const INVALID_OBSERVATION_SOURCE = {
  ...VALID_OBSERVATION_SOURCE,
  raw_prompt: 'should fail closed',
}

function runFinalize(args: string[]) {
  return spawnSync('node', [FINALIZE_SCRIPT, ...args], { encoding: 'utf-8' })
}

describe('finalize-agent-run CLI -- entirecli_safety integration', () => {
  it('GIVEN check-entirecli-safety output via --entirecli-safety-file AND --public-surface-kind github_issue_comment THEN report.public_safety.entirecli_safety exists and validateFinalReport passes', () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'finalize-test-'))
    try {
      const draftPath = join(tempDir, 'draft.json')
      const safetyPath = join(tempDir, 'safety.json')
      const outputPath = join(tempDir, 'report.json')
      writeFileSync(draftPath, JSON.stringify(VALID_DRAFT), 'utf-8')
      writeFileSync(safetyPath, JSON.stringify(VALID_ENTIRECLI_SAFETY), 'utf-8')
      const observationSourcePath = join(tempDir, 'observation-source.json')
      writeFileSync(observationSourcePath, JSON.stringify(VALID_OBSERVATION_SOURCE), 'utf-8')

      const result = runFinalize([
        '--draft', draftPath,
        '--output', outputPath,
        '--public-surface-kind', 'github_issue_comment',
        '--entirecli-safety-file', safetyPath,
        '--observation-source-file', observationSourcePath,
        '--command-summary-json', VALID_COMMAND_SUMMARY,
      ])

      expect(result.status).toBe(0)
      expect(existsSync(outputPath)).toBe(true)

      const report = JSON.parse(readFileSync(outputPath, 'utf-8'))
      expect(report.public_safety.entirecli_safety).toBeTruthy()
      expect(report.public_safety.entirecli_safety.verdict).toBe('not_applicable')
      expect(report.public_safety.entirecli_safety.schema_version).toBe('entirecli_safety_result/v1')
      expect(report.public_safety.entirecli_safety.raw_values_emitted).toBe(false)

      // validateFinalReport must also pass on the produced report
      expect(() => validateFinalReport(report)).not.toThrow()
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN --public-surface-kind github_issue_comment AND no --entirecli-safety-* THEN finalize exits 1 AND output report is not written', () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'finalize-test-'))
    try {
      const draftPath = join(tempDir, 'draft.json')
      const outputPath = join(tempDir, 'report.json')
      writeFileSync(draftPath, JSON.stringify(VALID_DRAFT), 'utf-8')

      const result = runFinalize([
        '--draft', draftPath,
        '--output', outputPath,
        '--public-surface-kind', 'github_issue_comment',
        '--command-summary-json', VALID_COMMAND_SUMMARY,
      ])

      expect(result.status).toBe(1)
      expect(existsSync(outputPath)).toBe(false)
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN malformed --entirecli-safety-json THEN finalize exits 1 AND output report is not written', () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'finalize-test-'))
    try {
      const draftPath = join(tempDir, 'draft.json')
      const outputPath = join(tempDir, 'report.json')
      writeFileSync(draftPath, JSON.stringify(VALID_DRAFT), 'utf-8')

      const result = runFinalize([
        '--draft', draftPath,
        '--output', outputPath,
        '--public-surface-kind', 'github_issue_comment',
        '--entirecli-safety-json', '{not valid json',
        '--command-summary-json', VALID_COMMAND_SUMMARY,
      ])

      expect(result.status).toBe(1)
      expect(existsSync(outputPath)).toBe(false)
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN --public-surface-kind github_issue_comment AND --observation-source-* missing THEN finalize exits 1 AND output report is not written', () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'finalize-test-'))
    try {
      const draftPath = join(tempDir, 'draft.json')
      const safetyPath = join(tempDir, 'safety.json')
      const outputPath = join(tempDir, 'report.json')
      writeFileSync(draftPath, JSON.stringify(VALID_DRAFT), 'utf-8')
      writeFileSync(safetyPath, JSON.stringify(VALID_ENTIRECLI_SAFETY), 'utf-8')

      const result = runFinalize([
        '--draft', draftPath,
        '--output', outputPath,
        '--public-surface-kind', 'github_issue_comment',
        '--entirecli-safety-file', safetyPath,
        '--command-summary-json', VALID_COMMAND_SUMMARY,
      ])

      expect(result.status).toBe(1)
      expect(existsSync(outputPath)).toBe(false)
      expect(result.stderr).toContain('observation_source_required')
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN --observation-source-json with forbidden field THEN finalize exits 1 AND output report is not written', () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'finalize-test-'))
    try {
      const draftPath = join(tempDir, 'draft.json')
      const safetyPath = join(tempDir, 'safety.json')
      const outputPath = join(tempDir, 'report.json')
      const obsPath = join(tempDir, 'observation-source.json')
      writeFileSync(draftPath, JSON.stringify(VALID_DRAFT), 'utf-8')
      writeFileSync(safetyPath, JSON.stringify(VALID_ENTIRECLI_SAFETY), 'utf-8')
      writeFileSync(obsPath, JSON.stringify(INVALID_OBSERVATION_SOURCE), 'utf-8')

      const result = runFinalize([
        '--draft', draftPath,
        '--output', outputPath,
        '--public-surface-kind', 'github_issue_comment',
        '--entirecli-safety-file', safetyPath,
        '--observation-source-file', obsPath,
        '--command-summary-json', VALID_COMMAND_SUMMARY,
      ])

      expect(result.status).toBe(1)
      expect(existsSync(outputPath)).toBe(false)
      expect(result.stderr).toContain('forbidden_fields')
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN --observation-source-json AND --observation-source-file both set THEN finalize exits 1 AND output report is not written', () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'finalize-test-'))
    try {
      const draftPath = join(tempDir, 'draft.json')
      const safetyPath = join(tempDir, 'safety.json')
      const observationSourcePath = join(tempDir, 'observation-source.json')
      const outputPath = join(tempDir, 'report.json')
      writeFileSync(draftPath, JSON.stringify(VALID_DRAFT), 'utf-8')
      writeFileSync(safetyPath, JSON.stringify(VALID_ENTIRECLI_SAFETY), 'utf-8')
      writeFileSync(observationSourcePath, JSON.stringify(VALID_OBSERVATION_SOURCE), 'utf-8')

      const result = runFinalize([
        '--draft', draftPath,
        '--output', outputPath,
        '--public-surface-kind', 'github_issue_comment',
        '--entirecli-safety-file', safetyPath,
        '--observation-source-json', JSON.stringify(VALID_OBSERVATION_SOURCE),
        '--observation-source-file', observationSourcePath,
        '--command-summary-json', VALID_COMMAND_SUMMARY,
      ])

      expect(result.status).toBe(1)
      expect(existsSync(outputPath)).toBe(false)
      expect(result.stderr).toContain('observation_source_conflict')
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })
})
