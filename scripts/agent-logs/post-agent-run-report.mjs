#!/usr/bin/env node

import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { parseArgs, printCliError, runtimeError } from './lib/args.mjs'
import { loadDraft } from './lib/draft.mjs'
import { renderValidatedPublicMarkdown, validateFinalReport } from './lib/validate-final-report.mjs'
import {
  GhCliIssueCommentsClient,
  GithubApiError,
  summarizeGithubApiError,
  upsertAgentRunReportComment,
} from './lib/github-comments.mjs'

const OPTION_SPEC = {
  '--draft': { key: 'draftPath', required: true },
  '--report': { key: 'reportPath', required: true },
  '--repo': { key: 'repo', required: true },
  '--issue-number': { key: 'issueNumber' },
  '--pr-number': { key: 'prNumber' },
  '--dry-run': { key: 'dryRun', defaultValue: 'true' },
  '--confirm-live': { key: 'confirmLive', defaultValue: 'false' },
}

function parseBooleanFlag(value, optionName) {
  if (value === 'true') {
    return true
  }
  if (value === 'false') {
    return false
  }
  throw runtimeError('agent_run_post.invalid_flag', `${optionName} must be true or false`)
}

function loadReport(reportPath) {
  try {
    return JSON.parse(readFileSync(reportPath, 'utf-8'))
  } catch {
    throw runtimeError('agent_run_post.report_read_failed', 'report file could not be read as JSON')
  }
}

function requireAllowedRepo(repo) {
  if (repo !== 'squne121/loop-protocol') {
    throw runtimeError('agent_run_post.repo_not_allowed', 'repo must match the allowlisted repository')
  }
}

function resolvePostingTarget(draft, report, issueNumberInput, prNumberInput) {
  const targetKind = draft.target.kind
  const targetId = draft.target.id
  const issueNumberOverride = issueNumberInput === null ? null : Number(issueNumberInput)
  const prNumberOverride = prNumberInput === null ? null : Number(prNumberInput)

  if (report.public_surface_kind === 'github_issue_comment' && targetKind !== 'issue') {
    throw runtimeError('agent_run_post.surface_mismatch', 'github_issue_comment requires an issue-target draft')
  }
  if (report.public_surface_kind === 'github_pr_comment' && targetKind !== 'pull_request') {
    throw runtimeError('agent_run_post.surface_mismatch', 'github_pr_comment requires a pull_request-target draft')
  }
  if (report.public_surface_kind === 'none') {
    throw runtimeError('agent_run_post.surface_mismatch', 'public_surface_kind none cannot be posted to GitHub comments')
  }

  if (targetKind === 'issue') {
    if (issueNumberOverride !== null && issueNumberOverride !== targetId) {
      throw runtimeError('agent_run_post.issue_number_mismatch', 'issue-target override must match draft.target.id')
    }
    if (prNumberOverride !== null) {
      throw runtimeError('agent_run_post.pr_number_forbidden', 'issue-target draft does not allow --pr-number overrides')
    }

    return {
      targetNumber: targetId,
      issueNumber: targetId,
      prNumber: null,
    }
  }

  if (issueNumberOverride !== null && issueNumberOverride !== targetId) {
    throw runtimeError('agent_run_post.issue_number_mismatch', 'pull_request-target issue number must match draft.target.id')
  }
  if (prNumberOverride !== null && prNumberOverride !== targetId) {
    throw runtimeError('agent_run_post.pr_number_mismatch', 'pull_request-target pr number must match draft.target.id')
  }

  return {
    // Pull request comments use the issue-comments endpoint with the PR number.
    targetNumber: targetId,
    issueNumber: targetId,
    prNumber: targetId,
  }
}

export async function postAgentRunReport({
  draft,
  report,
  repo,
  issueNumber = null,
  prNumber = null,
  dryRun = true,
  confirmLive = false,
  client = new GhCliIssueCommentsClient(),
}) {
  requireAllowedRepo(repo)
  if (!dryRun && !confirmLive) {
    throw runtimeError('agent_run_post.live_confirmation_required', 'live posting requires --dry-run false and --confirm-live true')
  }
  validateFinalReport(report)
  const payloadMarkdown = renderValidatedPublicMarkdown(report)
  const target = resolvePostingTarget(draft, report, issueNumber, prNumber)
  return upsertAgentRunReportComment(client, {
    repo,
    targetNumber: target.targetNumber,
    issueNumber: target.issueNumber,
    prNumber: target.prNumber,
    runId: draft.run_id,
    payloadMarkdown,
    dryRun,
  })
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  const dryRun = parseBooleanFlag(options.dryRun, '--dry-run')
  const confirmLive = parseBooleanFlag(options.confirmLive, '--confirm-live')
  const draft = await loadDraft(options.draftPath)
  const report = loadReport(options.reportPath)

  try {
    const result = await postAgentRunReport({
      draft,
      report,
      repo: options.repo,
      issueNumber: options.issueNumber ?? null,
      prNumber: options.prNumber ?? null,
      dryRun,
      confirmLive,
    })
    console.log(JSON.stringify(result))
  } catch (error) {
    const summary = summarizeGithubApiError(error)
    if (summary) {
      console.error(JSON.stringify(summary))
      process.exit(1)
    }
    throw error
  }
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  main().catch((error) => {
    if (error instanceof GithubApiError) {
      console.error(JSON.stringify(summarizeGithubApiError(error)))
      process.exit(1)
    }
    process.exit(printCliError('agent-run:post', error))
  })
}
