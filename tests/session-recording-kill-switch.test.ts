/**
 * session-recording-kill-switch.test.ts
 *
 * Tests for:
 *   - .claude/scripts/kill_switch_runtime_smoke.py (Kill Switch smoke test)
 *   - .claude/scripts/secret_exposure_scanner.py (Secret exposure scanner)
 *
 * Covers AC1–AC12 from Issue #380.
 * Strategy: invoke Python scripts via subprocess with fixture-based inputs.
 * Fixtures in tests/fixtures/session-recording/ provide scenario inputs.
 */

import { spawnSync } from 'child_process'
import { existsSync } from 'fs'
import { resolve, join } from 'path'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..')
const SMOKE_SCRIPT = resolve(REPO_ROOT, '.claude', 'scripts', 'kill_switch_runtime_smoke.py')
const SCANNER_SCRIPT = resolve(REPO_ROOT, '.claude', 'scripts', 'secret_exposure_scanner.py')
const FIXTURES_DIR = resolve(REPO_ROOT, 'tests', 'fixtures', 'session-recording')

interface RunResult {
  stdout: string
  stderr: string
  exitCode: number
}

function runScript(script: string, args: string[], envOverrides: Record<string, string> = {}): RunResult {
  const fullEnv: Record<string, string> = { ...(process.env as Record<string, string>) }
  Object.assign(fullEnv, envOverrides)

  const result = spawnSync('python3', [script, ...args], {
    encoding: 'utf-8',
    env: fullEnv,
    timeout: 60000,
  })

  return {
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    exitCode: result.status ?? 1,
  }
}

/**
 * Run the smoke test with a specific fixture directory or JSON file.
 */
function runSmoke(fixturesPath: string, envOverrides: Record<string, string> = {}): RunResult {
  return runScript(SMOKE_SCRIPT, ['--fixtures', fixturesPath], envOverrides)
}

/**
 * Run the scanner on a local path.
 */
function runScanner(localPath: string, failOnFinding = false): RunResult {
  const args = ['--local', localPath]
  if (failOnFinding) args.push('--fail-on-finding')
  return runScript(SCANNER_SCRIPT, args)
}

// ============================================================================
// AC1 & AC2: Script existence
// ============================================================================

describe('scripts existence (AC1, AC2)', () => {
  it('AC1: GIVEN the kill switch smoke script path WHEN checking file existence THEN the script file exists', () => {
    expect(existsSync(SMOKE_SCRIPT)).toBe(true)
  })

  it('AC2: GIVEN the secret exposure scanner script path WHEN checking file existence THEN the script file exists', () => {
    expect(existsSync(SCANNER_SCRIPT)).toBe(true)
  })
})

// ============================================================================
// AC3–AC6: Kill Switch trigger fixtures — dangerous conditions
// ============================================================================

describe('kill_switch_secrets_mode (AC3)', () => {
  it('GIVEN secrets_mode fixture WHEN smoke test runs THEN verifier exits non-zero and fixture PASS reported', () => {
    const fixtureFile = join(FIXTURES_DIR, 'dangerous', 'secrets_mode.json')
    expect(existsSync(fixtureFile)).toBe(true)

    // The smoke test PASSES when the fixture correctly triggers nonzero exit
    const result = runSmoke(FIXTURES_DIR)
    // secrets_mode fixture should show as PASS in summary (smoke test asserts nonzero)
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('secrets_mode')
  })
})

describe('kill_switch_checkpoint_token (AC4)', () => {
  it('GIVEN checkpoint_token fixture WHEN smoke test runs THEN verifier exits non-zero and smoke PASS reported', () => {
    const fixtureFile = join(FIXTURES_DIR, 'dangerous', 'checkpoint_token.json')
    expect(existsSync(fixtureFile)).toBe(true)

    const result = runSmoke(FIXTURES_DIR)
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('checkpoint_token')
  })
})

describe('kill_switch_public_branch (AC5)', () => {
  it('GIVEN public_branch fixture WHEN smoke test runs THEN verifier exits non-zero and smoke PASS reported', () => {
    const fixtureFile = join(FIXTURES_DIR, 'dangerous', 'public_branch.json')
    expect(existsSync(fixtureFile)).toBe(true)

    const result = runSmoke(FIXTURES_DIR)
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('public_branch')
  })
})

describe('kill_switch_raw_transcript (AC6)', () => {
  it('GIVEN raw_transcript fixture WHEN smoke test runs THEN verifier exits non-zero and smoke PASS reported', () => {
    const fixtureFile = join(FIXTURES_DIR, 'dangerous', 'raw_transcript.json')
    expect(existsSync(fixtureFile)).toBe(true)

    const result = runSmoke(FIXTURES_DIR)
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('raw_transcript')
  })
})

// ============================================================================
// AC7: required_end_state YAML output
// ============================================================================

