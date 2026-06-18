#!/usr/bin/env node

import { spawnSync } from 'child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'fs'
import { dirname } from 'path'
import { fileURLToPath } from 'url'

import { parseArgs, printCliError, runtimeError } from './lib/args.mjs'
import {
  buildRetroIndex,
  detectSchemaMigrationRequirement,
  RETRO_INDEX_ALGORITHM,
  sha256Digest,
} from './lib/retro-index-builder.mjs'
import {
  GhCliIssueCommentsClient,
  GithubApiError,
  parseBooleanFlag,
  summarizeGithubApiError,
  upsertRetroIndexComment,
} from './lib/retro-index-comment-helper.mjs'
import { listAllIssueComments } from './lib/github-comments.mjs'
import { renderPublicMarkdown } from '../lib/agent-run-report-validation.mjs'

const OPTION_SPEC = {
  '--repo': { key: 'repo' },
  '--parent-issue': { key: 'parentIssue' },
  '--dry-run': { key: 'dryRun', defaultValue: 'true' },
  '--confirm-live': { key: 'confirmLive', defaultValue: 'false' },
  '--artifact-json-out': { key: 'artifactJsonOut' },
  '--summary-json-out': { key: 'summaryJsonOut' },
  '--verify-artifact-json': { key: 'verifyArtifactJson' },
  '--summary-json-in': { key: 'summaryJsonIn' },
  '--expected-canonical-digest': { key: 'expectedCanonicalDigest' },
  '--expected-source-comment-set-digest': { key: 'expectedSourceCommentSetDigest' },
}

function ensureAllowedRepo(repo) {
  if (repo !== 'squne121/loop-protocol') {
    throw runtimeError('retro_index.repo_not_allowed', 'repo must match the allowlisted repository')
  }
}

