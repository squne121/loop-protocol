/**
 * session-recording-smoke-verdict-check.test.ts
 *
 * JSON Schema Draft 2020-12: docs/schemas/session-recording-smoke-verdict.schema.json
 * and scripts/check-session-recording-smoke-verdict.mjs (pnpm smoke-verdict:check).
 */
import { readFileSync } from 'fs'
import { dirname, resolve } from 'path'
import { fileURLToPath } from 'url'
import { execFileSync } from 'child_process'
import { describe, expect, it } from 'vitest'

import { validateVerdict } from '../scripts/check-session-recording-smoke-verdict.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CHECK_SCRIPT = resolve(__dirname, '../scripts/check-session-recording-smoke-verdict.mjs')
const FIXTURES_DIR = resolve(__dirname, 'fixtures/session-recording-smoke-verdict')

function loadFixture(name: string) {
  return JSON.parse(readFileSync(resolve(FIXTURES_DIR, name), 'utf-8'))
}

function runCli(args: string[]) {
  try {
    const stdout = execFileSync(process.execPath, [CHECK_SCRIPT, ...args], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { exitCode: 0, stdout }
  } catch (err) {
    const e = err as { status?: number; stdout?: string; stderr?: string }
    return { exitCode: e.status ?? 1, stdout: e.stdout ?? '', stderr: e.stderr ?? '' }
  }
}

describe('session_recording_smoke_verdict/v1 schema', () => {
  it('GIVEN valid-basic fixture WHEN validated THEN passes schema validation (AC1)', async () => {
    const verdict = loadFixture('valid-basic.json')
    const result = await validateVerdict(verdict)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN valid-basic fixture WHEN checked THEN schema title is session_recording_smoke_verdict/v1 (AC1)', () => {
    const schema = JSON.parse(
      readFileSync(resolve(__dirname, '../docs/schemas/session-recording-smoke-verdict.schema.json'), 'utf-8'),
    )
    expect(schema.title).toBe('session_recording_smoke_verdict/v1')
    expect(schema.$schema).toBe('https://json-schema.org/draft/2020-12/schema')
    expect(schema.required).toEqual(
      expect.arrayContaining([
        'schema',
        'issue',
        'completion_state',
        'recommendation',
        'followup_issue',
        'legacy_evidence_excluded',
        'authoritative_count',
        'ac_results',
        'token_usage',
        'github_secret_scanning',
        'stop_conditions_triggered',
        'evidence_refs',
      ]),
    )
  })

  it('GIVEN valid-token-usage-unavailable fixture WHEN validated THEN passes (AC2 null semantics)', async () => {
    const verdict = loadFixture('valid-token-usage-unavailable.json')
    const result = await validateVerdict(verdict)
    expect(result.valid).toBe(true)
  })

  it('GIVEN invalid-token-usage-zero fixture (prompt_tokens: 0 while unavailable) WHEN validated THEN fails (AC2/AC9)', async () => {
    const verdict = loadFixture('invalid-token-usage-zero.json')
    const result = await validateVerdict(verdict)
    expect(result.valid).toBe(false)
    expect(result.errors.length).toBeGreaterThan(0)
  })

  it('GIVEN invalid-missing-followup-issue fixture WHEN validated THEN fails required-field check (AC1)', async () => {
    const verdict = loadFixture('invalid-missing-followup-issue.json')
    const result = await validateVerdict(verdict)
    expect(result.valid).toBe(false)
  })

  it('GIVEN invalid-secret-scanning-reason-mismatch fixture WHEN validated THEN fails (AC3)', async () => {
    const verdict = loadFixture('invalid-secret-scanning-reason-mismatch.json')
    const result = await validateVerdict(verdict)
    expect(result.valid).toBe(false)
  })

  it('GIVEN followup_issue as a string WHEN validated THEN passes (AC10)', async () => {
    const verdict = loadFixture('valid-token-usage-unavailable.json')
    expect(typeof verdict.followup_issue).toBe('string')
    const result = await validateVerdict(verdict)
    expect(result.valid).toBe(true)
  })
})

describe('scripts/check-session-recording-smoke-verdict.mjs CLI (AC7/AC8)', () => {
  it('GIVEN --help WHEN run THEN prints usage and exits 0', () => {
    const { exitCode, stdout } = runCli(['--help'])
    expect(exitCode).toBe(0)
    expect(stdout).toContain('Usage')
  })

  it('GIVEN valid-basic fixture path WHEN run THEN exits 0 (PASS)', () => {
    const { exitCode, stdout } = runCli([resolve(FIXTURES_DIR, 'valid-basic.json')])
    expect(exitCode).toBe(0)
    expect(stdout).toContain('PASS')
  })

  it('GIVEN invalid-token-usage-zero fixture path WHEN run THEN exits non-zero (FAIL, AC8/AC9)', () => {
    const { exitCode } = runCli([resolve(FIXTURES_DIR, 'invalid-token-usage-zero.json')])
    expect(exitCode).not.toBe(0)
  })

  it('GIVEN invalid-missing-followup-issue fixture path WHEN run THEN exits non-zero (AC8 negative control)', () => {
    const { exitCode } = runCli([resolve(FIXTURES_DIR, 'invalid-missing-followup-issue.json')])
    expect(exitCode).not.toBe(0)
  })

  it('GIVEN default target patterns WHEN run with no args in repo root THEN validates valid-*.json fixtures', () => {
    const { exitCode, stdout } = runCli([])
    expect(exitCode).toBe(0)
    expect(stdout).toContain('smoke-verdict:check:')
  })
})