describe('kill_switch_required_end_state (AC7)', () => {
  it('GIVEN any fixture WHEN smoke test runs THEN required_end_state YAML is output to stdout', () => {
    const result = runSmoke(FIXTURES_DIR)
    expect(result.stdout).toContain('required_end_state:')
    expect(result.stdout).toContain('session_recording_tool_enabled: false')
    expect(result.stdout).toContain('git_hooks_recording_enabled: false')
    expect(result.stdout).toContain('public_checkpoint_branch_present: false')
    expect(result.stdout).toContain('auto_push_sessions_allowed: false')
    expect(result.stdout).toContain('full_transcript_remote_visibility: none')
    expect(result.stdout).toContain('status: not_applicable')
    expect(result.stdout).toContain('reason: fake_fixture_only')
    expect(result.stdout).toContain('remediation_ticket_required: true')
  })
})

// ============================================================================
// AC8: transcript / EntireCLI pattern detection
// ============================================================================

describe('scanner_transcript (AC8)', () => {
  it('GIVEN a file containing source_kind: transcript WHEN scanner runs THEN finding is reported', () => {
    const transcriptFile = join(FIXTURES_DIR, 'invalid', 'with_transcript.txt')
    expect(existsSync(transcriptFile)).toBe(true)

    const result = runScanner(transcriptFile)
    const parsed = JSON.parse(result.stdout)
    expect(parsed.schema).toBe('SECRET_EXPOSURE_SCAN_RESULT_V1')
    expect(parsed.raw_value_included).toBe(false)
    expect(parsed.finding_count).toBeGreaterThan(0)

    const ruleIds = parsed.findings.map((f: { rule_id: string }) => f.rule_id)
    expect(ruleIds).toContain('transcript_source_kind')
  })
})

// ============================================================================
// AC9: GitHub token / Anthropic key detection
// ============================================================================

describe('scanner_token (AC9)', () => {
  it('GIVEN a file containing ghp_ fake token WHEN scanner runs THEN github_token_classic rule fires', () => {
    const tokenFile = join(FIXTURES_DIR, 'invalid', 'with_token.txt')
    expect(existsSync(tokenFile)).toBe(true)

    const result = runScanner(tokenFile)
    const parsed = JSON.parse(result.stdout)
    expect(parsed.raw_value_included).toBe(false)
    expect(parsed.finding_count).toBeGreaterThan(0)

    const ruleIds = parsed.findings.map((f: { rule_id: string }) => f.rule_id)
    // B6: rule was renamed to github_token_classic (length range updated for 2026 formats)
    expect(ruleIds).toContain('github_token_classic')
  })
})

// ============================================================================
// AC10: Absolute path detection
// ============================================================================

describe('scanner_path (AC10)', () => {
  it('GIVEN a file containing /home/user/secret/path WHEN scanner runs THEN absolute_path rule fires', () => {
    const pathFile = join(FIXTURES_DIR, 'invalid', 'with_path.txt')
    expect(existsSync(pathFile)).toBe(true)

    const result = runScanner(pathFile)
    const parsed = JSON.parse(result.stdout)
    expect(parsed.raw_value_included).toBe(false)
    expect(parsed.finding_count).toBeGreaterThan(0)

    const ruleIds = parsed.findings.map((f: { rule_id: string }) => f.rule_id)
    expect(ruleIds.some((id: string) => id.startsWith('absolute_path'))).toBe(true)
  })
})

// ============================================================================
// AC11: No raw value in output
// ============================================================================

describe('scanner_no_raw (AC11)', () => {
  it('GIVEN a file with secrets WHEN scanner runs THEN raw_value / matched_text / context_line are absent from JSON', () => {
    const tokenFile = join(FIXTURES_DIR, 'invalid', 'with_token.txt')
    const result = runScanner(tokenFile)
    const parsed = JSON.parse(result.stdout)

    expect(parsed.raw_value_included).toBe(false)

    // Verify no finding contains forbidden fields
    for (const finding of parsed.findings) {
      expect(Object.keys(finding)).not.toContain('raw_value')
      expect(Object.keys(finding)).not.toContain('matched_text')
      expect(Object.keys(finding)).not.toContain('context_line')
    }

    // Verify the fake sentinel token does not appear verbatim in output
    const sentinel = 'ghp_FAKETOKEN123456789012345678901234'
    expect(result.stdout).not.toContain(sentinel)
  })
})

// ============================================================================
// AC12: no-secret case PASS, secret-detected case FAIL
// ============================================================================

describe('scanner_fixture (AC12)', () => {
  it('GIVEN a clean file with no secrets WHEN scanner runs with --fail-on-finding THEN exits 0', () => {
    const cleanFile = join(FIXTURES_DIR, 'valid', 'clean.txt')
    expect(existsSync(cleanFile)).toBe(true)

    const result = runScanner(cleanFile, true)
    const parsed = JSON.parse(result.stdout)
    expect(parsed.finding_count).toBe(0)
    expect(result.exitCode).toBe(0)
  })

  it('GIVEN a file with detected secrets WHEN scanner runs with --fail-on-finding THEN exits non-zero', () => {
    const tokenFile = join(FIXTURES_DIR, 'invalid', 'with_token.txt')
    const result = runScanner(tokenFile, true)
    const parsed = JSON.parse(result.stdout)
    expect(parsed.finding_count).toBeGreaterThan(0)
    expect(result.exitCode).not.toBe(0)
  })
})
