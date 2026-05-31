/**
 * agent-session-manifest.test.ts
 *
 * JSON Schema Draft 2020-12: docs/schemas/agent-session-manifest.schema.json
 * を Ajv 2020-12 で compile / validate するテスト。
 */
import { existsSync, readFileSync } from 'fs'
import { dirname, resolve } from 'path'
import { fileURLToPath } from 'url'
import { describe, expect, it } from 'vitest'

import {
  validateManifest,
  validateManifestAgainstSchema,
  validateManifestSemantics,
} from '../scripts/lib/agent-session-manifest-validation.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCHEMA_PATH = resolve(__dirname, '../docs/schemas/agent-session-manifest.schema.json')

function createBaseManifest() {
  return {
    schema: 'agent_session_manifest/v1',
    manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
    recorded_at: '2026-05-24T10:00:00Z',
    repository: 'squne121/loop-protocol',
    actor: {
      type: 'ai_agent',
      name: 'implementation-worker',
      session_id: 'session-001',
    },
    phase: {
      main_loop: 'impl',
      ledger_phase: 'implementation',
      phase_instance_id: 'issue-401:impl:001',
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
        source_kind: 'artifact',
        source_ref: 'artifacts/manifest.json',
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

function createSecretPolicy() {
  return {
    value_exposed: false,
    mode: 'presence_only',
    producer_contract: {
      declared: true,
      id: 'presence_only_no_secret_values',
      version: 'v1',
      claims: {
        secret_values_not_serialized: true,
        presence_only: true,
      },
    },
    runtime_boundary: {
      attested: false,
      evidence_ref: null,
    },
  }
}

describe('agent-session-manifest schema file', () => {
  it('GIVEN schema JSON file WHEN checking existence THEN file exists', () => {
    expect(existsSync(SCHEMA_PATH)).toBe(true)
  })

  it('GIVEN schema JSON file WHEN parsing THEN it has draft 2020-12 metadata', () => {
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    expect(schema['$schema']).toBe('https://json-schema.org/draft/2020-12/schema')
    expect(schema['title']).toBe('agent_session_manifest/v1')
  })

  it('GIVEN schema JSON file WHEN checking producer property THEN root optionality and nested shape are defined', () => {
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const required = schema['required'] as string[]
    const producer = (schema['properties'] as Record<string, Record<string, unknown>>)['producer']
    expect(required).not.toContain('producer')
    expect(producer['type']).toBe('object')
    expect(producer['additionalProperties']).toBe(false)
    expect(producer['required']).toEqual(['kind'])
  })
})

describe('agent-session-manifest schema validation (Ajv 2020-12)', () => {
  it('GIVEN manifest without producer field WHEN validating THEN omitted producer remains valid', () => {
    const result = validateManifestAgainstSchema(createBaseManifest())
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN manifest with valid producer kind WHEN validating THEN valid producer kind manifest is accepted', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        kind: 'script_generated',
        version: null,
        command: 'node scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
      sanitization_status: 'sanitized',
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN manifest with invalid producer kind WHEN validating THEN invalid producer kind manifest is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        kind: 'unknown_source',
        version: null,
        command: 'node scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path.includes('/producer/kind'))).toBe(true)
  })

  it('GIVEN manifest with unknown nested producer property WHEN validating THEN unknown nested producer property is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        kind: 'script_generated',
        version: null,
        command: 'node scripts/generate-session-manifest.mjs',
        source_ref: null,
        secret_dump: 'forbidden',
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.message?.includes('must NOT have additional properties'))).toBe(true)
  })

  it('GIVEN manifest with missing producer.kind WHEN validating THEN missing producer.kind is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        version: null,
        command: null,
        source_ref: null,
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.message?.includes("must have required property 'kind'"))).toBe(true)
  })

  it('GIVEN manifest with stale sanitization_status value WHEN validating THEN schema drift is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      sanitization_status: 'clean',
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path.includes('/sanitization_status'))).toBe(true)
  })

  it('GIVEN manifest with separated secret policy contract WHEN validating THEN static producer contract is accepted without runtime attestation', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: createSecretPolicy(),
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN manifest with legacy boundary_enforced shape WHEN validating THEN legacy shape is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        value_exposed: false,
        boundary_enforced: true,
        mode: 'presence_only',
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.message?.includes('must have required property'))).toBe(true)
    expect(result.errors.some((error) => error.message?.includes('must NOT have additional properties'))).toBe(true)
  })

  it('GIVEN manifest with attested runtime boundary and null evidence WHEN validating THEN missing runtime evidence is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        ...createSecretPolicy(),
        runtime_boundary: {
          attested: true,
          evidence_ref: null,
        },
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path.includes('/secret_policy/runtime_boundary/evidence_ref'))).toBe(true)
  })

  it('GIVEN manifest with attested runtime boundary and evidence WHEN validating THEN runtime evidence requirement is accepted', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        ...createSecretPolicy(),
        runtime_boundary: {
          attested: true,
          evidence_ref: 'artifacts/runtime-boundary.log',
        },
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN unattested runtime boundary without evidence_ref WHEN validating THEN explicit null evidence_ref is required', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        ...createSecretPolicy(),
        runtime_boundary: {
          attested: false,
        },
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path.includes('/secret_policy/runtime_boundary'))).toBe(true)
  })

  it('GIVEN manifest with whitespace-only attested evidence WHEN validating THEN whitespace evidence is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        ...createSecretPolicy(),
        runtime_boundary: {
          attested: true,
          evidence_ref: '   ',
        },
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path.includes('/secret_policy/runtime_boundary/evidence_ref'))).toBe(true)
  })

  it('GIVEN manifest mixing legacy boundary_enforced with new fields WHEN validating THEN mixed legacy shape is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        ...createSecretPolicy(),
        boundary_enforced: true,
      },
    }
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.message?.includes('must NOT have additional properties'))).toBe(true)
  })
})

