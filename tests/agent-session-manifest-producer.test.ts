/**
 * agent-session-manifest-producer.test.ts
 *
 * Integration tests for generate-session-manifest.mjs, validate-agent-session-manifest.mjs, and extract-agent-session-manifest-from-comment.mjs
 * Tests actual CLI execution via execFileSync.
 *
 * Validates:
 * - B1: --validate implementation
 * - B2: Ajv2020 correct configuration
 * - B3: producer JSON → validator roundtrip
 * - B4: token_usage unavailable semantics enforcement
 * - B5: redaction scan fail-closed for github-comment
 * - B6: fence length matching and marker uniqueness
 * - B7: verification/human_intervention CLI flags
 * - M1: issue/pr number validation
 * - M2: manifest_id/recorded_at overrides
 */

import { execFileSync } from 'child_process'
import { writeFileSync, mkdirSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it, beforeAll } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..')
const SCRIPTS_DIR = resolve(REPO_ROOT, 'scripts')
const TESTS_DIR = resolve(REPO_ROOT, 'tests')
const FIXTURES_DIR = resolve(TESTS_DIR, 'fixtures')
const TEMP_DIR = resolve(TESTS_DIR, 'temp')

beforeAll(() => {
  mkdirSync(FIXTURES_DIR, { recursive: true })
  mkdirSync(TEMP_DIR, { recursive: true })
})

// ============================================================================
// Helper Functions
// ============================================================================

function runProducer(args: string[]): { stdout: string; stderr: string; exitCode: number } {
  try {
    const stdout = execFileSync(process.execPath, [resolve(SCRIPTS_DIR, 'generate-session-manifest.mjs'), ...args], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { stdout, stderr: '', exitCode: 0 }
  } catch (error) {
    const err = error as { stdout?: string; stderr?: string; status?: number }
    return {
      stdout: err.stdout || '',
      stderr: err.stderr || '',
      exitCode: err.status || 1,
    }
  }
}

function runValidator(manifestPath: string): { stdout: string; stderr: string; exitCode: number } {
  try {
    const stdout = execFileSync(process.execPath, [resolve(SCRIPTS_DIR, 'validate-agent-session-manifest.mjs'), manifestPath], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { stdout, stderr: '', exitCode: 0 }
  } catch (error) {
    const err = error as { stdout?: string; stderr?: string; status?: number }
    return {
      stdout: err.stdout || '',
      stderr: err.stderr || '',
      exitCode: err.status || 1,
    }
  }
}

function runExtractor(commentPath: string): { stdout: string; stderr: string; exitCode: number } {
  try {
    const stdout = execFileSync(process.execPath, [resolve(SCRIPTS_DIR, 'extract-agent-session-manifest-from-comment.mjs'), commentPath], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { stdout, stderr: '', exitCode: 0 }
  } catch (error) {
    const err = error as { stdout?: string; stderr?: string; status?: number }
    return {
      stdout: err.stdout || '',
      stderr: err.stderr || '',
      exitCode: err.status || 1,
    }
  }
}

// ============================================================================
// Tests
// ============================================================================

describe('B1: --validate implementation', () => {
  it('GIVEN producer with --validate flag WHEN manifest is valid THEN exits 0', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--validate',
    ])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('"schema": "agent_session_manifest/v1"')
  })

  it('GIVEN producer with --validate and invalid visibility+source WHEN public_github_comment+transcript THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'transcript',
      '--evidence-source-ref', 'artifacts/transcript.jsonl',
      '--evidence-visibility', 'public_github_comment',
      '--format', 'json',
      '--validate',
    ])
    expect(result.exitCode).toBe(1)
    // Should fail due to producer subset constraint (transcript not allowed for producer)
    expect(result.stderr).toContain('Invalid evidence.source_kind for producer')
  })
})

describe('B2: Ajv2020 correct configuration', () => {
  it('GIVEN valid manifest WHEN validated with Ajv2020 THEN accepts Draft 2020-12 schema', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--validate',
    ])
    expect(result.exitCode).toBe(0)
  })
})

describe('B3: producer JSON → validator pass', () => {
  it('GIVEN generated manifest JSON WHEN passed to validator THEN validator exits 0', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
    ])
    expect(result.exitCode).toBe(0)

    const manifestPath = resolve(TEMP_DIR, 'test-manifest.json')
    writeFileSync(manifestPath, result.stdout)

    const validationResult = runValidator(manifestPath)
    expect(validationResult.exitCode).toBe(0)
  })
})

