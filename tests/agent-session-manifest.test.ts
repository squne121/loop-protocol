/**
 * agent-session-manifest.test.ts
 *
 * JSON Schema Draft 2020-12: docs/schemas/agent-session-manifest.schema.json
 * を Ajv 2020-12 で compile / validate するテスト。
 */
import { existsSync, readFileSync } from 'fs'
import { dirname, resolve } from 'path'
import { fileURLToPath } from 'url'
import { execFileSync } from 'child_process'
import { describe, expect, it } from 'vitest'

import {
  validateManifest,
  validateManifestAgainstSchema,
  validateManifestSemantics,
} from '../scripts/lib/agent-session-manifest-validation.mjs'
import {
  resolveIssueNumber,
  isValidIssueNumberValue,
  extractIssueFromString,
} from '../.claude/hooks/generate_session_manifest_from_hook.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCHEMA_PATH = resolve(__dirname, '../docs/schemas/agent-session-manifest.schema.json')
const GENERATOR_SCRIPT = resolve(__dirname, '../scripts/generate-session-manifest.mjs')

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
    secret_policy: {
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

  it('root required preserves base fields with secret_policy', () => {
    // AC2: schema-level exact assertion on root required fields
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const required = schema['required'] as string[]
    // Exact match: base 7 fields + secret_policy
    expect(required).toEqual([
      'schema',
      'manifest_id',
      'recorded_at',
      'repository',
      'actor',
      'phase',
      'redaction',
      'secret_policy',
    ])
  })

  it('secret_policy required shape unchanged', () => {
    // AC7: schema-level exact assertion on secret_policy.required
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const secretPolicy = (schema['properties'] as Record<string, Record<string, unknown>>)['secret_policy']
    const required = secretPolicy['required'] as string[]
    expect(required).toEqual([
      'value_exposed',
      'mode',
      'producer_contract',
      'runtime_boundary',
    ])
  })

  it('legacy boundary_enforced shape', () => {
    // AC8: schema-level assertion that boundary_enforced is NOT in properties
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const secretPolicy = (schema['properties'] as Record<string, Record<string, unknown>>)['secret_policy']
    const properties = secretPolicy['properties'] as Record<string, unknown>
    expect(properties).not.toHaveProperty('boundary_enforced')
  })

  it('secret_policy value_exposed const false', () => {
    // AC11: schema-level const assertion for value_exposed
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const secretPolicy = (schema['properties'] as Record<string, Record<string, unknown>>)['secret_policy']
    const properties = secretPolicy['properties'] as Record<string, Record<string, unknown>>
    const valueExposed = properties['value_exposed']
    expect(valueExposed['type']).toBe('boolean')
    expect(valueExposed['const']).toBe(false)
  })

  it('secret_policy mode enum presence_only', () => {
    // AC11: schema-level enum assertion for mode
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const secretPolicy = (schema['properties'] as Record<string, Record<string, unknown>>)['secret_policy']
    const properties = secretPolicy['properties'] as Record<string, Record<string, unknown>>
    const mode = properties['mode']
    expect(mode['type']).toBe('string')
    expect(mode['enum']).toEqual(['presence_only'])
  })

  it('secret_policy producer_contract const claims', () => {
    // AC11: schema-level const assertion for producer_contract.claims
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const secretPolicy = (schema['properties'] as Record<string, Record<string, unknown>>)['secret_policy']
    const properties = secretPolicy['properties'] as Record<string, Record<string, unknown>>
    const producerContract = properties['producer_contract'] as Record<string, Record<string, unknown>>
    const contractProperties = producerContract['properties'] as Record<string, Record<string, unknown>>
    const claims = contractProperties['claims'] as Record<string, Record<string, unknown>>
    const claimsProperties = claims['properties'] as Record<string, Record<string, unknown>>

    // Both claims MUST be const true
    expect(claimsProperties['secret_values_not_serialized']['const']).toBe(true)
    expect(claimsProperties['presence_only']['const']).toBe(true)
  })

  // AC9: value_exposed=false かつ mode=presence_only の invariant（schema-level + canonical manifest）
  it('secret_policy presence_only invariant', () => {
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const secretPolicy = (schema['properties'] as Record<string, Record<string, unknown>>)['secret_policy']
    const properties = secretPolicy['properties'] as Record<string, Record<string, unknown>>
    expect(properties['value_exposed']['const']).toBe(false)
    expect(properties['mode']['enum']).toEqual(['presence_only'])
    const manifest = createBaseManifest() as Record<string, unknown>
    const secret = manifest['secret_policy'] as Record<string, unknown>
    expect(secret['value_exposed']).toBe(false)
    expect(secret['mode']).toBe('presence_only')
    expect(validateManifestAgainstSchema(manifest).valid).toBe(true)
  })

  // AC11: secret value が manifest に serialize されない契約（schema-level claims + serialization smoke）
  it('no secret values serialized', () => {
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, unknown>
    const secretPolicy = (schema['properties'] as Record<string, Record<string, unknown>>)['secret_policy']
    const properties = secretPolicy['properties'] as Record<string, Record<string, unknown>>
    const producerContract = properties['producer_contract'] as Record<string, Record<string, unknown>>
    const contractProperties = producerContract['properties'] as Record<string, Record<string, unknown>>
    const claims = (contractProperties['claims'] as Record<string, Record<string, unknown>>)[
      'properties'
    ] as Record<string, Record<string, unknown>>
    expect(claims['secret_values_not_serialized']['const']).toBe(true)
    expect(claims['presence_only']['const']).toBe(true)
    const serialized = JSON.stringify(createBaseManifest())
    for (const sentinel of ['sk-', 'ghp_', 'github_pat_']) {
      expect(serialized.includes(sentinel)).toBe(false)
    }
  })
})

