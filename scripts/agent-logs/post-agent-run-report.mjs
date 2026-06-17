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
  '--dry-run': { key: 'dryRun', defaultValue: 'false' },
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

function resolvePostingTarget(draft, report, issueNumberInput, prNumberInput) {
  const targetKind = draft.target.kind
  const targetId = draft.target.id
  const issueNumber = issueNumberInput ? Number(issueNumberInput) : targetKind === 'issue' ? targetId : null
  const prNumber = prNumberInput ? Number(prNumberInput) : targetKind === 'pull_request' ? targetId : null

  if (report.public_surface_kind === 'github_issue_comment' && targetKind !== 'issue') {
    throw runtimeError('agent_run_post.surface_mismatch', 'github_issue_comment requires an issue-target draft')
  }
  if (report.public_surface_kind === 'github_pr_comment' && targetKind !== 'pull_request') {
    throw runtimeError('agent_run_post.surface_mismatch', 'github_pr_comment requires a pull_request-target draft')
  }
  if (report.public_surface_kind === 'none') {
    throw runtimeError('agent_run_post.surface_mismatch', 'public_surface_kind none cannot be posted to GitHub comments')
  }
  if (!Number.isInteger(issueNumber) || issueNumber <= 0) {
    throw runtimeError('agent_run_post.issue_number', 'issue_number must resolve to a positive integer')
  }
  if (targetKind === 'pull_request' && (!Number.isInteger(prNumber) || prNumber <= 0)) {
    throw runtimeError('agent_run_post.pr_number', 'pr_number must resolve to a positive integer for pull request comments')
  }

  return {
    targetNumber: targetKind === 'issue' ? issueNumber : prNumber,
    issueNumber,
    prNumber: prNumber ?? null,
  }
}

export async function postAgentRunReport({
  draft,
  report,
  repo,
  issueNumber = null,
  prNumber = null,
  dryRun = false,
  client = new GhCliIssueCommentsClient(),
}) {
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
