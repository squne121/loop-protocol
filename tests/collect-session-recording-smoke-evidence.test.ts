/**
 * collect-session-recording-smoke-evidence.test.ts
 *
 * scripts/collect-session-recording-smoke-evidence.mjs
 */
import { dirname, resolve } from 'path'
import { fileURLToPath } from 'url'
import { execFileSync } from 'child_process'
import { describe, expect, it } from 'vitest'

import {
  isLegacyManifest,
  isAuthoritativeGeneratedManifest,
  classifyEvidenceEntry,
  collectEvidence,
} from '../scripts/collect-session-recording-smoke-evidence.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const COLLECT_SCRIPT = resolve(__dirname, '../scripts/collect-session-recording-smoke-evidence.mjs')
const FIXTURES_DIR = resolve(__dirname, 'fixtures/session-recording-smoke-verdict')
const MIXED_EVIDENCE_INPUT = resolve(FIXTURES_DIR, 'evidence-input-mixed.json')

function runCli(args: string[]) {
  try {
    const stdout = execFileSync(process.execPath, [COLLECT_SCRIPT, ...args], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { exitCode: 0, stdout }
  } catch (err) {
    const e = err as { status?: number; stdout?: string; stderr?: string }
    return { exitCode: e.status ?? 1, stdout: e.stdout ?? '', stderr: e.stderr ?? '' }
  }
}

const AUTHORITATIVE_MANIFEST = {
  schema: 'agent_session_manifest/v1',
  manifest_id: 'asm-11111111-1111-4111-8111-111111111111',
  recorded_at: '2026-07-01T00:00:00Z',
  repository: 'squne121/loop-protocol',
  actor: { type: 'ai_agent', name: 'implementation-worker' },
  phase: { main_loop: 'impl', ledger_phase: 'implementation', phase_instance_id: 'issue-1312:impl:001' },
  token_usage: { availability: 'unavailable', source: 'none', prompt: null, completion: null, total: null },
  evidence: [
    { source_kind: 'artifact', source_ref: 'artifacts/manifest.json', source_sha256: null, visibility: 'private_artifact' },
  ],
  redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'clean' },
  secret_policy: {
    value_exposed: false,
    mode: 'presence_only',
    producer_contract: {
      declared: true,
      id: 'presence_only_no_secret_values',
      version: 'v1',
      claims: { secret_values_not_serialized: true, presence_only: true },
    },
    runtime_boundary: { attested: false, evidence_ref: null },
  },
}

function withoutSecretPolicy(manifest: Record<string, unknown>) {
  const clone: Record<string, unknown> = { ...manifest }
  delete clone.secret_policy
  return clone
}

describe('isLegacyManifest (AC5)', () => {
  it('GIVEN a manifest without secret_policy WHEN checked THEN it is legacy', () => {
    expect(isLegacyManifest(withoutSecretPolicy(AUTHORITATIVE_MANIFEST))).toBe(true)
  })

  it('GIVEN a manifest with secret_policy WHEN checked THEN it is not legacy', () => {
    expect(isLegacyManifest(AUTHORITATIVE_MANIFEST)).toBe(false)
  })
})

describe('isAuthoritativeGeneratedManifest (AC6)', () => {
  it('GIVEN --manifest-id and --recorded-at both explicit WHEN checked THEN authoritative', () => {
    expect(isAuthoritativeGeneratedManifest(['--manifest-id', 'asm-x', '--recorded-at', '2026-01-01T00:00:00Z'])).toBe(true)
  })

  it('GIVEN only --manifest-id explicit WHEN checked THEN not authoritative', () => {
    expect(isAuthoritativeGeneratedManifest(['--manifest-id', 'asm-x'])).toBe(false)
  })

  it('GIVEN only --recorded-at explicit WHEN checked THEN not authoritative', () => {
    expect(isAuthoritativeGeneratedManifest(['--recorded-at', '2026-01-01T00:00:00Z'])).toBe(false)
  })

  it('GIVEN no explicit flags (default generation) WHEN checked THEN not authoritative', () => {
    expect(isAuthoritativeGeneratedManifest([])).toBe(false)
    expect(isAuthoritativeGeneratedManifest(undefined)).toBe(false)
  })
})

