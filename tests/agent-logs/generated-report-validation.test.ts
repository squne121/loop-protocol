import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { validateAgentRunReport } from '../../scripts/lib/agent-run-report-validation.mjs'
import { validateFinalReport } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import { computeObservationSourceProjectionDigest } from '../../scripts/agent-logs/lib/observation-source-adapter.mjs'
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

function refreshObservationSourceDigest(source: Record<string, unknown> & {
  provenance: {
    ref: {
      digest: string,
    },
    source_projection_digest: string,
  },
}) {
  const digest = computeObservationSourceProjectionDigest(source)
  source.provenance.ref.digest = digest
  source.provenance.source_projection_digest = digest
  return source
}

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

  it('GIVEN a generated report with observation source from existing producer fixture WHEN validateFinalReport is called THEN it passes', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-observation-source-supported-available.json'))

    expect(() => validateFinalReport(report)).not.toThrow()
  })

  it('GIVEN a generated report with public_surface and missing observation_sources WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-basic.json'))
    expect(report.public_safety.observation_sources).toBeUndefined()

    expect(() => validateFinalReport(report)).toThrow(/require public_safety\.observation_sources/)
  })

  it('GIVEN a generated report with public_surface and empty observation_sources WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-observation-source-empty.json'))
    expect(report.public_safety.observation_sources.length).toBe(0)

    expect(() => validateFinalReport(report)).toThrow(/must not be empty/)
  })

  it('GIVEN a generated report with duplicate observation source_kind WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-observation-source-supported-available.json'))
    report.public_safety.observation_sources = [
      refreshObservationSourceDigest(report.public_safety.observation_sources[0]),
      refreshObservationSourceDigest({
        ...report.public_safety.observation_sources[0],
        provenance: structuredClone(report.public_safety.observation_sources[0].provenance),
      }),
    ]

    expect(() => validateFinalReport(report)).toThrow(/duplicate public safety source_kind/)
  })

  it('GIVEN a generated report with duplicate observation source_projection_digest WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-observation-source-supported-available.json'))
    report.public_safety.observation_sources = [
      refreshObservationSourceDigest(report.public_safety.observation_sources[0]),
      {
        ...refreshObservationSourceDigest({
          ...report.public_safety.observation_sources[0],
          source_kind: 'google_antigravity',
          provenance: structuredClone(report.public_safety.observation_sources[0].provenance),
        }),
        provenance: {
          ...structuredClone(report.public_safety.observation_sources[0].provenance),
          ref: {
            ...structuredClone(report.public_safety.observation_sources[0].provenance.ref),
            digest: report.public_safety.observation_sources[0].provenance.ref.digest,
          },
          source_projection_digest: report.public_safety.observation_sources[0].provenance.source_projection_digest,
        },
      },
    ]

    expect(() => validateFinalReport(report)).toThrow(/duplicate source_projection_digest/)
  })

  it('GIVEN a generated report with mismatched observation source canonical digest WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-observation-source-bad-digest.json'))

    expect(() => validateFinalReport(report)).toThrow(/canonical public projection digest/)
  })

  it('GIVEN a generated report with non-observation ref kind WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-observation-source-non-observation-ref-kind.json'))

    expect(() => validateFinalReport(report)).toThrow(/observation_projection_digest/)
  })

  it('GIVEN a generated report with real_pilot_verified observation source evidence WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-observation-source-supported-available.json'))
    report.public_safety.observation_sources[0].provenance.evidence_mode = 'real_pilot_verified'

    expect(() => validateFinalReport(report)).toThrow(/synthetic_only/)
  })

  it('GIVEN a generated report with unavailable observation source metrics non-null WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-observation-source-unsupported-unavailable.json'))
    report.public_safety.observation_sources[0] = refreshObservationSourceDigest({
      ...report.public_safety.observation_sources[0],
      metrics: {
        trace_count: 0,
        span_count: null,
        prompt_tokens: null,
        completion_tokens: null,
        total_tokens: null,
      },
      provenance: structuredClone(report.public_safety.observation_sources[0].provenance),
    })

    expect(() => validateFinalReport(report)).toThrow(/must be null when availability is unavailable/)
  })

  it('GIVEN a generated report with raw_values_emitted true in observation source safety WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-observation-source-supported-available.json'))
    report.public_safety.observation_sources[0] = refreshObservationSourceDigest({
      ...report.public_safety.observation_sources[0],
      safety: {
        ...report.public_safety.observation_sources[0].safety,
        raw_values_emitted: true,
      },
      provenance: structuredClone(report.public_safety.observation_sources[0].provenance),
    })

    expect(() => validateFinalReport(report)).toThrow(/raw_values_emitted must be false/)
  })

  it('GIVEN a generated report with regex-valid but unknown observation reason code WHEN validateFinalReport is called THEN it fails closed with a reason-code error', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-observation-source-reason-code-promptlike.json'))

    expect(() => validateFinalReport(report)).toThrow(/allowed observation source reason code/i)
  })

  it('GIVEN a generated report with unavailable observation source missing source_unavailable WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-observation-source-reason-code-unavailable-missing.json'))

    expect(() => validateFinalReport(report)).toThrow(/must include source_unavailable/i)
  })

  it('GIVEN a generated report with partial available observation source missing partial_projection WHEN validateFinalReport is called THEN it fails closed', () => {
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'invalid-public-observation-source-partial-available-missing-reason.json'))

    expect(() => validateFinalReport(report)).toThrow(/must include partial_projection/i)
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
    const report = readJson(resolve(REPORT_FIXTURES_DIR, 'valid-public-observation-source-supported-available.json'))
    delete report.public_safety.entirecli_safety
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