function runGhJson(args) {
  const result = spawnSync('gh', args, {
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  if (result.status !== 0) {
    throw runtimeError('retro_index.gh_failed', 'gh command failed while collecting retro index inputs')
  }
  return JSON.parse(result.stdout)
}

function fetchIssue(repo, issueNumber) {
  return runGhJson([
    'api',
    '-H', 'Accept: application/vnd.github+json',
    '-H', 'X-GitHub-Api-Version: 2022-11-28',
    `repos/${repo}/issues/${issueNumber}`,
  ])
}

function fetchPullRequest(repo, pullNumber) {
  return runGhJson([
    'api',
    '-H', 'Accept: application/vnd.github+json',
    '-H', 'X-GitHub-Api-Version: 2022-11-28',
    `repos/${repo}/pulls/${pullNumber}`,
  ])
}

function fetchAssociatedPullRequests(repo, commitSha) {
  if (typeof commitSha !== 'string' || commitSha.length === 0) {
    return []
  }
  return runGhJson([
    'api',
    '-H', 'Accept: application/vnd.github+json',
    '-H', 'X-GitHub-Api-Version: 2022-11-28',
    `repos/${repo}/commits/${commitSha}/pulls`,
  ])
}

function parseChecklistIssueNumbers(body) {
  const numbers = new Set()
  for (const match of (body ?? '').matchAll(/^- \[[ xX]\] #([0-9]+)/gmu)) {
    numbers.add(Number(match[1]))
  }
  return [...numbers]
}

function parsePullRequestNumbers(text) {
  const numbers = new Set()
  for (const match of (text ?? '').matchAll(/(?:\bPR\b|pull(?:\s+request)?)\s*#([0-9]+)/giu)) {
    numbers.add(Number(match[1]))
  }
  return [...numbers]
}

function issueCommentShape(comment, linkedPrHints, linkedIssueHints, branchHint) {
  return {
    html_url: comment.html_url,
    body: comment.body,
    linkedPrHints,
    linkedIssueHints,
    branchHint,
  }
}

async function collectSourceComments({ repo, parentIssue, issueCommentClient }) {
  const parent = fetchIssue(repo, parentIssue)
  const childIssues = parseChecklistIssueNumbers(parent.body)
  const childIssueObjects = childIssues.map((number) => fetchIssue(repo, number))
  const pullNumbers = new Set()
  const prMetadataByNumber = new Map()
  const associatedPrByMergeSha = new Map()
  const sourceComments = []

  for (const issue of childIssueObjects) {
    for (const number of parsePullRequestNumbers(issue.body)) {
      pullNumbers.add(number)
    }
    for (const number of parsePullRequestNumbers(issue.title)) {
      pullNumbers.add(number)
    }
  }

  for (const issue of childIssueObjects) {
    const linkedPrHints = parsePullRequestNumbers(issue.body)
    const issueComments = await listAllIssueComments(issueCommentClient, {
      repo,
      issueNumber: issue.number,
    })
    for (const comment of issueComments) {
      sourceComments.push(issueCommentShape(comment, linkedPrHints, [issue.number], null))
    }
  }

  for (const pullNumber of pullNumbers) {
    const pull = fetchPullRequest(repo, pullNumber)
    prMetadataByNumber.set(pull.number, {
      number: pull.number,
      body: pull.body ?? '',
      mergeSha: pull.merge_commit_sha ?? '',
      headRefName: pull.head?.ref ?? '',
    })
    const associated = fetchAssociatedPullRequests(repo, pull.merge_commit_sha ?? '')
    for (const pr of associated) {
      if (typeof pr?.number === 'number') {
        associatedPrByMergeSha.set(pull.merge_commit_sha, pr.number)
      }
    }
    const pullComments = await listAllIssueComments(issueCommentClient, {
      repo,
      issueNumber: pull.number,
    })
    const linkedIssueHints = parseChecklistIssueNumbers(parent.body).filter((issueNumber) => (pull.body ?? '').includes(`#${issueNumber}`))
    for (const comment of pullComments) {
      sourceComments.push(issueCommentShape(comment, [pull.number], linkedIssueHints, pull.head?.ref ?? null))
    }
  }

  return {
    parent,
    childIssues,
    sourceComments,
    prMetadataByNumber,
    associatedPrByMergeSha,
  }
}

function writeJsonFile(filePath, payload) {
  mkdirSync(dirname(filePath), { recursive: true })
  writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, 'utf-8')
}

function readJsonFile(filePath, code) {
  try {
    return JSON.parse(readFileSync(filePath, 'utf-8'))
  } catch {
    throw runtimeError(code, `failed to parse JSON file: ${filePath}`)
  }
}

function assertDigest(value, code, label) {
  if (typeof value !== 'string' || !/^sha256:[a-f0-9]{64}$/iu.test(value)) {
    throw runtimeError(code, `${label} must be a sha256:<64-hex> digest`)
  }
}

export function verifyRetroIndexArtifact({
  artifactJsonPath,
  summaryJsonPath = null,
  expectedCanonicalDigest = null,
  expectedSourceCommentSetDigest = null,
}) {
  const artifactPayload = readJsonFile(artifactJsonPath, 'retro_index.artifact_json_invalid')
  const summary = summaryJsonPath
    ? readJsonFile(summaryJsonPath, 'retro_index.summary_json_invalid')
    : {}
  const computedCanonicalDigest = sha256Digest(JSON.stringify(artifactPayload, null, 2))
  const summaryCanonicalDigest = summary.canonical_index_digest ?? null
  const summarySourceCommentSetDigest = summary.source_comment_set_digest ?? null
  const canonicalDigest = expectedCanonicalDigest ?? summaryCanonicalDigest
  const sourceCommentSetDigest = expectedSourceCommentSetDigest ?? summarySourceCommentSetDigest

  if (summaryCanonicalDigest !== null) {
    assertDigest(summaryCanonicalDigest, 'retro_index.summary_canonical_digest_invalid', 'summary canonical digest')
  }
  if (summarySourceCommentSetDigest !== null) {
    assertDigest(summarySourceCommentSetDigest, 'retro_index.summary_source_set_digest_invalid', 'summary source-comment-set digest')
  }
  if (!canonicalDigest) {
    throw runtimeError('retro_index.expected_canonical_digest_missing', 'canonical digest is required for artifact verification')
  }
  assertDigest(canonicalDigest, 'retro_index.expected_canonical_digest_invalid', 'expected canonical digest')
  if (sourceCommentSetDigest !== null) {
    assertDigest(sourceCommentSetDigest, 'retro_index.expected_source_set_digest_invalid', 'expected source-comment-set digest')
  }
  if (summaryCanonicalDigest && expectedCanonicalDigest && summaryCanonicalDigest !== expectedCanonicalDigest) {
    throw runtimeError('retro_index.summary_canonical_digest_mismatch', 'summary canonical digest does not match the expected digest')
  }
  if (summarySourceCommentSetDigest && expectedSourceCommentSetDigest && summarySourceCommentSetDigest !== expectedSourceCommentSetDigest) {
    throw runtimeError('retro_index.summary_source_set_digest_mismatch', 'summary source-comment-set digest does not match the expected digest')
  }
  if (computedCanonicalDigest !== canonicalDigest) {
    throw runtimeError('retro_index.canonical_digest_mismatch', 'artifact JSON digest does not match the expected canonical digest')
  }
  return {
    status: 'ok',
    entry_count: Array.isArray(artifactPayload.entries) ? artifactPayload.entries.length : null,
    orphan_count: Array.isArray(artifactPayload.orphan_reports) ? artifactPayload.orphan_reports.length : null,
    ambiguous_count: Array.isArray(artifactPayload.ambiguous_links) ? artifactPayload.ambiguous_links.length : null,
    computedCanonicalDigest,
    canonical_index_digest: canonicalDigest,
    source_comment_set_digest: sourceCommentSetDigest,
  }
}

export async function updateRetroIndex({
  repo,
  parentIssue,
  dryRun = true,
  confirmLive = false,
  issueCommentClient = new GhCliIssueCommentsClient(),
  sourceBundle = null,
}) {
  ensureAllowedRepo(repo)
  if (!dryRun && !confirmLive) {
    throw runtimeError('retro_index.live_confirmation_required', 'live posting requires --dry-run false and --confirm-live true')
  }

  const bundle = sourceBundle ?? await collectSourceComments({
    repo,
    parentIssue,
    issueCommentClient,
  })

  const built = buildRetroIndex({
    sourceComments: bundle.sourceComments,
    parentIssue,
    prMetadataByNumber: bundle.prMetadataByNumber,
    associatedPrByMergeSha: bundle.associatedPrByMergeSha,
    parentChildIssueNumbers: bundle.childIssues,
  })

  const schemaMigration = detectSchemaMigrationRequirement(built.index)
  if (schemaMigration) {
    return {
      status: 'blocked',
      reason_code: 'schema_migration_required',
      message: schemaMigration.reason,
      summary: built.summary,
    }
  }

  const payloadMarkdown = renderPublicMarkdown(built.index)
  const upsert = await upsertRetroIndexComment(issueCommentClient, {
    repo,
    parentIssue,
    algorithm: RETRO_INDEX_ALGORITHM,
    payloadMarkdown,
    canonicalIndexDigest: built.canonicalIndexDigest,
    sourceCommentSetDigest: built.sourceCommentSetDigest,
    dryRun,
  })

  return {
    status: built.index.generation_verdict === 'blocked' ? 'blocked' : 'ok',
    reason_code: built.index.generation_verdict === 'blocked' ? 'report_blocked' : null,
    action: upsert.action,
    comment_url: upsert.comment_url,
    comment_id: upsert.comment_id,
    parent_issue: parentIssue,
    canonical_index_digest: built.canonicalIndexDigest,
    source_comment_set_digest: built.sourceCommentSetDigest,
    summary: built.summary,
    index: built.index,
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  if (options.verifyArtifactJson) {
    const verification = verifyRetroIndexArtifact({
      artifactJsonPath: options.verifyArtifactJson,
      summaryJsonPath: options.summaryJsonIn ?? null,
      expectedCanonicalDigest: options.expectedCanonicalDigest ?? null,
      expectedSourceCommentSetDigest: options.expectedSourceCommentSetDigest ?? null,
    })
    console.log(JSON.stringify(verification))
    return
  }
  if (!options.repo) {
    throw runtimeError('cli.required_option', 'missing required option: --repo')
  }
  if (!options.parentIssue) {
    throw runtimeError('cli.required_option', 'missing required option: --parent-issue')
  }
  const result = await updateRetroIndex({
    repo: options.repo,
    parentIssue: Number(options.parentIssue),
    dryRun: parseBooleanFlag(options.dryRun, '--dry-run'),
    confirmLive: parseBooleanFlag(options.confirmLive, '--confirm-live'),
  })

  if (options.artifactJsonOut) {
    writeJsonFile(options.artifactJsonOut, result.index ?? {})
  }
  if (options.summaryJsonOut) {
    writeJsonFile(options.summaryJsonOut, result.summary)
  }

  console.log(JSON.stringify(result.summary))
  if (result.status === 'blocked') {
    process.exit(1)
  }
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  main().catch((error) => {
    if (error instanceof GithubApiError) {
      console.error(JSON.stringify(summarizeGithubApiError(error)))
      process.exit(1)
    }
    process.exit(printCliError('agent-retro-index:update', error))
  })
}