describe('classifyEvidenceEntry', () => {
  it('GIVEN an authoritative agent_session_manifest_script entry WHEN classified THEN ok and authoritative', () => {
    const result = classifyEvidenceEntry({
      source_kind: 'agent_session_manifest_script',
      source_ref: 'artifacts/manifest.json',
      generation_argv: ['--manifest-id', 'asm-x', '--recorded-at', '2026-01-01T00:00:00Z'],
      manifest: AUTHORITATIVE_MANIFEST,
    })
    expect(result.ok).toBe(true)
    expect(result.legacy).toBe(false)
    expect(result.authoritative).toBe(true)
  })

  it('GIVEN a default-generated agent_session_manifest_script entry WHEN classified THEN not authoritative (AC6)', () => {
    const result = classifyEvidenceEntry({
      source_kind: 'agent_session_manifest_script',
      source_ref: 'artifacts/manifest-default.json',
      generation_argv: [],
      manifest: AUTHORITATIVE_MANIFEST,
    })
    expect(result.ok).toBe(true)
    expect(result.authoritative).toBe(false)
  })

  it('GIVEN a manifest missing secret_policy WHEN classified THEN legacy and not authoritative, but ok (AC5)', () => {
    const result = classifyEvidenceEntry({
      source_kind: 'agent_session_manifest_generic',
      source_ref: 'https://github.com/squne121/loop-protocol/issues/246#issuecomment-legacy',
      manifest: withoutSecretPolicy(AUTHORITATIVE_MANIFEST),
    })
    expect(result.ok).toBe(true)
    expect(result.legacy).toBe(true)
    expect(result.authoritative).toBe(false)
  })

  it('GIVEN a negative control manifest (public_github_comment + transcript) WHEN classified THEN invalid (AC8)', () => {
    const negativeControl = {
      ...AUTHORITATIVE_MANIFEST,
      evidence: [
        {
          source_kind: 'transcript',
          source_ref: 'https://github.com/squne121/loop-protocol/issues/246#issuecomment-negative',
          source_sha256: null,
          visibility: 'public_github_comment',
        },
      ],
    }
    const result = classifyEvidenceEntry({
      source_kind: 'negative_control',
      source_ref: 'https://github.com/squne121/loop-protocol/issues/246#issuecomment-negative',
      manifest: negativeControl,
    })
    expect(result.ok).toBe(false)
    expect(result.authoritative).toBe(false)
    expect(result.errors.length).toBeGreaterThan(0)
  })

  it('GIVEN a plain github_comment entry with no manifest WHEN classified THEN ok and authoritative (supporting evidence)', () => {
    const result = classifyEvidenceEntry({
      source_kind: 'github_comment',
      source_ref: 'https://github.com/squne121/loop-protocol/issues/246#issuecomment-plain',
    })
    expect(result.ok).toBe(true)
    expect(result.authoritative).toBe(true)
  })
})

describe('collectEvidence', () => {
  it('GIVEN the mixed fixture set WHEN collected THEN legacy is excluded and authoritative_count matches expectations', async () => {
    const { readFileSync } = await import('fs')
    const entries = JSON.parse(readFileSync(MIXED_EVIDENCE_INPUT, 'utf-8'))
    const summary = collectEvidence(entries)

    // entries: [authoritative script manifest, default-generated script manifest,
    //           legacy generic manifest, negative_control (invalid), plain github_comment]
    expect(summary.legacy_evidence_excluded).toBe(true)
    // authoritative: entry 1 (explicit flags) + entry 5 (plain comment, no manifest) = 2
    expect(summary.authoritative_count).toBe(2)
    expect(summary.evidence_refs.length).toBe(5)
    expect(summary.invalid.length).toBe(1)
  })
})

describe('scripts/collect-session-recording-smoke-evidence.mjs CLI (AC4)', () => {
  it('GIVEN --help WHEN run THEN prints usage and exits 0', () => {
    const { exitCode, stdout } = runCli(['--help'])
    expect(exitCode).toBe(0)
    expect(stdout).toContain('USAGE')
  })

  it('GIVEN the mixed evidence-input fixture WHEN run in strict mode (default) THEN exits non-zero due to the negative control', () => {
    const { exitCode, stdout } = runCli(['--evidence-input', MIXED_EVIDENCE_INPUT])
    expect(exitCode).toBe(1)
    const summary = JSON.parse(stdout)
    expect(summary.legacy_evidence_excluded).toBe(true)
    expect(summary.authoritative_count).toBe(2)
  })

  it('GIVEN the mixed evidence-input fixture WHEN run with --no-strict THEN exits 0', () => {
    const { exitCode } = runCli(['--evidence-input', MIXED_EVIDENCE_INPUT, '--no-strict'])
    expect(exitCode).toBe(0)
  })

  it('GIVEN no --evidence-input WHEN run THEN exits with usage error', () => {
    const { exitCode } = runCli([])
    expect(exitCode).toBe(2)
  })
})
