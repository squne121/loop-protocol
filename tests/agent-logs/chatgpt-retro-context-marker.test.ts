import { describe, expect, it } from 'vitest'
import { spawnSync } from 'child_process'

import { renderPublicMarkdown, validateChatgptRetrospectiveResultAgainstSchema } from '../../scripts/lib/agent-run-report-validation.mjs'
import {
  buildChatgptRetroContextCommentBody,
  classifyChatgptRetroContextMarkerCandidate,
  computeChatgptRetroContextPayloadDigest,
  parseChatgptRetroContextComment,
  resolveChatgptRetroContextFromFixtures,
  resolveChatgptRetroContextLive,
  upsertChatgptRetroContextComment,
} from '../../scripts/agent-logs/lib/chatgpt-retro-context-marker-helper.mjs'
import { buildSourceCommentSetDigest } from '../../scripts/agent-logs/lib/retro-index-builder.mjs'
import { buildRetroIndexCommentBody } from '../../scripts/agent-logs/lib/retro-index-comment-helper.mjs'
import { buildAgentRunReportCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { mkdtempSync, writeFileSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { resolve } from 'path'
import { createValidObservationSourceResult } from './report-test-fixtures'

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

function createRunReport() {
  return {
    schema: 'agent_run_report/v1',
    public_surface_kind: 'github_issue_comment',
    public_safety: {
      redaction_status: 'clean',
      checked_by: 'pnpm agent-run-report:check',
      validator_version: '1.0.0',
      checked_at: '2026-07-01T00:00:00.000Z',
      verdict: 'pass',
      blocked_reasons: [],
      observation_sources: [createValidObservationSourceResult()],
      entirecli_safety: {
        schema_version: 'entirecli_safety_result/v1',
        verdict: 'not_applicable',
        reason_codes: ['entire_absent'],
        raw_values_emitted: false,
        checked_surfaces: {
          entire_binary: false,
          entire_version: null,
          entire_enable_help: false,
          entire_configure_help: false,
        },
      },
    },
    actor: { type: 'ai_agent', name: 'Codex worker' },
    authority: { level: 'non_authoritative', basis: 'ai_self_report', evidence_refs: [] },
    token_usage: { availability: 'unavailable', source: 'none', prompt: null, completion: null, total: null },
    manifest_refs: [],
    evidence_refs: [],
    commands_summary: [
      {
        command_label: 'pnpm test -- tests/agent-logs',
        exit_code: 0,
        verdict: 'pass',
        summary: 'passed',
        artifact_ref: 'artifact:agent-logs-tests',
      },
    ],
    docs_read_refs: [],
  }
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

  it('GIVEN a fresh create WHEN upsert runs live and the post-write readback finds exactly one marker THEN it succeeds', async () => {
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
    let listCallCount = 0
    const client = {
      listIssueComments: async () => {
        listCallCount += 1
        if (listCallCount === 1) {
          return []
        }
        return [{ id: 99, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-99', body: built.body }]
      },
      createIssueComment: async () => ({ id: 99, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-99' }),
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
      dryRun: false,
    })).resolves.toMatchObject({
      action: 'create',
      comment_id: 99,
    })
    expect(listCallCount).toBe(2)
  })

  it('GIVEN a fresh create WHEN the post-write readback finds two markers (a concurrent-write race) THEN it fails closed', async () => {
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
    let listCallCount = 0
    const client = {
      listIssueComments: async () => {
        listCallCount += 1
        if (listCallCount === 1) {
          return []
        }
        return [
          { id: 99, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-99', body: built.body },
          { id: 100, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-100', body: built.body },
        ]
      },
      createIssueComment: async () => ({ id: 99, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-99' }),
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
      dryRun: false,
    })).rejects.toThrow(/post-write readback found more than one/)
  })

  it('GIVEN an existing comment WHEN upsert supersedes it live and the post-write readback finds exactly one marker THEN it succeeds', async () => {
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
    nextPayloadObject.created_at = '2026-07-01T00:40:00.000Z'
    nextPayloadObject.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(nextPayloadObject)
    const nextPayload = renderPublicMarkdown(nextPayloadObject)
    const nextBuilt = buildChatgptRetroContextCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        targetType: 'issue',
        targetNumber: 1224,
        parentIssue: 1153,
      },
      payloadMarkdown: nextPayload,
    })
    let listCallCount = 0
    const client = {
      listIssueComments: async () => {
        listCallCount += 1
        if (listCallCount === 1) {
          return [{ id: 9, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9', body: original.body }]
        }
        return [{ id: 9, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9', body: nextBuilt.body }]
      },
      getIssueComment: async () => ({ id: 9, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9', body: original.body }),
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => ({ id: 9, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9' }),
    }

    await expect(upsertChatgptRetroContextComment(client, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      payloadMarkdown: nextPayload,
      dryRun: false,
      expectedSupersedesDigest: original.digest,
    })).resolves.toMatchObject({
      action: 'supersede',
      comment_id: 9,
    })
    expect(listCallCount).toBe(2)
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
        type: 'issue',
        number: 1224,
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

  it('GIVEN reordered object keys WHEN computing payload digest THEN canonical JSON digest stays stable', () => {
    const payload = createPayload()
    const reordered = JSON.parse(JSON.stringify(payload))
    reordered.refs = {
      retro_index: payload.refs.retro_index,
      run_reports: payload.refs.run_reports,
    }

    expect(computeChatgptRetroContextPayloadDigest(reordered))
      .toBe(computeChatgptRetroContextPayloadDigest(payload))
  })

  it('GIVEN an existing comment and no expected supersedes digest WHEN upsert runs THEN it fails closed', async () => {
    const originalPayload = createPayload()
    const built = buildChatgptRetroContextCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        targetType: 'issue',
        targetNumber: 1224,
        parentIssue: 1153,
      },
      payloadMarkdown: renderPublicMarkdown(originalPayload),
    })
    const nextPayload = createPayload()
    nextPayload.created_at = '2026-07-01T00:10:00.000Z'
    nextPayload.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(nextPayload)
    const client = {
      listIssueComments: async () => [{ id: 1, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9', body: built.body }],
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
      payloadMarkdown: renderPublicMarkdown(nextPayload),
      dryRun: true,
    })).rejects.toThrow(/expectedSupersedesDigest is required/)
  })

  it('GIVEN referenced comments with recomputed source-set digest mismatch WHEN resolving marker mode THEN it fails closed', async () => {
    const tempDir = mkdtempSync(resolve(tmpdir(), 'chatgpt-retro-context-resolve-'))
    try {
      const reportPayload = createRunReport()
      const reportComment = buildAgentRunReportCommentBody({
        ownership: {
          repo: 'squne121/loop-protocol',
          issueNumber: 1224,
          prNumber: null,
          runId: 'run-1224-001',
        },
        payloadMarkdown: renderPublicMarkdown(reportPayload),
      })
      const reportDigest = `sha256:${reportComment.digest}`
      const retroSourceSetDigest = 'sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
      const retroPayload = {
        schema: 'agent_retro_index/v1',
        generation_verdict: 'complete',
        entries: [
          {
            report_comment_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11',
            report_digest: reportDigest,
            issue: 1224,
            pr: 1300,
            merge_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            tags: ['retro'],
            friction_summary: 'safe',
            quality_signals: ['deterministic'],
            follow_up_issues: [],
          },
        ],
        orphan_reports: [],
        ambiguous_links: [],
      }
      const retroComment = buildRetroIndexCommentBody({
        repo: 'squne121/loop-protocol',
        parentIssue: 1153,
        algorithm: 'retro-index-builder@1',
        payloadMarkdown: renderPublicMarkdown(retroPayload),
        canonicalIndexDigest: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        sourceCommentSetDigest: retroSourceSetDigest,
      })

      const markerPayload = createPayload()
      markerPayload.refs.run_reports[0].comment_url = 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11'
      markerPayload.refs.run_reports[0].payload_digest = reportDigest
      markerPayload.refs.retro_index.comment_url = 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12'
      markerPayload.refs.retro_index.payload_digest = 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
      markerPayload.refs.retro_index.source_set_digest = 'sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
      markerPayload.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(markerPayload)
      const markerComment = buildChatgptRetroContextCommentBody({
        ownership: {
          repo: 'squne121/loop-protocol',
          targetType: 'issue',
          targetNumber: 1224,
          parentIssue: 1153,
        },
        payloadMarkdown: renderPublicMarkdown(markerPayload),
      })

      const markerFile = resolve(tempDir, 'marker.json')
      const commentsFile = resolve(tempDir, 'comments.json')
      writeFileSync(markerFile, JSON.stringify({ id: 21, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-21', body: markerComment.body }))
      writeFileSync(commentsFile, JSON.stringify([
        { id: 11, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11', body: reportComment.body },
        { id: 12, html_url: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12', body: retroComment.body },
      ]))

      await expect(resolveChatgptRetroContextFromFixtures({
        markerCommentJson: markerFile,
        githubCommentsJson: [commentsFile],
      })).rejects.toThrow(/source-set digest must match the recomputed referenced comment set/)
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN a live issue-comment scan with exactly one ownership match WHEN resolving live THEN it returns a structured resolved result', async () => {
    const payload = createPayload()
    const reportPayload = createRunReport()
    const reportComment = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 1224,
        prNumber: 1224,
        runId: 'run-1224-001',
      },
      payloadMarkdown: renderPublicMarkdown(reportPayload),
    })
    const reportDigest = `sha256:${reportComment.digest}`
    const retroPayload = {
      schema: 'agent_retro_index/v1',
      generation_verdict: 'complete',
      entries: [
        {
          report_comment_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11',
          report_digest: reportDigest,
          issue: 1224,
          pr: 1224,
          merge_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          tags: ['retro'],
          friction_summary: 'safe',
          quality_signals: ['deterministic'],
          follow_up_issues: [],
        },
      ],
      orphan_reports: [],
      ambiguous_links: [],
    }
    const retroDigest = 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
    const sourceSetDigest = buildSourceCommentSetDigest([
      {
        comment_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11',
        source_kind: 'issues',
        source_number: 1224,
        body_digest: reportDigest,
      },
      {
        comment_url: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12',
        source_kind: 'issues',
        source_number: 1153,
        body_digest: retroDigest,
      },
    ])
    payload.target = {
      type: 'pull_request',
      number: 1224,
    }
    payload.refs.run_reports[0].comment_url = 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11'
    payload.refs.run_reports[0].payload_digest = reportDigest
    payload.refs.retro_index.comment_url = 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12'
    payload.refs.retro_index.payload_digest = retroDigest
    payload.refs.retro_index.source_set_digest = sourceSetDigest
    payload.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(payload)
    const comment = buildChatgptRetroContextCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        targetType: 'pull_request',
        targetNumber: 1224,
        parentIssue: 1153,
      },
      payloadMarkdown: renderPublicMarkdown(payload),
    })
    const retroComment = buildRetroIndexCommentBody({
      repo: 'squne121/loop-protocol',
      parentIssue: 1153,
      algorithm: 'retro-index-builder@1',
      payloadMarkdown: renderPublicMarkdown(retroPayload),
      canonicalIndexDigest: retroDigest,
      sourceCommentSetDigest: sourceSetDigest,
    })
    const client = {
      listIssueComments: async ({ issueNumber, page }) => {
        if (issueNumber === 1224) {
          return page === 1
            ? [
                { id: 11, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11', body: reportComment.body },
                { id: 41, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41', body: comment.body },
              ]
            : []
        }
        if (issueNumber === 1153) {
          return page === 1
            ? [{ id: 12, html_url: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12', body: retroComment.body }]
            : []
        }
        return []
      },
      listPullRequestReviewsPage: async () => ({
        items: [
          {
            id: 4671349811,
            node_id: 'PRR_kwDOSfQcDc8AAAABFm8kMw',
            state: 'COMMENTED',
            commit_id: '5190a306c3795bd2762ca218dd173a663207cfad',
            submitted_at: '2026-07-10T12:27:19Z',
            html_url: 'https://github.com/squne121/loop-protocol/pull/1224#pullrequestreview-4671349811',
          },
        ],
        hasNextPage: false,
      }),
      listPullRequestReviewCommentsPage: async () => ({
        items: [
          {
            id: 3558855703,
            node_id: 'PRRC_kwDOSfQcDc7UH9QX',
            pull_request_review_id: 4671349811,
            path: 'docs/dev/agent-retro-index.md',
            line: 100,
            commit_id: '5190a306c3795bd2762ca218dd173a663207cfad',
            created_at: '2026-07-10T12:27:19Z',
            updated_at: '2026-07-10T12:27:19Z',
            html_url: 'https://github.com/squne121/loop-protocol/pull/1224#discussion_r3558855703',
          },
        ],
        hasNextPage: false,
      }),
      listPullRequestReviewThreadsPage: async () => ({
        items: [
          {
            id: 'PRRT_kwDOSfQcDc6P4Sca',
            isResolved: true,
            isOutdated: false,
            path: 'docs/dev/agent-retro-index.md',
            line: 100,
            subjectType: 'LINE',
            comments: {
              totalCount: 1,
              pageInfo: {
                hasNextPage: false,
              },
            },
          },
        ],
        hasNextPage: false,
        endCursor: null,
      }),
    }

    await expect(resolveChatgptRetroContextLive(client, {
      repo: 'squne121/loop-protocol',
      targetType: 'pull_request',
      targetNumber: 1224,
      parentIssue: 1153,
    })).resolves.toMatchObject({
      status: 'resolved',
      target: {
        type: 'pull_request',
        number: 1224,
        endpoint_kind: 'issue_comments_for_pull_request',
      },
      comment_chain: {
        status: 'resolved',
        marker_comment: {
          id: 41,
          url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
        },
        matched_comment_count: 1,
        evidence_ref_count: 1,
        source_manifest_count: 3,
        pagination: {
          comments_complete: true,
          reference_comments_complete: true,
        },
      },
      pr_review_surface: {
        status: 'resolved',
        review_count: 1,
        review_comment_count: 1,
        resolved_thread_count: 1,
        pagination: {
          complete: true,
        },
      },
    })
  })

  it('GIVEN a live issue-comment scan with malformed marker syntax WHEN resolving live THEN it fails closed', async () => {
    const client = {
      listIssueComments: async () => [
        {
          id: 7,
          html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-7',
          body: '<!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=issue:1224 parent_issue=1153 trailing -->',
        },
      ],
    }

    await expect(resolveChatgptRetroContextLive(client, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
    })).resolves.toMatchObject({
      status: 'blocked_malformed_marker_syntax',
      comment_chain: {
        status: 'blocked_malformed_marker_syntax',
        marker_comment: {
          id: 7,
        },
      },
      pr_review_surface: {
        status: 'not_applicable',
      },
    })
  })

  it('GIVEN the context chain is malformed but the PR review surface resolves WHEN resolving live THEN top-level status stays blocked_malformed_marker_syntax', async () => {
    const client = {
      listIssueComments: async () => [
        {
          id: 7,
          html_url: 'https://github.com/squne121/loop-protocol/pull/1224#issuecomment-7',
          body: '<!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=pull_request:1224 parent_issue=1153 trailing -->',
        },
      ],
      listPullRequestReviewsPage: async () => ({
        items: [
          {
            id: 4671349811,
            node_id: 'PRR_kwDOSfQcDc8AAAABFm8kMw',
            state: 'COMMENTED',
            commit_id: '5190a306c3795bd2762ca218dd173a663207cfad',
            submitted_at: '2026-07-10T12:27:19Z',
            html_url: 'https://github.com/squne121/loop-protocol/pull/1224#pullrequestreview-4671349811',
          },
        ],
        hasNextPage: false,
      }),
      listPullRequestReviewCommentsPage: async () => ({
        items: [
          {
            id: 3558855703,
            node_id: 'PRRC_kwDOSfQcDc7UH9QX',
            pull_request_review_id: 4671349811,
            path: 'docs/dev/agent-retro-index.md',
            line: 100,
            commit_id: '5190a306c3795bd2762ca218dd173a663207cfad',
            created_at: '2026-07-10T12:27:19Z',
            updated_at: '2026-07-10T12:27:19Z',
            html_url: 'https://github.com/squne121/loop-protocol/pull/1224#discussion_r3558855703',
          },
        ],
        hasNextPage: false,
      }),
      listPullRequestReviewThreadsPage: async () => ({
        items: [
          {
            id: 'PRRT_kwDOSfQcDc6P4Sca',
            isResolved: true,
            isOutdated: false,
            path: 'docs/dev/agent-retro-index.md',
            line: 100,
            subjectType: 'LINE',
            comments: {
              totalCount: 1,
              pageInfo: {
                hasNextPage: false,
              },
            },
          },
        ],
        hasNextPage: false,
        endCursor: null,
      }),
    }

    await expect(resolveChatgptRetroContextLive(client, {
      repo: 'squne121/loop-protocol',
      targetType: 'pull_request',
      targetNumber: 1224,
      parentIssue: 1153,
    })).resolves.toMatchObject({
      status: 'blocked_malformed_marker_syntax',
      comment_chain: {
        status: 'blocked_malformed_marker_syntax',
      },
      pr_review_surface: {
        status: 'resolved',
      },
    })
  })

  it('GIVEN a live issue-comment scan that hits the page budget WHEN resolving live THEN it returns a structured blocked result', async () => {
    const client = {
      listIssueCommentsPage: async ({ page }) => ({
        items: Array.from({ length: 100 }, (_, index) => ({
          id: (page - 1) * 100 + index + 1,
          body: 'plain comment body',
        })),
        hasNextPage: true,
      }),
    }

    await expect(resolveChatgptRetroContextLive(client, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
    })).resolves.toMatchObject({
      status: 'blocked_page_budget_exhausted',
      target: {
        type: 'issue',
        number: 1224,
        endpoint_kind: 'issue_comments_for_issue',
      },
      comment_chain: {
        status: 'blocked_page_budget_exhausted',
        matched_comment_count: 0,
        pagination: {
          comments_complete: false,
        },
      },
      pr_review_surface: {
        status: 'not_applicable',
      },
    })
  })

  it('GIVEN an existing comment and a changed digest before update WHEN upsert runs THEN it fails closed after a fresh reread', async () => {
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
    nextPayloadObject.created_at = '2026-07-01T00:20:00.000Z'
    nextPayloadObject.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(nextPayloadObject)
    const stalePayloadObject = createPayload()
    stalePayloadObject.created_at = '2026-07-01T00:30:00.000Z'
    stalePayloadObject.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(stalePayloadObject)
    const staleComment = buildChatgptRetroContextCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        targetType: 'issue',
        targetNumber: 1224,
        parentIssue: 1153,
      },
      payloadMarkdown: renderPublicMarkdown(stalePayloadObject),
    })
    const client = {
      listIssueComments: async () => [{ id: 1, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9', body: original.body }],
      getIssueComment: async () => ({ id: 1, html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-9', body: staleComment.body }),
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
      payloadMarkdown: renderPublicMarkdown(nextPayloadObject),
      dryRun: false,
      expectedSupersedesDigest: original.digest,
    })).rejects.toThrow(/digest changed before update/)
  })

  it('GIVEN CLI resolve-live without repo WHEN executing the command THEN it returns a machine-readable error', () => {
    const scriptPath = resolve(process.cwd(), 'scripts/agent-logs/lib/chatgpt-retro-context-marker-helper.mjs')
    const result = spawnSync(process.execPath, [
      scriptPath,
      '--command', 'resolve-live',
      '--target-type', 'issue',
      '--target-number', '1224',
      '--parent-issue', '1153',
    ], {
      encoding: 'utf-8',
    })

    expect(result.status).toBe(1)
    expect(JSON.parse(result.stdout)).toMatchObject({
      command: 'resolve-live',
      status: 'error',
      error_code: 'chatgpt_retro_context.repo',
    })
  })

  it('GIVEN CLI post without confirm-live WHEN executing a live post THEN it returns a machine-readable error', () => {
    const tempDir = mkdtempSync(resolve(tmpdir(), 'chatgpt-retro-context-cli-'))
    try {
      const payloadFile = resolve(tempDir, 'payload.md')
      writeFileSync(payloadFile, renderPublicMarkdown(createPayload()))
      const scriptPath = resolve(process.cwd(), 'scripts/agent-logs/lib/chatgpt-retro-context-marker-helper.mjs')
      const result = spawnSync(process.execPath, [
        scriptPath,
        '--command', 'post',
        '--repo', 'squne121/loop-protocol',
        '--target-type', 'issue',
        '--target-number', '1224',
        '--parent-issue', '1153',
        '--payload-markdown-file', payloadFile,
        '--dry-run', 'false',
        '--confirm-live', 'false',
      ], {
        encoding: 'utf-8',
      })

      expect(result.status).toBe(1)
      expect(JSON.parse(result.stdout)).toMatchObject({
        command: 'post',
        status: 'error',
        error_code: 'chatgpt_retro_context.live_confirmation_required',
      })
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

describe('classifyChatgptRetroContextMarkerCandidate', () => {
  it('GIVEN a canonical ownership marker as the first non-empty line WHEN classifying THEN it is a valid_marker', () => {
    const body = '<!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=issue:1224 parent_issue=1153 -->\n<!-- CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->\n\npayload'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('valid_marker')
  })

  it('GIVEN leading blank lines before the canonical ownership marker WHEN classifying THEN it is still a valid_marker', () => {
    const body = '\n\n<!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=issue:1224 parent_issue=1153 -->\n<!-- CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('valid_marker')
  })

  it('GIVEN a broken ownership marker (missing parent_issue) at column 0 WHEN classifying THEN it is malformed_marker_intent', () => {
    const body = '<!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=issue:1224 -->'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('malformed_marker_intent')
  })

  it('GIVEN an unclosed ownership marker at column 0 WHEN classifying THEN it is malformed_marker_intent', () => {
    const body = '<!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=issue:1224 parent_issue=1153'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('malformed_marker_intent')
  })

  it('GIVEN a digest-marker-shaped first line WHEN classifying THEN it is malformed_marker_intent (wrong position for ownership)', () => {
    const body = '<!-- CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=zz -->'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('malformed_marker_intent')
  })

  it('GIVEN prose mentioning the marker name WHEN classifying THEN it is not_marker', () => {
    const body = 'The CHATGPT_RETRO_CONTEXT_V1 marker starts every context comment.'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it('GIVEN the marker wrapped in inline code WHEN classifying THEN it is not_marker', () => {
    const body = 'Example: `<!-- CHATGPT_RETRO_CONTEXT_V1 repo=a/b target=issue:1 parent_issue=2 -->` is the marker.'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it('GIVEN the marker inside a backtick fenced code block WHEN classifying THEN it is not_marker', () => {
    const body = '```\n<!-- CHATGPT_RETRO_CONTEXT_V1 repo=a/b target=issue:1 parent_issue=2 -->\n```'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it('GIVEN the marker inside a tilde fenced code block WHEN classifying THEN it is not_marker', () => {
    const body = '~~~\n<!-- CHATGPT_RETRO_CONTEXT_V1 repo=a/b target=issue:1 parent_issue=2 -->\n~~~'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it('GIVEN the marker inside a blockquote WHEN classifying THEN it is not_marker', () => {
    const body = '> <!-- CHATGPT_RETRO_CONTEXT_V1 repo=a/b target=issue:1 parent_issue=2 -->'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it('GIVEN the marker inside a list item WHEN classifying THEN it is not_marker', () => {
    const body = '- <!-- CHATGPT_RETRO_CONTEXT_V1 repo=a/b target=issue:1 parent_issue=2 -->'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it.each([1, 2, 3, 4])('GIVEN the marker indented by %i spaces WHEN classifying THEN it is not_marker', (spaceCount) => {
    const body = `${' '.repeat(spaceCount)}<!-- CHATGPT_RETRO_CONTEXT_V1 repo=a/b target=issue:1 parent_issue=2 -->`
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it('GIVEN the marker indented by a tab WHEN classifying THEN it is not_marker', () => {
    const body = '\t<!-- CHATGPT_RETRO_CONTEXT_V1 repo=a/b target=issue:1 parent_issue=2 -->'
    expect(classifyChatgptRetroContextMarkerCandidate(body).state).toBe('not_marker')
  })

  it('GIVEN an empty body WHEN classifying THEN it is not_marker', () => {
    expect(classifyChatgptRetroContextMarkerCandidate('').state).toBe('not_marker')
  })

  it('GIVEN a non-string body WHEN classifying THEN it is not_marker', () => {
    expect(classifyChatgptRetroContextMarkerCandidate(undefined).state).toBe('not_marker')
  })
})

describe('validateChatgptRetroContextCommentBody indentation regression (via parseChatgptRetroContextComment)', () => {
  it('GIVEN an ownership marker indented by 4 spaces WHEN parsing the comment THEN it is not treated as an ownership marker at all', () => {
    const body = '    <!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=issue:1224 parent_issue=1153 -->\n<!-- CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->'
    const parsed = parseChatgptRetroContextComment({ body })
    expect(parsed.ownership).toBeUndefined()
    expect(parsed.malformed).toBe(false)
  })
})

})