describe('agent-session-manifest semantic validation', () => {
  it('GIVEN unavailable token usage with null values WHEN validating semantics THEN result is valid', () => {
    const result = validateManifestSemantics(createBaseManifest())
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN unavailable token usage with total=0 WHEN validating semantics THEN result is invalid', () => {
    const manifest = {
      ...createBaseManifest(),
      token_usage: {
        availability: 'unavailable',
        source: 'none',
        prompt: null,
        completion: null,
        total: 0,
      },
    }
    const result = validateManifestSemantics(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path === 'token_usage.total')).toBe(true)
  })

  it('GIVEN producer.kind mismatches evidence.source_kind WHEN validating semantics THEN result is invalid', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        kind: 'github_action_generated',
        version: null,
        command: 'node scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
    }
    const result = validateManifestSemantics(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path === 'evidence[0].source_kind')).toBe(true)
  })

  it('GIVEN producer without evidence WHEN validating semantics THEN result is invalid', () => {
    const manifest = {
      ...createBaseManifest(),
      evidence: [],
      producer: {
        kind: 'script_generated',
        version: null,
        command: 'node scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
    }
    const result = validateManifestSemantics(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path === 'evidence')).toBe(true)
  })

  it('GIVEN mixed evidence kinds WHEN validating semantics THEN result is invalid', () => {
    const manifest = {
      ...createBaseManifest(),
      evidence: [
        {
          source_kind: 'artifact',
          source_ref: 'artifacts/manifest.json',
          source_sha256: null,
          visibility: 'private_artifact',
        },
        {
          source_kind: 'ci_check',
          source_ref: 'https://github.com/squne121/loop-protocol/actions/runs/1',
          source_sha256: null,
          visibility: 'public_github_comment',
        },
      ],
      producer: {
        kind: 'script_generated',
        version: null,
        command: 'node scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
    }
    const result = validateManifestSemantics(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path === 'evidence[1].source_kind')).toBe(true)
  })

  it('GIVEN producer.command contains absolute local path WHEN validating semantics THEN result is invalid', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        kind: 'script_generated',
        version: null,
        command: '/home/squne/projects/LOOP_PROTOCOL/scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
    }
    const result = validateManifestSemantics(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path === 'producer.command')).toBe(true)
  })

  it('GIVEN producer.command contains token-like value WHEN validating semantics THEN result is invalid', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        kind: 'script_generated',
        version: null,
        command: 'OPENAI_API_KEY=sk-12345678901234567890 node scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
    }
    const result = validateManifestSemantics(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path === 'producer.command')).toBe(true)
  })

  it('GIVEN manifest with valid producer object WHEN running combined validation THEN combined result is valid', () => {
    const manifest = {
      ...createBaseManifest(),
      producer: {
        kind: 'script_generated',
        version: null,
        command: 'node scripts/generate-session-manifest.mjs',
        source_ref: null,
      },
      sanitization_status: 'sanitized',
    }
    const result = validateManifest(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN attested runtime boundary with evidence_ref not linked to evidence list WHEN validating semantics THEN it is rejected', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        ...createSecretPolicy(),
        runtime_boundary: {
          attested: true,
          evidence_ref: 'artifacts/runtime-boundary.log',
        },
      },
    }
    const result = validateManifest(manifest)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.path === 'secret_policy.runtime_boundary.evidence_ref')).toBe(true)
  })

  it('GIVEN attested runtime boundary with linked evidence entry WHEN validating semantics THEN it is accepted', () => {
    const manifest = {
      ...createBaseManifest(),
      secret_policy: {
        ...createSecretPolicy(),
        runtime_boundary: {
          attested: true,
          evidence_ref: 'artifacts/runtime-boundary.log',
        },
      },
      evidence: [
        {
          source_kind: 'artifact',
          source_ref: 'artifacts/runtime-boundary.log',
          source_sha256: null,
          visibility: 'private_artifact',
        },
      ],
    }
    const result = validateManifest(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })
})
