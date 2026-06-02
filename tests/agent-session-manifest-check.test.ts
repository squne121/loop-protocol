/**
 * agent-session-manifest-check.test.ts
 *
 * Integration tests for check-agent-session-manifests.mjs (pnpm manifest:check entrypoint).
 * Tests actual CLI execution via execFileSync (spawn pattern).
 *
 * Covers AC3-AC12 from Issue #378.
 */

import { execFileSync } from 'child_process'
import { writeFileSync, mkdirSync, rmSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it, beforeAll, afterAll } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..')
const SCRIPTS_DIR = resolve(REPO_ROOT, 'scripts')
const TESTS_DIR = resolve(REPO_ROOT, 'tests')
const FIXTURES_DIR = resolve(TESTS_DIR, 'fixtures', 'agent-session-manifest')

// ============================================================================
// Helper: spawn manifest:check
// ============================================================================

function runManifestCheck(args: string[]): { stdout: string; stderr: string; exitCode: number } {
  try {
    const stdout = execFileSync(
      process.execPath,
      [resolve(SCRIPTS_DIR, 'check-agent-session-manifests.mjs'), ...args],
      {
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
        cwd: REPO_ROOT,
      }
    )
    return { stdout, stderr: '', exitCode: 0 }
  } catch (error) {
    const err = error as { stdout?: string; stderr?: string; status?: number }
    return {
      stdout: err.stdout || '',
      stderr: err.stderr || '',
      exitCode: err.status ?? 1,
    }
  }
}

// ============================================================================
// Temp fixture paths
// ============================================================================

const TEMP_FIXTURE_PATHS = [
  resolve(FIXTURES_DIR, 'temp-unsupported.txt'),
  resolve(FIXTURES_DIR, 'temp-invalid-no-markers.md'),
]

function cleanupTempFixtures(): void {
  for (const p of TEMP_FIXTURE_PATHS) {
    rmSync(p, { force: true })
  }
}

// ============================================================================
// Setup / Teardown
// ============================================================================

beforeAll(() => {
  mkdirSync(FIXTURES_DIR, { recursive: true })
  cleanupTempFixtures()
})

afterAll(() => {
  cleanupTempFixtures()
})

// ============================================================================
// Test Suite: manifest:check entrypoint
// ============================================================================

describe('manifest:check entrypoint', () => {

  // AC3: valid fixture passes
  it('GIVEN a valid JSON manifest fixture WHEN manifest:check valid fixture is run THEN exits 0', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'valid-basic.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('PASS')
  })

  // AC4: invalid schema rejected
  it('GIVEN a manifest fixture missing required redaction field WHEN manifest:check invalid schema is checked THEN exits 1', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'invalid-missing-redaction.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('redaction')
  })

  // AC5: comment marker roundtrip (extractor → validator)
  it('GIVEN a GitHub comment markdown fixture with markers WHEN manifest:check marker roundtrip extracts and validates THEN exits 0', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'valid-comment.md')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('PASS')
  })

  // AC6: public_github_comment + transcript rejected
  it('GIVEN a manifest with visibility public_github_comment and source_kind transcript WHEN manifest:check public_github_comment restriction is enforced THEN exits 1', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'invalid-public-transcript.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('transcript')
  })

  // AC7: redaction violation rejected
  it('GIVEN a manifest with redaction.raw_transcript_included true WHEN manifest:check redaction violation is detected THEN exits 1', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'invalid-redaction-violation.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('raw_transcript_included')
  })

  // AC8: leakage (secret/path) detected
  it('GIVEN a manifest with absolute path in producer.command WHEN manifest:check leakage is detected THEN exits 1', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'invalid-leakage.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('absolute path')
  })

  // AC9: token_usage availability mismatch (0 instead of null)
  it('GIVEN a manifest with token_usage availability unavailable and prompt=0 WHEN manifest:check token_usage availability rule is applied THEN exits 1', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'invalid-token-usage-zero.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('null')
  })

  // AC10: skipped vs pass conflict
  it('GIVEN a manifest with skipped_count > 0 and overall pass WHEN manifest:check skipped vs pass inconsistency is validated THEN exits 1', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'invalid-skipped-pass.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('skipped_count')
  })

  // AC11: empty target fails
  it('GIVEN an explicit target glob matching 0 files WHEN manifest:check empty target is given THEN exits 1', () => {
    const result = runManifestCheck(['tests/fixtures/nonexistent-glob-that-does-not-exist/*.json'])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('no files found')
  })

  // AC12: detailed error fields present
  it('GIVEN an invalid manifest WHEN manifest:check detailed error output is produced THEN stderr contains field, expected, and actual', () => {
    const fixturePath = resolve(FIXTURES_DIR, 'invalid-missing-redaction.json')
    const result = runManifestCheck([fixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('field:')
    expect(result.stderr).toContain('expected:')
    expect(result.stderr).toContain('actual:')
  })

  // Multiple-marker invalid comment fixture
  it('GIVEN a GitHub comment with malformed markers WHEN manifest:check fails on invalid comment marker THEN exits 1', () => {
    // Create a temp invalid comment fixture with no markers
    const tempFixturePath = resolve(FIXTURES_DIR, 'temp-invalid-no-markers.md')
    writeFileSync(tempFixturePath, '# No markers here\n\nJust some text without any manifest markers.\n')
    const result = runManifestCheck([tempFixturePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('marker')
  })

  // B1 regression: explicit target matching files but no supported manifest files
  it('GIVEN an explicit target that matches only .txt files WHEN manifest:check is run THEN exits 1 with no-supported-files message', () => {
    const tempTxtPath = resolve(FIXTURES_DIR, 'temp-unsupported.txt')
    writeFileSync(tempTxtPath, 'this is not a manifest file\n')
    const result = runManifestCheck([tempTxtPath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toMatch(/unsupported file type|no supported manifest files/)
  })

  // B1 regression: explicit target with mixed .json and unsupported (.txt) files
  it('GIVEN an explicit target containing both a valid .json and a .txt file WHEN manifest:check is run THEN exits 1 rejecting the unsupported file', () => {
    const tempTxtPath = resolve(FIXTURES_DIR, 'temp-unsupported.txt')
    writeFileSync(tempTxtPath, 'this is not a manifest file\n')
    const fixturePath = resolve(FIXTURES_DIR, 'valid-basic.json')
    const result = runManifestCheck([fixturePath, tempTxtPath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('unsupported file type')
  })

  // NB1: unknown option causes exit 2
  it('GIVEN an unknown CLI option --unknown-flag WHEN manifest:check is run THEN exits 2 with unknown option message', () => {
    const result = runManifestCheck(['--unknown-flag'])
    expect(result.exitCode).toBe(2)
    expect(result.stderr).toContain('unknown option')
  })

})
