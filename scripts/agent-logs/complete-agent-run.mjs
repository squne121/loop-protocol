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
 */

import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { parseArgs, printCliError, runtimeError, CliError } from './lib/args.mjs'
import { loadDraft } from './lib/draft.mjs'
import { GhCliIssueCommentsClient, GithubApiError, summarizeGithubApiError } from './lib/github-comments.mjs'
import { postAgentRunReport } from './post-agent-run-report.mjs'
import { updateRetroIndex } from './update-retro-index.mjs'
import { upsertChatgptRetroContextComment } from './lib/chatgpt-retro-context-marker-helper.mjs'

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
  '--chatgpt-payload-markdown-file': { key: 'chatgptPayloadMarkdownFile', required: true },
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
      rest: { digest: upsert.canonical_index_digest ?? null },
    })
  })

  const chatgptRetroContext = await runStep('chatgpt_retro_context', async () => {
    const upsert = await upsertChatgptRetroContextComment(chatgptClient, {
      repo,
      targetType: chatgptTarget.targetType,
      targetNumber: chatgptTarget.targetNumber,
      parentIssue,
      payloadMarkdown: chatgptTarget.payloadMarkdown,
      dryRun,
      expectedSupersedesDigest: chatgptTarget.expectedSupersedesDigest ?? null,
    })
    return statusFromAction('chatgpt_retro_context', upsert.action, {
      comment_id: upsert.comment_id,
      comment_url: upsert.comment_url,
      rest: { digest: upsert.digest ?? null },
    })
  })

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
  let chatgptPayloadMarkdown
  try {
    chatgptPayloadMarkdown = readFileSync(options.chatgptPayloadMarkdownFile, 'utf-8')
  } catch {
    throw runtimeError('agent_run_complete.chatgpt_payload_read_failed', 'chatgpt payload markdown file could not be read')
  }

  const result = await completeAgentRun({
    repo: options.repo,
    draft,
    report,
    parentIssue: Number(options.parentIssue),
    chatgptTarget: {
      targetType: options.chatgptTargetType,
      targetNumber: Number(options.chatgptTargetNumber),
      payloadMarkdown: chatgptPayloadMarkdown,
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
