import { describe, expect, it } from 'vitest'

import { renderPublicMarkdown, validateChatgptRetrospectiveResultAgainstSchema } from '../../scripts/lib/agent-run-report-validation.mjs'
import {
  buildChatgptRetroContextCommentBody,
  computeChatgptRetroContextPayloadDigest,
  parseChatgptRetroContextComment,
  upsertChatgptRetroContextComment,
} from '../../scripts/agent-logs/lib/chatgpt-retro-context-marker-helper.mjs'

function createPayload() {
  const payload = {
    schema: 'chatgpt_retro_context_marker/v1',
    marker_kind: 'CHATGPT_RETRO_CONTEXT_V1',
    repo: 'squne121/loop-protocol',
    target: { type: 'issue', number: 1224 },
    parent_issue: 1153,
    canonicalization: {
      algorithm: 'canonical-json-v1',
      payload_digest: 'sha256:0000000000000000000000000000000000000000000000000000000000000000',
    },
    refs: {
      run_reports: [
        {
          comment_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-1',
          payload_digest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          schema_ref: 'docs/schemas/agent-run-report.schema.json#agent_run_report/v1',
          validation_verdict: 'pass',
          supersedes_digest: null,
        },
      ],
      retro_index: {
        comment_url: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-2',
        payload_digest: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        source_set_digest: 'sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
        schema_ref: 'docs/schemas/agent-retro-index.schema.json#agent_retro_index/v1',
        validation_verdict: 'pass',
      },
    },
    safety: {
      untrusted_evidence_mode: 'typed_refs_only',
      free_form_instructions_present: false,
      forbidden_fields_scan: 'pass',
      rendered_markdown_scan: 'pass',
      raw_values_emitted: false,
    },
    prerequisites: {
      containment_issue: 1157,
      pilot_exception_issue: 1220,
      capability_matrix_issue: 1221,
      schema_issue: 1222,
      adapter_issue: 1223,
      real_pilot_allowed: false,
      evidence_mode: 'synthetic_only',
    },
    created_at: '2026-07-01T00:00:00.000Z',
  }
  payload.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(payload)
  return payload
}

describe('chatgpt retro context marker helper', () => {
  it('GIVEN a canonical payload WHEN building a comment THEN it round-trips through the parser', () => {
    const payload = createPayload()
    const payloadMarkdown = renderPublicMarkdown(payload)
    expect(payload.canonicalization.payload_digest).toBeTruthy()

    const result = buildChatgptRetroContextCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        targetType: 'issue',
        targetNumber: 1224,
        parentIssue: 1153,
      },
      payloadMarkdown,
    })

    const parsed = parseChatgptRetroContextComment({
      id: 1,
      html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9',
      body: result.body,
    })

    expect(parsed.ok).toBe(true)
    expect(parsed.digest).toBe(result.digest)
  })

  it('GIVEN duplicate matching context comments WHEN upsert runs THEN it fails closed', async () => {
    const payloadMarkdown = renderPublicMarkdown(createPayload())
    const built = buildChatgptRetroContextCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        targetType: 'issue',
        targetNumber: 1224,
        parentIssue: 1153,
      },
      payloadMarkdown,
    })
    const client = {
      listIssueComments: async () => [{ id: 1, body: built.body }, { id: 2, body: built.body }],
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    await expect(upsertChatgptRetroContextComment(client, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      payloadMarkdown,
    })).rejects.toThrow(/multiple existing context marker comments/)
  })

  it('GIVEN a new payload with a supersedes digest WHEN upsert dry-run runs THEN it reports supersede', async () => {
    const originalPayload = renderPublicMarkdown(createPayload())
    const original = buildChatgptRetroContextCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        targetType: 'issue',
        targetNumber: 1224,
        parentIssue: 1153,
      },
      payloadMarkdown: originalPayload,
    })
    const nextPayloadObject = createPayload()
    nextPayloadObject.created_at = '2026-07-01T00:10:00.000Z'
    nextPayloadObject.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(nextPayloadObject)
    const nextPayload = renderPublicMarkdown(nextPayloadObject)
    const client = {
      listIssueComments: async () => [{ id: 1, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9', body: original.body }],
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run during dry-run')
      },
    }

    await expect(upsertChatgptRetroContextComment(client, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      payloadMarkdown: nextPayload,
      dryRun: true,
      expectedSupersedesDigest: original.digest,
    })).resolves.toMatchObject({
      action: 'supersede',
    })
  })

  it('GIVEN the retrospective result schema WHEN compiled THEN it accepts a valid public-safe payload', () => {
    const validation = validateChatgptRetrospectiveResultAgainstSchema({
      schema: 'chatgpt_retrospective_result/v1',
      target: {
        repo: 'squne121/loop-protocol',
        issue: 1224,
      },
      input_marker_digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      verdict: 'approve',
      findings: [
        {
          severity: 'low',
          title: 'deterministic bundle',
          evidence_refs: [
            {
              kind: 'github_comment',
              ref: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-1',
              digest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            },
          ],
          claim: 'bundle is deterministic',
          recommendation: 'keep the digest chain',
        },
      ],
      follow_up_issue_candidates: [],
      raw_values_emitted: false,
    })

    expect(validation.valid).toBe(true)
  })
})