describe('agent-session-manifest schema validation (Ajv 2020-12)', () => {
  it('GIVEN manifest without producer field WHEN validating THEN omitted producer remains valid', () => {
    const result = validateManifestAgainstSchema(createBaseManifest())
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  // AC4: canonical base manifest fixture が root secret_policy を含み validation を pass する
  it('GIVEN canonical base manifest fixture WHEN validating THEN canonical base manifest includes root secret_policy and passes', () => {
    const manifest = createBaseManifest()
    expect(Object.prototype.hasOwnProperty.call(manifest, 'secret_policy')).toBe(true)
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(true)
    expect(result.errors).toEqual([])
  })

  // AC3: root secret_policy を欠く manifest は schema validation で reject される（core enforcement）
  it('GIVEN base manifest with root secret_policy removed WHEN validating THEN missing root secret_policy is rejected', () => {
    const manifest = createBaseManifest()
    delete (manifest as Record<string, unknown>)['secret_policy']
    const result = validateManifestAgainstSchema(manifest)
    expect(result.valid).toBe(false)
    expect(
      result.errors.some((error) => error.message?.includes("must have required property 'secret_policy'")),
    ).toBe(true)
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

describe('agent-session-manifest generated manifest validation (subprocess)', () => {
  it('generated manifest json validates against schema', () => {
    // AC5: Run generator subprocess and validate output against schema
    try {
      const output = execFileSync('node', [
        GENERATOR_SCRIPT,
        '--repository', 'squne121/loop-protocol',
        '--issue', '549',
        '--phase-main-loop', 'impl',
        '--phase-ledger-phase', 'implementation',
        '--phase-instance-id', 'issue-549:impl:001',
        '--actor-type', 'ai_agent',
        '--actor-name', 'implementation-worker',
        '--actor-session-id', 'session-001',
        '--evidence-source-kind', 'artifact',
        '--evidence-source-ref', 'artifacts/manifest.json',
        '--evidence-visibility', 'private_artifact',
        '--format', 'json',
      ], { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] })

      // Parse generated JSON
      const manifest = JSON.parse(output)

      // Validate against schema
      const result = validateManifestAgainstSchema(manifest)
      expect(result.valid).toBe(true)
      expect(result.errors).toEqual([])

      // Assert secret_policy is included in generated manifest
      expect(manifest.secret_policy).toBeDefined()
      expect(manifest.secret_policy.value_exposed).toBe(false)
      expect(manifest.secret_policy.mode).toBe('presence_only')
    } catch (error) {
      if (error instanceof Error && error.message.includes('ENOENT')) {
        throw new Error(`Generator script not found at ${GENERATOR_SCRIPT}`, { cause: error })
      }
      throw error
    }
  })

  it('generated github-comment artifact validates against schema', () => {
    // AC6: Run generator with github-comment format and extract/validate JSON
    try {
      const output = execFileSync('node', [
        GENERATOR_SCRIPT,
        '--repository', 'squne121/loop-protocol',
        '--issue', '549',
        '--phase-main-loop', 'impl',
        '--phase-ledger-phase', 'implementation',
        '--phase-instance-id', 'issue-549:impl:001',
        '--actor-type', 'ai_agent',
        '--actor-name', 'implementation-worker',
        '--actor-session-id', 'session-001',
        '--evidence-source-kind', 'artifact',
        '--evidence-source-ref', 'artifacts/manifest.json',
        '--evidence-visibility', 'private_artifact',
        '--format', 'github-comment',
      ], { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] })

      // Extract JSON from fenced code block (HTML marker style)
      // Format: <!-- agent_session_manifest:v1 start -->
      //         ````json
      //         { ... }
      //         ````
      //         <!-- agent_session_manifest:v1 end -->
      const startMarker = '<!-- agent_session_manifest:v1 start -->'
      const endMarker = '<!-- agent_session_manifest:v1 end -->'
      const startIdx = output.indexOf(startMarker)
      const endIdx = output.indexOf(endMarker)

      if (startIdx === -1 || endIdx === -1) {
        throw new Error('HTML markers not found in github-comment output')
      }

      const fencedContent = output.substring(startIdx + startMarker.length, endIdx)

      // Extract JSON from fenced code block (backticks with language tag)
      const fenceMatch = fencedContent.match(/`{4,}json\n([\s\S]*?)\n`{4,}/)
      if (!fenceMatch || !fenceMatch[1]) {
        throw new Error('JSON fenced code block not found in comment output')
      }

      const jsonStr = fenceMatch[1]
      const manifest = JSON.parse(jsonStr)

      // Validate against schema
      const result = validateManifestAgainstSchema(manifest)
      expect(result.valid).toBe(true)
      expect(result.errors).toEqual([])

      // Assert secret_policy is included
      expect(manifest.secret_policy).toBeDefined()
      expect(manifest.secret_policy.value_exposed).toBe(false)
      expect(manifest.secret_policy.mode).toBe('presence_only')
    } catch (error) {
      if (error instanceof Error && error.message.includes('ENOENT')) {
        throw new Error(`Generator script not found at ${GENERATOR_SCRIPT}`, { cause: error })
      }
      throw error
    }
  })
})

// ============================================================================
// Issue Identity Resolution (AC2 — #821)
// ============================================================================

describe('resolveIssueNumber — issue identity dynamic resolution (AC2)', () => {
  // Case 1: payload issue_number wins at highest priority
  it('GIVEN payload issue_number:821 WHEN resolving THEN returns 821 (payload wins)', () => {
    const result = resolveIssueNumber(
      { issue_number: 821 },
      { branchName: 'worktree-issue-999-other', cwdPath: '/some/path' },
    )
    expect(result).toBe(821)
  })

  // Case 2: branch extraction when payload absent
  it('GIVEN no payload and branch worktree-issue-821-session-manifest WHEN resolving THEN returns 821', () => {
    const result = resolveIssueNumber(null, {
      branchName: 'worktree-issue-821-session-manifest',
      cwdPath: null,
    })
    expect(result).toBe(821)
  })

  // Case 3: cwd extraction when neither payload nor branch present
  it('GIVEN no payload no branch and cwd .claude/worktrees/issue-821-foo WHEN resolving THEN returns 821', () => {
    const result = resolveIssueNumber(null, {
      branchName: null,
      cwdPath: '/home/user/project/.claude/worktrees/issue-821-foo',
    })
    expect(result).toBe(821)
  })

  // Case 4: payload beats branch when both present and conflicting
  it('GIVEN payload issue_number:821 and branch worktree-issue-999 WHEN resolving THEN payload wins returns 821', () => {
    const result = resolveIssueNumber(
      { issue_number: 821 },
      { branchName: 'worktree-issue-999-other', cwdPath: null },
    )
    expect(result).toBe(821)
  })

  // Case 5: tool_input.command "gh issue view 402" is not a trusted key → not adopted
  it('GIVEN payload tool_input.command containing issue 402 WHEN resolving THEN ignores it returns null', () => {
    const result = resolveIssueNumber(
      { tool_input: { command: 'gh issue view 402' } },
      { branchName: null, cwdPath: null },
    )
    expect(result).toBeNull()
  })

  // Case 6: invalid issue_number values in payload → fallback
  it('GIVEN issue_number:0 WHEN resolving THEN returns null (0 is invalid)', () => {
    expect(resolveIssueNumber({ issue_number: 0 }, {})).toBeNull()
  })

  it('GIVEN issue_number:"000821" WHEN resolving THEN returns null (leading zeros invalid)', () => {
    expect(resolveIssueNumber({ issue_number: '000821' }, {})).toBeNull()
  })

  it('GIVEN issue_number:-1 WHEN resolving THEN returns null (negative invalid)', () => {
    expect(resolveIssueNumber({ issue_number: -1 }, {})).toBeNull()
  })

  it('GIVEN issue_number:1.5 WHEN resolving THEN returns null (decimal invalid)', () => {
    expect(resolveIssueNumber({ issue_number: 1.5 }, {})).toBeNull()
  })

  it('GIVEN issue_number:"" WHEN resolving THEN returns null (empty string invalid)', () => {
    expect(resolveIssueNumber({ issue_number: '' }, {})).toBeNull()
  })

  // Case 7: fully unresolvable → null (caller uses issue-0 sentinel)
  it('GIVEN no payload no branch no cwd issue pattern WHEN resolving THEN returns null (issue-0 sentinel)', () => {
    const result = resolveIssueNumber(null, {
      branchName: 'main',
      cwdPath: '/home/user/project',
    })
    expect(result).toBeNull()
  })

  // Additional: token-boundary regex — notissue-821 must NOT match
  it('GIVEN branch notissue-821 WHEN extracting THEN returns null (boundary guard)', () => {
    expect(extractIssueFromString('notissue-821')).toBeNull()
  })

  // Additional: issue-000 must NOT match (leading zeros guard)
  it('GIVEN branch worktree-issue-000-foo WHEN extracting THEN returns null (issue-000 guard)', () => {
    expect(extractIssueFromString('worktree-issue-000-foo')).toBeNull()
  })

  // isValidIssueNumberValue edge cases
  it('GIVEN isValidIssueNumberValue WHEN checking valid number THEN returns true', () => {
    expect(isValidIssueNumberValue(821)).toBe(true)
    expect(isValidIssueNumberValue('1')).toBe(true)
    expect(isValidIssueNumberValue(1)).toBe(true)
  })

  it('GIVEN isValidIssueNumberValue WHEN checking edge cases THEN returns false', () => {
    expect(isValidIssueNumberValue(null)).toBe(false)
    expect(isValidIssueNumberValue(undefined)).toBe(false)
    expect(isValidIssueNumberValue('')).toBe(false)
    expect(isValidIssueNumberValue(0)).toBe(false)
    expect(isValidIssueNumberValue(-1)).toBe(false)
    expect(isValidIssueNumberValue('00')).toBe(false)
    expect(isValidIssueNumberValue('1.5')).toBe(false)
  })
})