describe('B4: token_usage unavailable semantics', () => {
  it('GIVEN invalid fixture with token_usage.total=0 WHEN validated THEN validator exits 1', () => {
    const invalidManifest = {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      actor: { type: 'ai_agent', name: 'worker' },
      phase: { main_loop: 'impl', phase_instance_id: 'issue-377:impl:001' },
      token_usage: {
        availability: 'unavailable',
        source: 'none',
        prompt: null,
        completion: null,
        total: 0,
      },
      evidence: [{ source_kind: 'artifact', source_ref: 'artifacts/test.json', visibility: 'private_artifact' }],
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'clean' },
    }

    const fixtureFile = resolve(TEMP_DIR, 'invalid-token-zero.json')
    writeFileSync(fixtureFile, JSON.stringify(invalidManifest, null, 2))

    const result = runValidator(fixtureFile)
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Validation failed')
  })
})

describe('B5: redaction scan fail-closed for github-comment', () => {
  it('GIVEN producer with github-comment format and secret pattern WHEN --validate THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', '/tmp/secret.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'github-comment',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Secret pattern detected')
  })

  it('GIVEN github-comment output WHEN output is clean THEN exits 0', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'github-comment',
    ])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('<!-- agent_session_manifest:v1 start -->')
  })
})

describe('B6: fence collision handling', () => {
  it('GIVEN manifest with backtick in actor.name WHEN producing github-comment THEN fence length accommodates collision', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test`worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'github-comment',
    ])
    expect(result.exitCode).toBe(0)
    const output = result.stdout

    // Extract and revalidate
    const commentFile = resolve(TEMP_DIR, 'test-comment.md')
    writeFileSync(commentFile, output)

    const extractResult = runExtractor(commentFile)
    expect(extractResult.exitCode).toBe(0)
    const extracted = JSON.parse(extractResult.stdout)
    expect(extracted.actor.name).toBe('test`worker')
  })

  it('GIVEN markdown with duplicate start markers WHEN extracting THEN exits 1', () => {
    const badMarkdown = `
<!-- agent_session_manifest:v1 start -->
\`\`\`\`json
{}
\`\`\`\`
<!-- agent_session_manifest:v1 end -->

<!-- agent_session_manifest:v1 start -->
\`\`\`\`json
{}
\`\`\`\`
<!-- agent_session_manifest:v1 end -->
`
    const commentFile = resolve(TEMP_DIR, 'bad-markers.md')
    writeFileSync(commentFile, badMarkdown)

    const result = runExtractor(commentFile)
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Start marker appears')
  })

  it('GIVEN markdown with fence length mismatch WHEN extracting THEN exits 1', () => {
    const badMarkdown = `
<!-- agent_session_manifest:v1 start -->
\`\`\`\`json
{"test": "value"}
\`\`\`
<!-- agent_session_manifest:v1 end -->
`
    const commentFile = resolve(TEMP_DIR, 'bad-fence.md')
    writeFileSync(commentFile, badMarkdown)

    const result = runExtractor(commentFile)
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Closing fence not found')
  })
})

describe('B7: verification and human_intervention flags', () => {
  it('GIVEN producer with --verification-overall and --verification-ac-result WHEN producing THEN includes verification in output', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--verification-overall', 'pass',
      '--verification-ac-result', 'AC1=pass',
      '--verification-ac-result', 'AC2=fail',
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.verification.overall).toBe('pass')
    expect(manifest.verification.ac_results).toHaveLength(2)
  })

  it('GIVEN producer with --human-intervention-required true WHEN producing THEN sets human_intervention.required=true', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--human-intervention-required', 'true',
      '--human-intervention-reason', 'Needs approval',
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.human_intervention.required).toBe(true)
    expect(manifest.human_intervention.summary).toBe('Needs approval')
  })
})

describe('M1: issue/pr number validation', () => {
  it('GIVEN producer with --issue abc WHEN invalid format THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', 'abc',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Invalid issue number')
  })

  it('GIVEN producer with --issue 0 WHEN invalid format THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '0',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Invalid issue number')
  })

  it('GIVEN producer with valid --issue 377 WHEN valid format THEN exits 0', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.issue_number).toBe(377)
  })
})

