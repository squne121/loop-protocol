#!/usr/bin/env node

/**
 * complete-agent-run.mjs
 *
 * #2489 (#1939 Workstream 2): thin idempotent completion transaction
 * coordinator. Calls the three existing exported upsert functions
 * (postAgentRunReport / updateRetroIndex / upsertChatgptRetroContextComment)
 * in sequence and reports each artifact's outcome as
 * created|updated|unchanged|failed.
 *
 * This coordinator does NOT reimplement upsert/duplicate-detection logic —
 * it only orchestrates the existing idempotent operations. It does NOT add
 * a DB, transaction log, or 2-phase commit: each operation is independently
 * idempotent, so re-running the same coordinator invocation converges even
 * after a partial failure (human_context_comment P1-3 / P1-4).
 *
 * Each step is attempted regardless of a prior step's outcome so that a
 * partial failure (e.g. run report succeeds, retro index fails) does not
 * block the remaining artifacts from completing on the same invocation or a
 * rerun.
 *
 * #2489 P0-2 fix (PR #2496 OWNER review issuecomment-5546874328): the
 * chatgpt_retro_context payload is built from the ACTUAL agent_run_report /
 * retro_index upsert results (their real comment_url / digest /
 * canonical_index_digest / source_comment_set_digest) via
 * buildChatgptRetroContextPayloadFromResults(), never from a pre-generated
 * markdown file. If either prerequisite step failed (or has no real comment
 * reference yet, e.g. a dry-run first-create), the chatgpt_retro_context
 * step is not attempted at all this invocation -- it is reported as
 * `failed` with `reason_code: prerequisite_failed` so a rerun of the same
 * completion command builds the marker once the prerequisites are real.
 */

import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { parseArgs, printCliError, runtimeError, CliError } from './lib/args.mjs'
import { loadDraft } from './lib/draft.mjs'
import { GhCliIssueCommentsClient, GithubApiError, summarizeGithubApiError } from './lib/github-comments.mjs'
import { extractPayloadFromMarkdown, renderPublicMarkdown } from '../lib/agent-run-report-validation.mjs'
import { postAgentRunReport } from './post-agent-run-report.mjs'
import { updateRetroIndex } from './update-retro-index.mjs'
import {
  buildChatgptRetroContextPayloadFromResults,
  upsertChatgptRetroContextComment,
} from './lib/chatgpt-retro-context-marker-helper.mjs'

const OPTION_SPEC = {
  '--repo': { key: 'repo', required: true },
  '--draft': { key: 'draftPath', required: true },
  '--report': { key: 'reportPath', required: true },
  '--parent-issue': { key: 'parentIssue', required: true },
  '--issue-number': { key: 'issueNumber' },
  '--pr-number': { key: 'prNumber' },
  '--additional-pull-number': { key: 'additionalPullNumbers', multiple: true },
  '--chatgpt-target-type': { key: 'chatgptTargetType', required: true },
  '--chatgpt-target-number': { key: 'chatgptTargetNumber', required: true },
  // #2489 P0-2 fix: no longer required / no longer authoritative for
  // comment URLs or digests. When present, it is read only as a
  // supplemental template supplying `marker_kind` / `safety` /
  // `prerequisites` (all fixed pilot-governance metadata); the actual
  // refs/canonicalization always come from the live postAgentRunReport /
  // updateRetroIndex results.
  '--chatgpt-payload-markdown-file': { key: 'chatgptPayloadMarkdownFile' },
  '--chatgpt-expected-supersedes-digest': { key: 'chatgptExpectedSupersedesDigest' },
  '--dry-run': { key: 'dryRun', defaultValue: 'true' },
  '--confirm-live': { key: 'confirmLive', defaultValue: 'false' },
}

// Maps the existing upsert functions' `action` values onto the coordinator's
// per-artifact status vocabulary (Issue #2489 Current Validated Scope).
const ACTION_TO_STATUS = {
  create: 'created',
  update: 'updated',
  supersede: 'updated',
  noop: 'unchanged',
}

function parseBooleanFlag(value, optionName) {
  if (value === 'true') return true
  if (value === 'false') return false
  throw runtimeError('agent_run_complete.invalid_flag', `${optionName} must be true or false`)
}

/**
 * Runs a single completion-transaction step, normalizing both thrown
 * CliError-style failures and non-'ok' status results (e.g.
 * updateRetroIndex's `status: blocked`) into a single
 * created|updated|unchanged|failed vocabulary. Never throws.
 */
