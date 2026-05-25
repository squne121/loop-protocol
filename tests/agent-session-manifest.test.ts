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
})
