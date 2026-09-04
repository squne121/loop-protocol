/**
 * complete-agent-run.test.ts
 *
 * #2489 AC4 (#1939 AC5 相当): completeAgentRun() が既存 3 upsert
 * （postAgentRunReport / updateRetroIndex / upsertChatgptRetroContextComment）
 * を順に呼び出し、それぞれの結果を created|updated|unchanged|failed として
 * 返すことを、以下 4 ケースの決定論的 focused test で証明する:
 *
 *   1. 初回実行 -> 3 artifact が作成される
 *   2. 同一 run を再実行 -> コメント数が増えない（unchanged/noop）
 *   3. run report 成功後に retro index が失敗するケースをモックし、
 *      再実行すると run report は重複せず、残りが完成する
 *   4. malformed marker が既に存在するケースで deterministic に failed
 *      になる（既存 policy に従い自動修復・強制上書きはしない）
 */

import { describe, expect, it } from 'vitest'

import { completeAgentRun } from '../../scripts/agent-logs/complete-agent-run.mjs'
import { renderPublicMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
import { computeChatgptRetroContextPayloadDigest } from '../../scripts/agent-logs/lib/chatgpt-retro-context-marker-helper.mjs'
import { createValidObservationSourceResult } from './report-test-fixtures'

const REPO = 'squne121/loop-protocol'
const PARENT_ISSUE = 928

function createDraft() {
  return {
    schema: 'agent_run_draft/v1',
    run_id: 'run-928-001',
    target: { kind: 'issue', id: PARENT_ISSUE },
    phase: 'implementation',
    actor: { type: 'ai_agent', name: 'Codex worker' },
    started_at: '2026-06-17T12:00:00.000Z',
  }
}

function createReport(summary = 'focused tests passed') {
  return {
    schema: 'agent_run_report/v1',
    public_surface_kind: 'github_issue_comment',
    public_safety: {
      redaction_status: 'clean',
      checked_by: 'pnpm agent-run-report:check',
      validator_version: '1.0.0',
      checked_at: '2026-06-17T12:30:00.000Z',
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
        summary,
        artifact_ref: 'artifact:agent-logs-tests',
      },
    ],
    docs_read_refs: [],
  }
}

