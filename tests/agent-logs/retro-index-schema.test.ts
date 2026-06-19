import { describe, expect, it } from 'vitest'

import { detectSchemaMigrationRequirement } from '../../scripts/agent-logs/lib/retro-index-builder.mjs'

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
})
