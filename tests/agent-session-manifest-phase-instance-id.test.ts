/**
 * agent-session-manifest-phase-instance-id.test.ts
 *
 * Tests for the extended `phase_instance_id` pattern in agent_session_manifest/v1 schema.
 * AC1: ci:session-manifest:12345678:1 is valid
 * AC2: issue-432:impl:001 is still valid (backward compat)
 * AC3: malformed CI IDs are rejected
 * AC4: generator CLI accepts ci:session-manifest:12345678:1 with --validate
 * AC5: generator help/error text lists both formats
 * AC8: phase_instance_id positive/negative cases in TypeScript tests
 */
import { execFileSync } from 'child_process'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'

import { validateManifestAgainstSchema } from '../scripts/lib/agent-session-manifest-validation.mjs'

const REPO_ROOT = resolve(__dirname, '..')
const SCRIPTS_DIR = resolve(REPO_ROOT, 'scripts')

function runProducerCLI(args: string[]): { stdout: string; stderr: string; exitCode: number } {
  try {
    const stdout = execFileSync(process.execPath, [resolve(SCRIPTS_DIR, 'generate-session-manifest.mjs'), ...args], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { stdout, stderr: '', exitCode: 0 }
  } catch (error) {
    const err = error as { stdout?: string; stderr?: string; status?: number }
    return { stdout: err.stdout || '', stderr: err.stderr || '', exitCode: err.status ?? 1 }
  }
}

const BASE_PRODUCER_ARGS = [
  '--repository', 'squne121/loop-protocol',
  '--phase-main-loop', 'impl',
  '--phase-ledger-phase', 'post_commit_verification',
  '--actor-type', 'github_action',
  '--actor-name', 'session-manifest-workflow',
  '--evidence-source-kind', 'ci_check',
  '--evidence-source-ref', 'https://github.com/squne121/loop-protocol/actions/runs/12345678',
  '--evidence-visibility', 'private_artifact',
  '--format', 'json',
  '--validate',
]

function createBaseManifest(phaseInstanceId: string) {
  return {
    schema: 'agent_session_manifest/v1',
    manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
    recorded_at: '2026-05-31T10:00:00Z',
    repository: 'squne121/loop-protocol',
    actor: {
      type: 'github_action',
      name: 'session-manifest-workflow',
      session_id: null,
    },
    phase: {
      main_loop: 'impl',
      ledger_phase: 'post_commit_verification',
      phase_instance_id: phaseInstanceId,
    },
    token_usage: {
      availability: 'unavailable',
      source: 'none',
      prompt: null,
      completion: null,
      total: null,
    },
    evidence: [
      {
        source_kind: 'ci_check',
        source_ref: 'https://github.com/squne121/loop-protocol/actions/runs/12345678',
        source_sha256: null,
        visibility: 'private_artifact',
      },
    ],
    redaction: {
      raw_transcript_included: false,
      local_paths_included: false,
      secret_scan_status: 'clean',
    },
  }
}

describe('phase_instance_id — CI-native format (AC1, AC2, AC3, AC8)', () => {
  // AC1: ci:session-manifest:12345678:1 is accepted
  it('GIVEN CI-native phase_instance_id ci:session-manifest:12345678:1 WHEN validating THEN manifest is accepted', () => {
    const manifest = createBaseManifest('ci:session-manifest:12345678:1')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  // AC2: existing issue-432:impl:001 format remains valid
  it('GIVEN legacy phase_instance_id issue-432:impl:001 WHEN validating THEN manifest is accepted (backward compat)', () => {
    const manifest = createBaseManifest('issue-432:impl:001')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  // AC8 positive — additional CI format variants
  it('GIVEN CI phase_instance_id with large run_id ci:session-manifest:9999999999:2 WHEN validating THEN manifest is accepted', () => {
    const manifest = createBaseManifest('ci:session-manifest:9999999999:2')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN CI phase_instance_id with slug containing dots and hyphens ci:my-workflow.v2:12345678:1 WHEN validating THEN manifest is accepted', () => {
    const manifest = createBaseManifest('ci:my-workflow.v2:12345678:1')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  // AC3: malformed CI IDs are rejected

  // empty slug (space in slug)
  it('GIVEN CI phase_instance_id with space in slug ci:session manifest:12345678:1 WHEN validating THEN manifest is rejected', () => {
    const manifest = createBaseManifest('ci:session manifest:12345678:1')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.path.includes('phase_instance_id') || e.path.includes('/phase'))).toBe(true)
  })

  // run_id = 0 (rejected — must be >= 1)
  it('GIVEN CI phase_instance_id with run_id=0 ci:session-manifest:0:1 WHEN validating THEN manifest is rejected', () => {
    const manifest = createBaseManifest('ci:session-manifest:0:1')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.path.includes('phase_instance_id') || e.path.includes('/phase'))).toBe(true)
  })

  // run_attempt = 0 (rejected — must be >= 1)
  it('GIVEN CI phase_instance_id with run_attempt=0 ci:session-manifest:12345678:0 WHEN validating THEN manifest is rejected', () => {
    const manifest = createBaseManifest('ci:session-manifest:12345678:0')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.path.includes('phase_instance_id') || e.path.includes('/phase'))).toBe(true)
  })

  // run_attempt missing (ci:<slug>:<run_id> without run_attempt)
  it('GIVEN CI phase_instance_id with missing run_attempt ci:session-manifest:12345678 WHEN validating THEN manifest is rejected', () => {
    const manifest = createBaseManifest('ci:session-manifest:12345678')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.path.includes('phase_instance_id') || e.path.includes('/phase'))).toBe(true)
  })

  // empty slug (ci: with empty producer_slug)
  it('GIVEN CI phase_instance_id with empty slug ci::12345678:1 WHEN validating THEN manifest is rejected', () => {
    const manifest = createBaseManifest('ci::12345678:1')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.path.includes('phase_instance_id') || e.path.includes('/phase'))).toBe(true)
  })

  // uppercase in slug (must be lowercase)
  it('GIVEN CI phase_instance_id with uppercase in slug ci:Session-Manifest:12345678:1 WHEN validating THEN manifest is rejected', () => {
    const manifest = createBaseManifest('ci:Session-Manifest:12345678:1')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.path.includes('phase_instance_id') || e.path.includes('/phase'))).toBe(true)
  })

  // completely malformed
  it('GIVEN completely malformed phase_instance_id foobar WHEN validating THEN manifest is rejected', () => {
    const manifest = createBaseManifest('foobar')
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.path.includes('phase_instance_id') || e.path.includes('/phase'))).toBe(true)
  })
})

