import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve as resolvePath } from 'node:path'
import { describe, expect, it } from 'vitest'

import { buildRetroIndex, detectSchemaMigrationRequirement } from '../../scripts/agent-logs/lib/retro-index-builder.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const RETRO_INDEX_SCHEMA_FILE = resolvePath(__dirname, '../../docs/schemas/agent-retro-index.schema.json')

async function compileRetroIndexSchema() {
  const schema = JSON.parse(readFileSync(RETRO_INDEX_SCHEMA_FILE, 'utf-8'))
  const AjvModule = await import('ajv/dist/2020.js')
  const Ajv2020 = AjvModule.default
  const ajv = new Ajv2020({ strict: false, allErrors: true })
  return ajv.compile(schema)
}

function baseArtifact() {
  return {
    schema: 'agent_retro_index/v1',
    generation_verdict: 'complete',
    entries: [],
    orphan_reports: [],
    ambiguous_links: [],
  }
}

describe('retro index schema guard', () => {
  it('GIVEN an extra key outside docs/schemas/agent-retro-index.schema.json WHEN schema migration detection runs THEN it points to a follow-up Issue instead of expanding the key set here', () => {
    const result = detectSchemaMigrationRequirement({
      schema: 'agent_retro_index/v1',
      generation_verdict: 'complete',
      entries: [],
      orphan_reports: [],
      ambiguous_links: [],
      source_comment_refs: [],
    })

    expect(result).toMatchObject({
      status: 'blocked',
      reason: expect.stringContaining('follow-up Issue'),
    })
    expect(result?.reason).toContain('docs/schemas/agent-retro-index.schema.json')
  })

  // Issue #2308 AC4: retrospective_runs_blocked is an optional additive
  // property -- artifacts that omit it remain schema-valid, and artifacts
  // that include it also validate and stay outside the schema-migration
  // guard's allowedKeys mismatch.
  it('GIVEN an artifact without retrospective_runs_blocked WHEN validated against agent-retro-index.schema.json THEN it stays schema-valid (Issue #2308 AC4 backward compatibility)', async () => {
    const validate = await compileRetroIndexSchema()
    const valid = validate(baseArtifact())
    expect(valid).toBe(true)
  })

  it('GIVEN an artifact with a well-formed retrospective_runs_blocked entry WHEN validated against agent-retro-index.schema.json and detectSchemaMigrationRequirement THEN both accept it (Issue #2308 AC4)', async () => {
    const validate = await compileRetroIndexSchema()
    const artifact = {
      ...baseArtifact(),
      generation_verdict: 'partial',
      retrospective_runs_blocked: [
        {
          run_comment_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000010',
          reason: 'retrospective_run_publication_digest_missing',
        },
      ],
    }
    expect(validate(artifact)).toBe(true)
    expect(detectSchemaMigrationRequirement(artifact)).toBeNull()
  })

  it('GIVEN a retrospective_runs_blocked entry missing the required reason field WHEN validated against agent-retro-index.schema.json THEN it fails closed', async () => {
    const validate = await compileRetroIndexSchema()
    const artifact = {
      ...baseArtifact(),
      generation_verdict: 'partial',
      retrospective_runs_blocked: [
        {
          run_comment_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000010',
        },
      ],
    }
    expect(validate(artifact)).toBe(false)
  })

  it('GIVEN buildRetroIndex output with a blocked retrospective run WHEN validated against agent-retro-index.schema.json THEN the live-shaped artifact validates (Issue #2308 AC4/AC5 end-to-end)', async () => {
    const validate = await compileRetroIndexSchema()
    const built = buildRetroIndex({
      sourceComments: [
        {
          html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000011',
          body: '<!-- agent_retrospective_run:v1 repository_id=squne121/loop-protocol -->\n\nno idempotency_key on the marker line',
          linkedPrHints: [],
          linkedIssueHints: [928],
          branchHint: null,
        },
      ],
      parentIssue: 928,
    })

    expect(validate(built.index)).toBe(true)
    expect(built.index.retrospective_runs_blocked).toHaveLength(1)
  })
})
