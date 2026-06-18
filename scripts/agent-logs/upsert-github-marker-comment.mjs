#!/usr/bin/env node

import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { parseArgs, printCliError } from './lib/args.mjs'
import {
  GhCliIssueCommentsClient,
  GithubApiError,
  summarizeGithubApiError,
  upsertAgentRunReportComment,
} from './lib/github-comments.mjs'
import { validateMarkdownCandidate } from '../lib/agent-run-report-validation.mjs'

const OPTION_SPEC = {
  '--repo': { key: 'repo', required: true },
  '--target-number': { key: 'targetNumber', required: true },
  '--issue-number': { key: 'issueNumber', required: true },
  '--pr-number': { key: 'prNumber' },
  '--run-id': { key: 'runId', required: true },
  '--payload-markdown-file': { key: 'payloadMarkdownFile', required: true },
  '--dry-run': { key: 'dryRun', defaultValue: 'true' },
}

function parseBooleanFlag(value) {
  if (value === 'true') {
    return true
  }
  if (value === 'false') {
    return false
  }
  throw new Error('--dry-run must be true or false')
}

function loadValidatedPayloadMarkdown(payloadMarkdownFile) {
  const payloadMarkdown = readFileSync(payloadMarkdownFile, 'utf-8')
  const validation = validateMarkdownCandidate(payloadMarkdown, 'agent_run_report/v1')
  if (!validation.valid) {
    const summary = validation.errors
      .slice(0, 3)
      .map((error) => `${error.code} at ${error.path}`)
      .join('; ')
    throw new Error(summary || 'payload markdown failed canonical validation')
  }
  return payloadMarkdown
}

export async function upsertGithubMarkerCommentFromFile({
  repo,
  targetNumber,
  issueNumber,
  prNumber = null,
  runId,
  payloadMarkdownFile,
  dryRun = true,
  client = new GhCliIssueCommentsClient(),
}) {
  if (!dryRun) {
    throw new Error('live posting is disabled for upsert-github-marker-comment; use post-agent-run-report.mjs')
  }
  const payloadMarkdown = loadValidatedPayloadMarkdown(payloadMarkdownFile)
  return upsertAgentRunReportComment(client, {
    repo,
    targetNumber: Number(targetNumber),
    issueNumber: Number(issueNumber),
    prNumber: prNumber === null ? null : Number(prNumber),
    runId,
    payloadMarkdown,
    dryRun,
  })
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  try {
    const result = await upsertGithubMarkerCommentFromFile({
      repo: options.repo,
      targetNumber: options.targetNumber,
      issueNumber: options.issueNumber,
      prNumber: options.prNumber ?? null,
      runId: options.runId,
      payloadMarkdownFile: options.payloadMarkdownFile,
      dryRun: parseBooleanFlag(options.dryRun),
    })
    console.log(JSON.stringify(result))
  } catch (error) {
    if (error instanceof GithubApiError) {
      console.error(JSON.stringify(summarizeGithubApiError(error)))
      process.exit(1)
    }
    process.exit(printCliError('upsert-github-marker-comment', error))
  }
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  main().catch((error) => {
    if (error instanceof GithubApiError) {
      console.error(JSON.stringify(summarizeGithubApiError(error)))
      process.exit(1)
    }
    process.exit(printCliError('upsert-github-marker-comment', error))
  })
}