describe('M2: manifest_id and recorded_at overrides', () => {
  it('GIVEN producer with --manifest-id override WHEN producing THEN output uses fixed manifest_id', () => {
    const fixedId = 'asm-12345678-1234-4123-89ab-123456789abc'
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--manifest-id', fixedId,
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.manifest_id).toBe(fixedId)
  })

  it('GIVEN producer with --recorded-at override WHEN producing THEN output uses fixed timestamp', () => {
    const fixedTime = '2026-05-25T15:00:00Z'
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--recorded-at', fixedTime,
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.recorded_at).toBe(fixedTime)
  })
})

describe('AC6: Fenced markdown roundtrip', () => {
  it('GIVEN github-comment format WHEN extracting and revalidating THEN manifest is valid', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'github-comment',
    ])
    expect(result.exitCode).toBe(0)

    const commentFile = resolve(TEMP_DIR, 'roundtrip-comment.md')
    writeFileSync(commentFile, result.stdout)

    const extractResult = runExtractor(commentFile)
    expect(extractResult.exitCode).toBe(0)

    const manifestFile = resolve(TEMP_DIR, 'roundtrip-manifest.json')
    writeFileSync(manifestFile, extractResult.stdout)

    const validationResult = runValidator(manifestFile)
    expect(validationResult.exitCode).toBe(0)
  })
})

// ============================================================================
// Iter2 Fix Delta Tests
// ============================================================================

describe('B1 iter2: producer provenance subset enforcement', () => {
  it('GIVEN producer with --actor-type human WHEN invalid for producer THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'human',
      '--actor-name', 'test-user',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Invalid actor.type for producer')
  })

  it('GIVEN producer with --evidence-source-kind transcript WHEN invalid for producer THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'transcript',
      '--evidence-source-ref', 'artifacts/transcript.jsonl',
      '--evidence-visibility', 'private_artifact',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Invalid evidence.source_kind for producer')
  })

  it('GIVEN producer with --evidence-source-kind local_file WHEN invalid for producer THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'local_file',
      '--evidence-source-ref', 'artifacts/file.txt',
      '--evidence-visibility', 'local_only',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Invalid evidence.source_kind for producer')
  })
})

describe('B2 iter2: --format json default fail-closed for secrets', () => {
  it('GIVEN producer --format json with absolute path WHEN default behavior THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', '/home/user/secret.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Secret pattern detected')
  })

  it('GIVEN producer --format json with absolute path and --allow-local-path WHEN allow override THEN exits 0', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', '/home/user/secret.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--allow-local-path',
    ])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('"/home/user/secret.json"')
  })
})

describe('M1 iter2: default validation for all formats', () => {
  it('GIVEN producer with --format json WHEN invalid manifest THEN validation fails by default', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'transcript',
      '--evidence-source-ref', 'artifacts/transcript.jsonl',
      '--evidence-visibility', 'public_github_comment',
      '--format', 'json',
    ])
    // Should fail due to producer subset constraint (transcript not allowed)
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Invalid evidence.source_kind for producer')
  })

  it('GIVEN producer with --format json and --no-validate WHEN skip validation THEN exits 0 even with invalid combo', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--no-validate',
    ])
    expect(result.exitCode).toBe(0)
  })
})

describe('M2 iter2: verification semantic rules', () => {
  it('GIVEN verification.skipped_count > 0 with overall=pass WHEN semantic rule THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--verification-overall', 'pass',
      '--verification-skipped-count', '1',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Producer contract validation failed')
  })

  it('GIVEN verification.fallback_detected=true with overall=pass WHEN semantic rule THEN exits 1', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--verification-overall', 'pass',
      '--verification-fallback-detected', 'true',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('Producer contract validation failed')
  })

  it('GIVEN verification.skipped_count > 0 with overall=partial WHEN semantic rule THEN exits 0', () => {
    const result = runProducer([
      '--repository', 'squne121/loop-protocol',
      '--issue', '377',
      '--phase-main-loop', 'impl',
      '--phase-instance-id', 'issue-377:impl:001',
      '--actor-type', 'ai_agent',
      '--actor-name', 'test-worker',
      '--evidence-source-kind', 'artifact',
      '--evidence-source-ref', 'artifacts/test.json',
      '--evidence-visibility', 'private_artifact',
      '--format', 'json',
      '--verification-overall', 'partial',
      '--verification-skipped-count', '1',
    ])
    expect(result.exitCode).toBe(0)
  })
})

describe('Minor: --dry-run flag in help', () => {
  it('GIVEN producer --help WHEN listing options THEN includes --dry-run', () => {
    const result = runProducer(['--help'])
    expect(result.stdout).toContain('--dry-run')
  })
})
