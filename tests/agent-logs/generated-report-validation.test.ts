import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { validateAgentRunReport } from '../../scripts/lib/agent-run-report-validation.mjs'
import { validateFinalReport } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import {
  cleanupTempDir,
  createDraftArgs,
  createFinalizeArgs,
  createTempDir,
  PACKAGE_JSON,
  readJson,
  runNodeScript,
  writeJson,
  FINALIZE_SCRIPT,
  START_SCRIPT,
} from './helpers'

const tempDirs: string[] = []
const REPORT_FIXTURES_DIR = resolve(__dirname, '..', 'fixtures', 'agent-run-report')

afterEach(() => {
  while (tempDirs.length > 0) {
    cleanupTempDir(tempDirs.pop() as string)
  }
})

describe('generated agent run report', () => {
  it('GIVEN finalize-agent-run output WHEN validated THEN report passes current validator', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)
    const finalize = runNodeScript(FINALIZE_SCRIPT, createFinalizeArgs(draftPath, reportPath))
    expect(finalize.exitCode).toBe(0)

    const report = readJson(reportPath)
    expect(Object.keys(report).sort()).toEqual([
      'actor',
      'authority',
      'commands_summary',
      'docs_read_refs',
      'evidence_refs',
      'manifest_refs',
      'public_safety',
      'public_surface_kind',
      'schema',
      'token_usage',
    ])
    expect(report.public_safety.observation_sources).toBeUndefined()

    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(true)
  })

  it('GIVEN a valid entirecli fixture WHEN passed through the current validator THEN schema-admission succeeds without producer changes', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-entirecli-safe.json'))
    const result = validateAgentRunReport(report)

    expect(result.valid).toBe(true)
  })

  it('GIVEN a blocked public entirecli fixture WHEN passed through the current validator THEN schema-admission fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-entirecli-blocked.json'))
    const result = validateAgentRunReport(report)

    expect(result.valid).toBe(false)
  })

  it('GIVEN a bad-schema-version public entirecli fixture WHEN passed through the current validator THEN schema-admission fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-entirecli-bad-schema-version.json'))
    const result = validateAgentRunReport(report)

    expect(result.valid).toBe(false)
  })

  it('GIVEN a raw-values public entirecli fixture WHEN passed through the current validator THEN schema-admission fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-entirecli-raw-values.json'))
    const result = validateAgentRunReport(report)

    expect(result.valid).toBe(false)
  })

  it('GIVEN a generated report without observation_sources WHEN passed through the current validator THEN C0 optional admission keeps it valid', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-basic.json'))
    expect(report.public_safety.observation_sources).toBeUndefined()

    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(true)
  })

  it('GIVEN a generated report without token_usage source extensions WHEN passed through the current validator THEN C0 token source admission keeps existing none semantics valid', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-basic.json'))

    expect(report.token_usage.source).toBe('none')
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(true)
  })

  it('GIVEN package scripts WHEN inspected THEN agent-run lifecycle aliases are present without duplicating the validator command', () => {
    const packageJson = JSON.parse(readFileSync(PACKAGE_JSON, 'utf-8'))
    expect(packageJson.scripts['agent-run:start']).toBe('node scripts/agent-logs/start-agent-run.mjs')
    expect(packageJson.scripts['agent-run:finalize']).toBe('node scripts/agent-logs/finalize-agent-run.mjs')
    expect(packageJson.scripts['agent-run:check']).toBe(packageJson.scripts['agent-run-report:check'])
  })

  it('GIVEN the same draft and checked-at WHEN finalized twice THEN generated reports are deterministic', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPathA = resolve(tempDir, 'report-a.json')
    const reportPathB = resolve(tempDir, 'report-b.json')

    expect(runNodeScript(START_SCRIPT, createDraftArgs(draftPath)).exitCode).toBe(0)
    expect(runNodeScript(FINALIZE_SCRIPT, createFinalizeArgs(draftPath, reportPathA)).exitCode).toBe(0)
    expect(runNodeScript(FINALIZE_SCRIPT, createFinalizeArgs(draftPath, reportPathB)).exitCode).toBe(0)

    expect(readFileSync(reportPathA, 'utf-8')).toBe(readFileSync(reportPathB, 'utf-8'))
  })

  it('GIVEN a public surface report with missing entirecli_safety WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-basic.json'))
    // valid-basic.json has public_surface_kind: github_issue_comment but no entirecli_safety
    expect(() => validateFinalReport(report)).toThrow(/entirecli_safety/)
  })

  it('GIVEN a report with entirecli_safety not_applicable WHEN validateFinalReport is called THEN it passes', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-entirecli-not-applicable.json'))
    expect(() => validateFinalReport(report)).not.toThrow()
  })

  it('GIVEN a draft with extra keys WHEN finalized THEN draft validation fails closed', () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const draftPath = resolve(tempDir, 'draft.json')
    const reportPath = resolve(tempDir, 'report.json')

    writeJson(draftPath, {
      schema: 'agent_run_draft/v1',
      run_id: 'run-936-001',
      target: { kind: 'issue', id: 936 },
      phase: 'implementation',
      actor: { type: 'ai_agent', name: 'Codex worker' },
      started_at: '2026-06-17T12:00:00.000Z',
      extra_field: 'unexpected',
    })

    const result = runNodeScript(FINALIZE_SCRIPT, createFinalizeArgs(draftPath, reportPath))
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('draft.invalid')
  })
})