function createChatgptPayload() {
  const payload = {
    schema: 'chatgpt_retro_context_marker/v1',
    marker_kind: 'CHATGPT_RETRO_CONTEXT_V1',
    repo: REPO,
    target: { type: 'issue', number: PARENT_ISSUE },
    parent_issue: PARENT_ISSUE,
    canonicalization: {
      algorithm: 'canonical-json-v1',
      payload_digest: 'sha256:0000000000000000000000000000000000000000000000000000000000000000',
    },
    refs: {
      run_reports: [
        {
          comment_url: `https://github.com/${REPO}/issues/${PARENT_ISSUE}#issuecomment-1`,
          payload_digest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          schema_ref: 'docs/schemas/agent-run-report.schema.json#agent_run_report/v1',
          validation_verdict: 'pass',
          supersedes_digest: null,
        },
      ],
      retro_index: {
        comment_url: `https://github.com/${REPO}/issues/${PARENT_ISSUE}#issuecomment-2`,
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

function emptySourceBundle() {
  return {
    childIssues: [],
    sourceComments: [],
    prMetadataByNumber: new Map(),
    associatedPrByMergeSha: new Map(),
  }
}

/**
 * Minimal in-memory GitHub issue comments store shared across the three
 * coordinated clients so ownership-marker upserts operate on one comment
 * list, mirroring a single live GitHub issue thread.
 */
class FakeIssueCommentsStore {
  constructor() {
    this.comments = []
    this.nextId = 1
    this.failCreateOnce = false
  }

  async listIssueComments({ page } = {}) {
    if (page && page > 1) return []
    return this.comments.map((comment) => ({ ...comment }))
  }

  async createIssueComment({ body }) {
    if (this.failCreateOnce) {
      this.failCreateOnce = false
      throw new Error('injected transient create failure')
    }
    const id = this.nextId
    this.nextId += 1
    const comment = {
      id,
      body,
      html_url: `https://github.com/${REPO}/issues/${PARENT_ISSUE}#issuecomment-${id}`,
    }
    this.comments.push(comment)
    return comment
  }

  async updateIssueComment({ commentId, body }) {
    const existing = this.comments.find((comment) => comment.id === commentId)
    if (!existing) {
      throw new Error(`unknown commentId ${commentId}`)
    }
    existing.body = body
    return { ...existing }
  }

  async getIssueComment({ commentId }) {
    const existing = this.comments.find((comment) => comment.id === commentId)
    if (!existing) {
      throw new Error(`unknown commentId ${commentId}`)
    }
    return { ...existing }
  }
}

function buildBaseArgs(store) {
  return {
    repo: REPO,
    draft: createDraft(),
    report: createReport(),
    parentIssue: PARENT_ISSUE,
    chatgptTarget: {
      targetType: 'issue',
      targetNumber: PARENT_ISSUE,
      payloadMarkdown: renderPublicMarkdown(createChatgptPayload()),
    },
    dryRun: false,
    confirmLive: true,
    reportClient: store,
    retroIndexClient: store,
    chatgptClient: store,
    sourceBundle: emptySourceBundle(),
  }
}

describe('completeAgentRun (#2489 AC4)', () => {
  it('GIVEN a fresh completion run WHEN completeAgentRun runs THEN all three artifacts are created', async () => {
    const store = new FakeIssueCommentsStore()

    const result = await completeAgentRun(buildBaseArgs(store))

    expect(result.status).toBe('ok')
    expect(result.artifacts.agent_run_report.status).toBe('created')
    expect(result.artifacts.retro_index.status).toBe('created')
    expect(result.artifacts.chatgpt_retro_context.status).toBe('created')
    expect(store.comments).toHaveLength(3)
  })

  it('GIVEN the same run re-invoked WHEN completeAgentRun runs again THEN comment count does not grow (unchanged)', async () => {
    const store = new FakeIssueCommentsStore()
    await completeAgentRun(buildBaseArgs(store))
    expect(store.comments).toHaveLength(3)

    const result = await completeAgentRun(buildBaseArgs(store))

    expect(result.status).toBe('ok')
    expect(result.artifacts.agent_run_report.status).toBe('unchanged')
    expect(result.artifacts.retro_index.status).toBe('unchanged')
    expect(result.artifacts.chatgpt_retro_context.status).toBe('unchanged')
    expect(store.comments).toHaveLength(3)
  })

  it('GIVEN run report succeeds and retro index transiently fails WHEN completeAgentRun re-runs THEN run report is not duplicated and the remaining artifacts complete', async () => {
    const store = new FakeIssueCommentsStore()
    const args = buildBaseArgs(store)

    // First invocation: run report create succeeds; retro index create is
    // injected to fail (simulating a partial-success interruption); chatgpt
    // context create still runs (best-effort, non-blocking per artifact).
    const originalCreate = store.createIssueComment.bind(store)
    let retroIndexCreateShouldFail = true
    store.createIssueComment = async ({ body }) => {
      if (retroIndexCreateShouldFail && typeof body === 'string' && body.includes('agent_retro_index:v1')) {
        retroIndexCreateShouldFail = false
        throw new Error('injected retro index post failure')
      }
      return originalCreate({ body })
    }

    const firstResult = await completeAgentRun(args)
    expect(firstResult.artifacts.agent_run_report.status).toBe('created')
    expect(firstResult.artifacts.retro_index.status).toBe('failed')
    expect(firstResult.artifacts.chatgpt_retro_context.status).toBe('created')
    expect(firstResult.status).toBe('partial')
    expect(store.comments.filter((c) => c.body.includes('agent_run_report:v1'))).toHaveLength(1)
    expect(store.comments.filter((c) => c.body.includes('agent_retro_index:v1'))).toHaveLength(0)

    // Second invocation (rerun of the same completion command): run report
    // must not be duplicated (idempotent noop), retro index now succeeds.
    const secondResult = await completeAgentRun(args)
    expect(secondResult.status).toBe('ok')
    expect(secondResult.artifacts.agent_run_report.status).toBe('unchanged')
    expect(secondResult.artifacts.retro_index.status).toBe('created')
    expect(secondResult.artifacts.chatgpt_retro_context.status).toBe('unchanged')
    expect(store.comments.filter((c) => c.body.includes('agent_run_report:v1'))).toHaveLength(1)
    expect(store.comments.filter((c) => c.body.includes('agent_retro_index:v1'))).toHaveLength(1)
  })

  it('GIVEN a malformed existing agent_run_report marker WHEN completeAgentRun runs THEN it is deterministically reported as failed without auto-repair', async () => {
    const store = new FakeIssueCommentsStore()
    // Pre-seed a malformed marker: matches the ownership prefix but is
    // missing the digest/body contract the parser requires, so
    // postAgentRunReport's malformed-marker guard fires.
    store.comments.push({
      id: 500,
      html_url: `https://github.com/${REPO}/issues/${PARENT_ISSUE}#issuecomment-500`,
      body: `<!-- agent_run_report:v1 repo=${REPO} issue=${PARENT_ISSUE} pr=null run_id=run-928-001 -->\nnot a valid digest marker line`,
    })
    store.nextId = 501

    const result = await completeAgentRun(buildBaseArgs(store))

    expect(result.artifacts.agent_run_report.status).toBe('failed')
    expect(result.artifacts.agent_run_report.reason_code).toBeTruthy()
    // Existing malformed comment is left untouched (no silent overwrite).
    expect(store.comments.find((c) => c.id === 500)?.body).toContain('not a valid digest marker line')
    // The other two artifacts still complete independently.
    expect(result.artifacts.retro_index.status).toBe('created')
    expect(result.artifacts.chatgpt_retro_context.status).toBe('created')
    expect(result.status).toBe('partial')
  })
})