async function runStep(name, fn) {
  try {
    return await fn()
  } catch (error) {
    const reasonCode = error instanceof CliError ? error.code : null
    return {
      artifact: name,
      status: 'failed',
      reason_code: reasonCode,
      comment_id: null,
      comment_url: null,
      error_message: error instanceof Error ? error.message : String(error),
    }
  }
}

function statusFromAction(name, action, extra = {}) {
  const status = ACTION_TO_STATUS[action] ?? 'failed'
  return {
    artifact: name,
    status,
    reason_code: status === 'failed' ? `unknown_action:${action}` : null,
    comment_id: extra.comment_id ?? null,
    comment_url: extra.comment_url ?? null,
    ...extra.rest,
  }
}

/**
 * Idempotent completion transaction: upserts run report, retro index, and
 * chatgpt retro context comment in sequence. Each artifact result uses the
 * created|updated|unchanged|failed vocabulary. Never throws — failures are
 * captured per-artifact so callers can inspect `status` and safely re-run
 * the same invocation to converge (idempotency is provided by the
 * underlying upsert operations, not by this coordinator).
 *
 * @returns {Promise<{status: 'ok'|'partial'|'failed', artifacts: {agent_run_report: object, retro_index: object, chatgpt_retro_context: object}}>}
 */
export async function completeAgentRun({
  repo,
  draft,
  report,
  parentIssue,
  chatgptTarget,
  issueNumber = null,
  prNumber = null,
  dryRun = true,
  confirmLive = false,
  reportClient = new GhCliIssueCommentsClient(),
  retroIndexClient = new GhCliIssueCommentsClient(),
  chatgptClient = new GhCliIssueCommentsClient(),
  additionalPullNumbers = [],
  sourceBundle = null,
  artifactBundle = null,
}) {
  const agentRunReport = await runStep('agent_run_report', async () => {
    const upsert = await postAgentRunReport({
      draft,
      report,
      repo,
      issueNumber,
      prNumber,
      dryRun,
      confirmLive,
      client: reportClient,
    })
    return statusFromAction('agent_run_report', upsert.action, {
      comment_id: upsert.comment_id,
      comment_url: upsert.comment_url,
      rest: { digest: upsert.sha256 ?? null },
    })
  })

  const retroIndex = await runStep('retro_index', async () => {
    const upsert = await updateRetroIndex({
      repo,
      parentIssue,
      dryRun,
      confirmLive,
      issueCommentClient: retroIndexClient,
      sourceBundle,
      artifactBundle,
      additionalPullNumbers,
    })
    if (upsert.status !== 'ok') {
      return {
        artifact: 'retro_index',
        status: 'failed',
        reason_code: upsert.reason_code ?? 'retro_index_blocked',
        comment_id: upsert.comment_id ?? null,
        comment_url: upsert.comment_url ?? null,
      }
    }
    return statusFromAction('retro_index', upsert.action, {
      comment_id: upsert.comment_id,
      comment_url: upsert.comment_url,
      rest: {
        digest: upsert.canonical_index_digest ?? null,
        source_set_digest: upsert.source_comment_set_digest ?? null,
      },
    })
  })

  // #2489 P0-2 fix: the chatgpt_retro_context marker must reference the
  // ACTUAL agent_run_report / retro_index comments -- if either prerequisite
  // did not succeed with a real comment reference this invocation (failed,
  // or a dry-run first-create with no comment_url yet), do not attempt the
  // upsert at all. A rerun of the same completion command will pick it up
  // once both prerequisites have real references (idempotency of the first
  // two steps means this never duplicates them).
  const prerequisitesReady = agentRunReport.status !== 'failed'
    && retroIndex.status !== 'failed'
    && Boolean(agentRunReport.comment_url)
    && Boolean(retroIndex.comment_url)

  const chatgptRetroContext = prerequisitesReady
    ? await runStep('chatgpt_retro_context', async () => {
        const payload = buildChatgptRetroContextPayloadFromResults({
          repo,
          targetType: chatgptTarget.targetType,
          targetNumber: chatgptTarget.targetNumber,
          parentIssue,
          agentRunReportDigest: agentRunReport.digest,
          agentRunReportCommentUrl: agentRunReport.comment_url,
          retroIndexDigest: retroIndex.digest,
          retroIndexSourceSetDigest: retroIndex.source_set_digest,
          retroIndexCommentUrl: retroIndex.comment_url,
          // Deterministic across reruns of the SAME completion command: derived
          // from the run's own start time (part of the input draft), not
          // wall-clock "now". Using `new Date().toISOString()` here would make
          // the payload digest differ on every rerun even when nothing about
          // the underlying run/report/index actually changed, breaking the
          // idempotent noop convergence this coordinator depends on.
          createdAt: draft.started_at,
          templateOverrides: chatgptTarget.templateOverrides ?? null,
        })
        const payloadMarkdown = renderPublicMarkdown(payload)
        const upsert = await upsertChatgptRetroContextComment(chatgptClient, {
          repo,
          targetType: chatgptTarget.targetType,
          targetNumber: chatgptTarget.targetNumber,
          parentIssue,
          payloadMarkdown,
          dryRun,
          expectedSupersedesDigest: chatgptTarget.expectedSupersedesDigest ?? null,
        })
        return statusFromAction('chatgpt_retro_context', upsert.action, {
          comment_id: upsert.comment_id,
          comment_url: upsert.comment_url,
          rest: { digest: upsert.digest ?? null },
        })
      })
    : {
        artifact: 'chatgpt_retro_context',
        status: 'failed',
        reason_code: 'prerequisite_failed',
        comment_id: null,
        comment_url: null,
      }

  const artifacts = {
    agent_run_report: agentRunReport,
    retro_index: retroIndex,
    chatgpt_retro_context: chatgptRetroContext,
  }

  const statuses = Object.values(artifacts).map((entry) => entry.status)
  const failedCount = statuses.filter((status) => status === 'failed').length
  let overallStatus
  if (failedCount === 0) {
    overallStatus = 'ok'
  } else if (failedCount === statuses.length) {
    overallStatus = 'failed'
  } else {
    overallStatus = 'partial'
  }

  return { status: overallStatus, artifacts }
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  const dryRun = parseBooleanFlag(options.dryRun, '--dry-run')
  const confirmLive = parseBooleanFlag(options.confirmLive, '--confirm-live')

  const draft = await loadDraft(options.draftPath)
  let report
  try {
    report = JSON.parse(readFileSync(options.reportPath, 'utf-8'))
  } catch {
    throw runtimeError('agent_run_complete.report_read_failed', 'report file could not be read as JSON')
  }
  // #2489 P0-2 fix: --chatgpt-payload-markdown-file is now optional and, when
  // present, supplies only supplemental template fields (marker_kind /
  // safety / prerequisites), never comment URLs or digests (those always
  // come from the actual postAgentRunReport / updateRetroIndex results built
  // inside completeAgentRun()). A read/parse failure here is non-fatal: it
  // only means the fixed defaults (CHATGPT_RETRO_CONTEXT_SAFETY_DEFAULTS /
  // CHATGPT_RETRO_CONTEXT_PREREQUISITES_DEFAULTS) are used instead.
  let chatgptTemplateOverrides = null
  if (options.chatgptPayloadMarkdownFile) {
    try {
      const templateMarkdown = readFileSync(options.chatgptPayloadMarkdownFile, 'utf-8')
      const extraction = extractPayloadFromMarkdown(templateMarkdown, 'chatgpt_retro_context_marker/v1')
      if (extraction.ok) {
        chatgptTemplateOverrides = {
          marker_kind: extraction.payload.marker_kind,
          safety: extraction.payload.safety,
          prerequisites: extraction.payload.prerequisites,
        }
      } else {
        console.error(
          `[agent-run:complete] warn: --chatgpt-payload-markdown-file could not be parsed as a chatgpt_retro_context_marker/v1 template (${extraction.error?.code ?? 'unknown'}); falling back to fixed defaults`,
        )
      }
    } catch {
      console.error(
        '[agent-run:complete] warn: --chatgpt-payload-markdown-file could not be read; falling back to fixed defaults',
      )
    }
  }

  const result = await completeAgentRun({
    repo: options.repo,
    draft,
    report,
    parentIssue: Number(options.parentIssue),
    chatgptTarget: {
      targetType: options.chatgptTargetType,
      targetNumber: Number(options.chatgptTargetNumber),
      templateOverrides: chatgptTemplateOverrides,
      expectedSupersedesDigest: options.chatgptExpectedSupersedesDigest ?? null,
    },
    issueNumber: options.issueNumber ?? null,
    prNumber: options.prNumber ?? null,
    dryRun,
    confirmLive,
    additionalPullNumbers: options.additionalPullNumbers ?? [],
  })

  console.log(JSON.stringify(result))
  if (result.status !== 'ok') {
    process.exitCode = 1
  }
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  main().catch((error) => {
    if (error instanceof GithubApiError) {
      console.error(JSON.stringify(summarizeGithubApiError(error)))
      process.exit(1)
    }
    process.exit(printCliError('agent-run:complete', error))
  })
}
