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

const OPTION_SPEC = {
  '--repo': { key: 'repo', required: true },
  '--target-number': { key: 'targetNumber', required: true },
  '--issue-number': { key: 'issueNumber', required: true },
  '--pr-number': { key: 'prNumber' },
  '--run-id': { key: 'runId', required: true },
  '--payload-markdown-file': { key: 'payloadMarkdownFile', required: true },
  '--dry-run': { key: 'dryRun', defaultValue: 'false' },
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

export async function upsertGithubMarkerCommentFromFile({
  repo,
  targetNumber,
  issueNumber,
  prNumber = null,
  runId,
  payloadMarkdownFile,
  dryRun = false,
  client = new GhCliIssueCommentsClient(),
}) {
  const payloadMarkdown = readFileSync(payloadMarkdownFile, 'utf-8')
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
