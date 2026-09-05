/**
 * complete-agent-run.test.ts
 *
 * #2489 AC4 (#1939 AC5 相当): completeAgentRun() が既存 3 upsert
 * （postAgentRunReport / updateRetroIndex / upsertChatgptRetroContextComment）
 * を順に呼び出し、それぞれの結果を created|updated|unchanged|failed として
 * 返すことを、以下 4 ケースの決定論的 focused test で証明する:
 *
 *   1. 初回実行 -> 3 artifact が作成される（chatgpt_retro_context の payload
 *      は postAgentRunReport / updateRetroIndex の実際の戻り値から構築され、
 *      pre-baked markdown の内容ではないことを確認する）
 *   2. 同一 run を再実行 -> コメント数が増えない（unchanged/noop）
 *   3. run report 成功後に retro index が失敗するケースをモックし、
 *      再実行すると run report は重複せず、残りが完成する。P0-2 fix 後は
 *      1回目の invocation で retro index が failed のとき chatgpt_retro_context
 *      は prerequisite_failed として試行されず、2回目の invocation で初めて
 *      実際の run report / retro index 参照から marker が作成される。作成後の
 *      marker を既存 live resolver（resolveChatgptRetroContextLive）に通し、
 *      実際の comment URL / digest を参照する `resolved` 状態に到達することを
 *      確認する
 *   4. malformed marker が既に存在するケースで deterministic に failed
 *      になる（既存 policy に従い自動修復・強制上書きはしない）
 */

import { describe, expect, it } from 'vitest'

import { completeAgentRun } from '../../scripts/agent-logs/complete-agent-run.mjs'
import { resolveChatgptRetroContextLive } from '../../scripts/agent-logs/lib/chatgpt-retro-context-marker-helper.mjs'
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
  it('GIVEN a fresh completion run WHEN completeAgentRun runs THEN all three artifacts are created and the marker payload references the ACTUAL run report / retro index comments (P0-2 fix)', async () => {
    const store = new FakeIssueCommentsStore()

    const result = await completeAgentRun(buildBaseArgs(store))

    expect(result.status).toBe('ok')
    expect(result.artifacts.agent_run_report.status).toBe('created')
    expect(result.artifacts.retro_index.status).toBe('created')
    expect(result.artifacts.chatgpt_retro_context.status).toBe('created')
    expect(store.comments).toHaveLength(3)

    // P0-2 fix: the context marker must cite the ACTUAL comment_url/digest of
    // the run report and retro index artifacts that were just created in
    // this same invocation (comments 1 and 2), never a pre-baked/hardcoded
    // reference.
    const markerComment = store.comments.find((c) => c.body.includes('CHATGPT_RETRO_CONTEXT_V1'))
    expect(markerComment.body).toContain(
      `comment_url": "${result.artifacts.agent_run_report.comment_url}"`,
    )
    expect(markerComment.body).toContain(
      `payload_digest": "sha256:${result.artifacts.agent_run_report.digest}"`,
    )
    expect(markerComment.body).toContain(
      `comment_url": "${result.artifacts.retro_index.comment_url}"`,
    )
    expect(markerComment.body).toContain(
      `payload_digest": "${result.artifacts.retro_index.digest}"`,
    )

    const live = await resolveChatgptRetroContextLive(store, {
      repo: REPO,
      targetType: 'issue',
      targetNumber: PARENT_ISSUE,
      parentIssue: PARENT_ISSUE,
    })
    expect(live.comment_chain.status).toBe('resolved')
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

  it('GIVEN run report succeeds and retro index transiently fails WHEN completeAgentRun re-runs THEN run report is not duplicated, the context marker is deferred until both prerequisites are real, and the retried marker resolves against the ACTUAL report/index references', async () => {
    const store = new FakeIssueCommentsStore()
    const args = buildBaseArgs(store)

    // First invocation: run report create succeeds; retro index create is
    // injected to fail (simulating a partial-success interruption).
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
    // P0-2 fix: retro_index failed this invocation, so the context marker
    // must NOT be attempted (it would otherwise have no real retro_index
    // reference to cite) -- it is deferred as prerequisite_failed rather
    // than created against an incomplete/fabricated reference.
    expect(firstResult.artifacts.chatgpt_retro_context.status).toBe('failed')
    expect(firstResult.artifacts.chatgpt_retro_context.reason_code).toBe('prerequisite_failed')
    expect(firstResult.status).toBe('partial')
    expect(store.comments.filter((c) => c.body.includes('agent_run_report:v1'))).toHaveLength(1)
    expect(store.comments.filter((c) => c.body.includes('agent_retro_index:v1'))).toHaveLength(0)
    expect(store.comments.filter((c) => c.body.includes('CHATGPT_RETRO_CONTEXT_V1'))).toHaveLength(0)

    // Second invocation (rerun of the same completion command): run report
    // must not be duplicated (idempotent noop), retro index now succeeds,
    // and the context marker is created for the first time -- built from
    // the ACTUAL (now-complete) run report / retro index references.
    const secondResult = await completeAgentRun(args)
    expect(secondResult.status).toBe('ok')
    expect(secondResult.artifacts.agent_run_report.status).toBe('unchanged')
    expect(secondResult.artifacts.retro_index.status).toBe('created')
    expect(secondResult.artifacts.chatgpt_retro_context.status).toBe('created')
    expect(store.comments.filter((c) => c.body.includes('agent_run_report:v1'))).toHaveLength(1)
    expect(store.comments.filter((c) => c.body.includes('agent_retro_index:v1'))).toHaveLength(1)
    expect(store.comments.filter((c) => c.body.includes('CHATGPT_RETRO_CONTEXT_V1'))).toHaveLength(1)

    // Feed the retried invocation's resulting marker through the EXISTING
    // live resolver and assert it reaches `resolved`, referencing the ACTUAL
    // report/retro-index comment URLs and digests (not hardcoded fakes that
    // happen to collide with sequential fake-store IDs -- the P0-2 bug this
    // test guards against).
    const live = await resolveChatgptRetroContextLive(store, {
      repo: REPO,
      targetType: 'issue',
      targetNumber: PARENT_ISSUE,
      parentIssue: PARENT_ISSUE,
    })
    expect(live.comment_chain.status).toBe('resolved')
    expect(live.comment_chain.digest).toBe(secondResult.artifacts.chatgpt_retro_context.digest)
  })

  it('GIVEN a malformed existing agent_run_report marker WHEN completeAgentRun runs THEN agent_run_report is deterministically reported as failed without auto-repair, and the context marker is deferred (prerequisite_failed) rather than created against an incomplete run report reference', async () => {
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
    // retro_index still completes independently (best-effort, non-blocking).
    expect(result.artifacts.retro_index.status).toBe('created')
    // P0-2 fix: agent_run_report failed this invocation, so the context
    // marker must NOT be attempted (no real run report reference to cite).
    expect(result.artifacts.chatgpt_retro_context.status).toBe('failed')
    expect(result.artifacts.chatgpt_retro_context.reason_code).toBe('prerequisite_failed')
    expect(result.status).toBe('partial')
  })
})