describe('phase_instance_id — generator CLI integration (AC4, AC5)', () => {
  // AC4: generator CLI accepts ci:session-manifest:12345678:1 with --validate
  it('GIVEN CI-native phase_instance_id ci:session-manifest:12345678:1 WHEN generator --validate THEN exits 0 with valid manifest', () => {
    const result = runProducerCLI([...BASE_PRODUCER_ARGS, '--phase-instance-id', 'ci:session-manifest:12345678:1'])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.phase.phase_instance_id).toBe('ci:session-manifest:12345678:1')
  })

  it('GIVEN legacy phase_instance_id issue-432:impl:001 WHEN generator --validate THEN exits 0 (backward compat)', () => {
    const result = runProducerCLI([...BASE_PRODUCER_ARGS, '--phase-instance-id', 'issue-432:impl:001'])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.phase.phase_instance_id).toBe('issue-432:impl:001')
  })

  // AC4 negative: invalid CI IDs are rejected by generator validation
  it('GIVEN run_id=0 ci:session-manifest:0:1 WHEN generator --validate THEN exits non-zero', () => {
    const result = runProducerCLI([...BASE_PRODUCER_ARGS, '--phase-instance-id', 'ci:session-manifest:0:1'])
    expect(result.exitCode).not.toBe(0)
    expect(result.stderr).toContain('phase_instance_id')
  })

  it('GIVEN run_attempt=0 ci:session-manifest:12345678:0 WHEN generator --validate THEN exits non-zero', () => {
    const result = runProducerCLI([...BASE_PRODUCER_ARGS, '--phase-instance-id', 'ci:session-manifest:12345678:0'])
    expect(result.exitCode).not.toBe(0)
    expect(result.stderr).toContain('phase_instance_id')
  })

  it('GIVEN space in slug ci:session manifest:12345678:1 WHEN generator --validate THEN exits non-zero', () => {
    const result = runProducerCLI([...BASE_PRODUCER_ARGS, '--phase-instance-id', 'ci:session manifest:12345678:1'])
    expect(result.exitCode).not.toBe(0)
    expect(result.stderr).toContain('phase_instance_id')
  })

  // AC5: help text and invalid-format error list both formats
  it('GIVEN --help flag WHEN running generator THEN output lists issue-<N>:<phase>:<seq> format', () => {
    const result = runProducerCLI(['--help'])
    expect(result.stdout + result.stderr).toContain('issue-<N>:<phase>:<seq>')
  })

  it('GIVEN --help flag WHEN running generator THEN output lists ci:<producer_slug>:<run_id>:<run_attempt> format', () => {
    const result = runProducerCLI(['--help'])
    expect(result.stdout + result.stderr).toContain('ci:<producer_slug>:<run_id>:<run_attempt>')
  })

  it('GIVEN invalid phase_instance_id WHEN generator runs THEN error message lists both accepted formats', () => {
    const result = runProducerCLI([...BASE_PRODUCER_ARGS, '--phase-instance-id', 'invalid-format'])
    expect(result.exitCode).not.toBe(0)
    expect(result.stderr).toContain('issue-')
    expect(result.stderr).toContain('ci:')
  })
})
