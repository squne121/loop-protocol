import { describe, expect, it } from 'vitest'

import { renderPublicMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
import { createValidRetroIndex } from '../agent-run-report-test-helpers'
import {
  buildRetroIndexCommentBody,
  formatRetroDigestMarker,
  formatRetroOwnershipMarker,
  parseRetroDigestMarker,
  parseRetroOwnershipMarker,
  upsertRetroIndexComment,
  validateRetroCommentBody,
} from '../../scripts/agent-logs/lib/retro-index-comment-helper.mjs'

function createPayloadMarkdown() {
  return renderPublicMarkdown(createValidRetroIndex())
}

describe('retro index comment marker helper', () => {
  it('GIVEN a stable tuple WHEN formatted THEN retro markers remain namespace-separated from agent_run_report markers', () => {
    const ownership = formatRetroOwnershipMarker({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      algorithm: 'retro-index-builder@1',
    })
    const digest = formatRetroDigestMarker({
      canonicalDigest: 'a'.repeat(64),
      sourceSetDigest: 'b'.repeat(64),
    })

    expect(ownership).toBe('<!-- agent_retro_index:v1 repo=squne121/loop-protocol parent_issue=928 algorithm=retro-index-builder@1 -->')
    expect(digest).toBe('<!-- agent_retro_index_digest:v1 sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa source_set_sha256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->')
    expect(ownership).not.toContain('agent_run_report:v1')
  })

  it('GIVEN a rendered retro index comment WHEN parsed and validated THEN both markers round-trip', () => {
    const candidate = buildRetroIndexCommentBody({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      algorithm: 'retro-index-builder@1',
      payloadMarkdown: createPayloadMarkdown(),
      canonicalIndexDigest: `sha256:${'a'.repeat(64)}`,
      sourceCommentSetDigest: `sha256:${'b'.repeat(64)}`,
    })
    const [ownershipLine, digestLine] = candidate.body.split('\n')
    const validation = validateRetroCommentBody(candidate.body, {
      expectedOwnership: {
        repo: 'squne121/loop-protocol',
        parentIssue: 928,
        algorithm: 'retro-index-builder@1',
      },
      expectedCanonicalDigest: 'a'.repeat(64),
      expectedSourceSetDigest: 'b'.repeat(64),
    })

    expect(parseRetroOwnershipMarker(ownershipLine)).toEqual({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      algorithm: 'retro-index-builder@1',
    })
    expect(parseRetroDigestMarker(digestLine)).toEqual({
      canonicalDigest: 'a'.repeat(64),
      sourceSetDigest: 'b'.repeat(64),
    })
    expect(validation.valid).toBe(true)
  })

  it('GIVEN a dry-run upsert with duplicate parent markers WHEN it scans comments THEN it fails closed', async () => {
    const candidate = buildRetroIndexCommentBody({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      algorithm: 'retro-index-builder@1',
      payloadMarkdown: createPayloadMarkdown(),
      canonicalIndexDigest: `sha256:${'a'.repeat(64)}`,
      sourceCommentSetDigest: `sha256:${'b'.repeat(64)}`,
    })
    const client = {
      listIssueComments: async () => [{ id: 1, body: candidate.body }, { id: 2, body: candidate.body }],
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    await expect(upsertRetroIndexComment(client, {
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      algorithm: 'retro-index-builder@1',
      payloadMarkdown: createPayloadMarkdown(),
      canonicalIndexDigest: `sha256:${'a'.repeat(64)}`,
      sourceCommentSetDigest: `sha256:${'b'.repeat(64)}`,
      dryRun: true,
    })).rejects.toThrow(/multiple existing retro index comments match/)
  })
})
