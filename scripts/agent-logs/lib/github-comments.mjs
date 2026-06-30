import { createHash } from 'crypto'
import { spawnSync } from 'child_process'
import { Buffer } from 'buffer'

import { runtimeError } from './args.mjs'

export const MAX_GITHUB_COMMENT_BYTES = 65536
export const OWNERSHIP_MARKER_PATTERN = /^<!--\s*agent_run_report:v1 repo=(?<repo>[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+) issue=(?<issue>[0-9]+) pr=(?<pr>[0-9]+|null) run_id=(?<runId>[A-Za-z0-9._:-]+)\s*-->$/u
export const DIGEST_MARKER_PATTERN = /^<!--\s*agent_run_report_digest:v1 sha256=(?<digest>[a-f0-9]{64})\s*-->$/iu

const OWNERSHIP_MARKER_SCAN = /<!--\s*agent_run_report:v1\b(?!\s+(?:start|end)\b)[^>]*-->/giu
const DIGEST_MARKER_SCAN = /<!--\s*agent_run_report_digest:v1\b[^>]*-->/giu

function countRegexMatches(text, pattern) {
  return Array.from(text.matchAll(pattern)).length
}

function firstNonEmptyLine(text) {
  return text
    .split('\n')
    .map((line) => line.trim())
    .find((line) => line.length > 0) ?? ''
}

function markerSafeRunId(runId) {
  return typeof runId === 'string' && /^[A-Za-z0-9._:-]+$/u.test(runId)
}

function markerSafeRepo(repo) {
  return typeof repo === 'string' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/u.test(repo)
}

function normalizeOptionalNumber(value, fieldName) {
  if (value === null || value === undefined) {
    return null
  }
  const numberValue = Number(value)
  if (!Number.isInteger(numberValue) || numberValue <= 0) {
    throw runtimeError(`github_comments.${fieldName}`, `${fieldName} must be a positive integer or null`)
  }
  return numberValue
}

export function buildOwnershipTuple({ repo, issueNumber, prNumber, runId }) {
  if (!markerSafeRepo(repo)) {
    throw runtimeError('github_comments.repo', 'repo must be an owner/name string')
  }
  if (!markerSafeRunId(runId)) {
    throw runtimeError('github_comments.run_id', 'run_id must be marker-safe (A-Z a-z 0-9 . _ : -)')
  }

  return {
    repo,
    issueNumber: normalizeOptionalNumber(issueNumber, 'issue_number'),
    prNumber: normalizeOptionalNumber(prNumber, 'pr_number'),
    runId,
  }
}

export function formatOwnershipMarker(input) {
  const ownership = buildOwnershipTuple(input)
  if (ownership.issueNumber === null) {
    throw runtimeError('github_comments.issue_number', 'issue_number is required for ownership markers')
  }
  return `<!-- agent_run_report:v1 repo=${ownership.repo} issue=${ownership.issueNumber} pr=${ownership.prNumber ?? 'null'} run_id=${ownership.runId} -->`
}

export function formatDigestMarker(sha256Hex) {
  if (typeof sha256Hex !== 'string' || !/^[a-f0-9]{64}$/iu.test(sha256Hex)) {
    throw runtimeError('github_comments.digest', 'sha256 digest must be 64 hex characters')
  }
  return `<!-- agent_run_report_digest:v1 sha256=${sha256Hex.toLowerCase()} -->`
}

export function parseOwnershipMarker(line) {
  const match = line.trim().match(OWNERSHIP_MARKER_PATTERN)
  if (!match?.groups) {
    return null
  }
  return {
    repo: match.groups.repo,
    issueNumber: Number(match.groups.issue),
    prNumber: match.groups.pr === 'null' ? null : Number(match.groups.pr),
    runId: match.groups.runId,
  }
}

export function parseDigestMarker(line) {
  const match = line.trim().match(DIGEST_MARKER_PATTERN)
  if (!match?.groups) {
    return null
  }
  return match.groups.digest.toLowerCase()
}

export function ownershipTupleEquals(left, right) {
  return left.repo === right.repo
    && left.issueNumber === right.issueNumber
    && left.prNumber === right.prNumber
    && left.runId === right.runId
}

export function sha256Hex(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex')
}

export function buildAgentRunReportCommentBody({ ownership, payloadMarkdown }) {
  const digest = sha256Hex(payloadMarkdown)
  const ownershipMarker = formatOwnershipMarker(ownership)
  const digestMarker = formatDigestMarker(digest)
  const body = `${ownershipMarker}\n${digestMarker}\n\n${payloadMarkdown}`
  const validation = validateFinalCommentBody(body, {
    expectedOwnership: ownership,
    expectedDigest: digest,
  })
  if (!validation.valid) {
    const [firstError] = validation.errors
    throw runtimeError(firstError.code, firstError.message)
  }
  return {
    body,
    digest,
    byteLength: validation.byteLength,
  }
}

export function validateFinalCommentBody(body, { expectedOwnership = null, expectedDigest = null, maxBytes = MAX_GITHUB_COMMENT_BYTES } = {}) {
  const errors = []
  const ownershipMatchCount = countRegexMatches(body, OWNERSHIP_MARKER_SCAN)
  const digestMatchCount = countRegexMatches(body, DIGEST_MARKER_SCAN)
  const lines = body.split('\n')
  const trimmedNonEmptyLines = lines.map((line) => line.trim()).filter((line) => line.length > 0)
  const ownershipMarker = trimmedNonEmptyLines[0] ?? ''
  const digestMarker = trimmedNonEmptyLines[1] ?? ''
  const parsedOwnership = parseOwnershipMarker(ownershipMarker)
  const parsedDigest = parseDigestMarker(digestMarker)
  const byteLength = Buffer.byteLength(body, 'utf8')

  if (ownershipMatchCount !== 1) {
    errors.push({
      path: 'body',
      code: 'github_comments.ownership_marker_count',
      message: `ownership marker must appear exactly once (found ${ownershipMatchCount})`,
    })
  }

  if (digestMatchCount !== 1) {
    errors.push({
      path: 'body',
      code: 'github_comments.digest_marker_count',
      message: `digest marker must appear exactly once (found ${digestMatchCount})`,
    })
  }

  if (!parsedOwnership) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'github_comments.ownership_marker_position',
      message: 'first non-empty line must be a valid ownership marker',
    })
  }

  if (!parsedDigest) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'github_comments.digest_marker_position',
      message: 'second non-empty line must be a valid digest marker',
    })
  }

  if (expectedOwnership && parsedOwnership && !ownershipTupleEquals(parsedOwnership, buildOwnershipTuple(expectedOwnership))) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'github_comments.ownership_mismatch',
      message: 'ownership marker does not match the expected repo / issue / pr / run_id tuple',
    })
  }

  if (expectedDigest && parsedDigest && parsedDigest !== expectedDigest.toLowerCase()) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'github_comments.digest_mismatch',
      message: 'digest marker does not match the canonical payload markdown digest',
    })
  }

  if (byteLength > maxBytes) {
    errors.push({
      path: 'body',
      code: 'github_comments.body_too_large',
      message: `comment body exceeds ${maxBytes} UTF-8 bytes`,
    })
  }

  return {
    valid: errors.length === 0,
    errors,
    byteLength,
    ownership: parsedOwnership,
    digest: parsedDigest,
  }
}

export function parseMarkerComment(comment) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const validation = validateFinalCommentBody(body)
  if (!validation.ownership) {
    return {
      ok: false,
      malformed: false,
      body,
      comment,
    }
  }
  return {
    ok: validation.valid,
    malformed: !validation.valid,
    body,
    comment,
    ownership: validation.ownership,
    digest: validation.digest,
    byteLength: validation.byteLength,
    errors: validation.errors,
  }
}

export class GithubApiError extends Error {
  constructor(message, { httpStatus = null, reasonCode = 'unknown_http_status', errorBody = '' } = {}) {
    super(message)
    this.name = 'GithubApiError'
    this.httpStatus = httpStatus
    this.reasonCode = reasonCode
    this.errorBody = errorBody
  }
}

function classifyGithubError(httpStatus, errorBody) {
  switch (httpStatus) {
    case 403:
      return 'permission_denied'
    case 404:
      return 'not_found'
    case 410:
      return 'gone'
    case 422:
      return /secondary rate/i.test(errorBody) ? 'secondary_rate_limit' : 'validation_failed'
    default:
      return 'unknown_http_status'
  }
}

function sanitizeGithubErrorBody(errorBody) {
  if (typeof errorBody !== 'string' || errorBody.trim().length === 0) {
    return {
      message: '',
      documentation_url: null,
    }
  }
  try {
    const parsed = JSON.parse(errorBody)
    return {
      message: typeof parsed?.message === 'string' ? parsed.message : errorBody.trim(),
      documentation_url: typeof parsed?.documentation_url === 'string' ? parsed.documentation_url : null,
    }
  } catch {
    return {
      message: errorBody.trim(),
      documentation_url: null,
    }
  }
}

export function parseGhHttpResponse(rawText) {
  const normalized = rawText.replace(/\r\n/g, '\n')
  const lines = normalized.split('\n')
  const statusLine = lines.find((line) => /^HTTP\/\d(?:\.\d)?\s+\d{3}\b/u.test(line)) ?? ''
  const statusMatch = statusLine.match(/^HTTP\/\d(?:\.\d)?\s+(?<status>\d{3})\b/u)
  const httpStatus = statusMatch ? Number(statusMatch.groups.status) : null
  const statusIndex = lines.findIndex((line) => line === statusLine)
  const bodyLines = statusIndex === -1 ? [] : lines.slice(statusIndex + 1)
  const separatorIndex = bodyLines.findIndex((line) => line.trim() === '')
  const responseBody = separatorIndex === -1 ? '' : bodyLines.slice(separatorIndex + 1).join('\n').trim()
  return {
    httpStatus,
    responseBody,
  }
}

function runGhApi(args, { stdinJson = null } = {}) {
  const result = spawnSync('gh', ['api', '-i', ...args], {
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
    input: stdinJson === null ? undefined : JSON.stringify(stdinJson),
  })
  const mergedOutput = `${result.stdout ?? ''}${result.stderr ?? ''}`.trim()
  const { httpStatus, responseBody } = parseGhHttpResponse(mergedOutput)

  if (result.status !== 0) {
    throw new GithubApiError('github api request failed', {
      httpStatus,
      reasonCode: classifyGithubError(httpStatus, responseBody),
      errorBody: responseBody,
    })
  }

  let parsedBody = null
  if (responseBody.length > 0) {
    parsedBody = JSON.parse(responseBody)
  }

  return {
    httpStatus,
    body: parsedBody,
  }
}

export class GhCliIssueCommentsClient {
  async listIssueComments({ repo, issueNumber, page, perPage }) {
    const response = runGhApi([
      '-H', 'Accept: application/vnd.github+json',
      '-H', 'X-GitHub-Api-Version: 2022-11-28',
      `repos/${repo}/issues/${issueNumber}/comments?per_page=${perPage}&page=${page}`,
    ])
    return Array.isArray(response.body) ? response.body : []
  }

  async createIssueComment({ repo, issueNumber, body }) {
    const response = runGhApi([
      '-X', 'POST',
      '-H', 'Accept: application/vnd.github+json',
      '-H', 'X-GitHub-Api-Version: 2022-11-28',
      `repos/${repo}/issues/${issueNumber}/comments`,
      '--input', '-',
    ], {
      stdinJson: { body },
    })
    return response.body
  }

  async updateIssueComment({ repo, commentId, body }) {
    const response = runGhApi([
      '-X', 'PATCH',
      '-H', 'Accept: application/vnd.github+json',
      '-H', 'X-GitHub-Api-Version: 2022-11-28',
      `repos/${repo}/issues/comments/${commentId}`,
      '--input', '-',
    ], {
      stdinJson: { body },
    })
    return response.body
  }
}

export async function listAllIssueComments(client, { repo, issueNumber, perPage = 100 }) {
  const result = await listAllIssueCommentsStructured(client, {
    repo,
    issueNumber,
    perPage,
  })
  if (result.pagination_exhausted) {
    throw runtimeError('github_comments.pagination_exhausted', 'issue comment pagination exhausted before scan completion')
  }
  return result.comments
}

export async function listAllIssueCommentsStructured(client, {
  repo,
  issueNumber,
  perPage = 100,
  maxPages = 100,
}) {
  const comments = []
  for (let page = 1; page <= maxPages; page += 1) {
    const pageItems = await client.listIssueComments({ repo, issueNumber, page, perPage })
    comments.push(...pageItems)
    if (pageItems.length < perPage) {
      return {
        comments,
        pageCount: page,
        scannedComments: comments.length,
        maxPages,
        perPage,
        endpoint: `repos/${repo}/issues/${issueNumber}/comments`,
        pagination_exhausted: false,
        lastPageSize: pageItems.length,
      }
    }
    if (page === maxPages) {
      return {
        comments,
        pageCount: page,
        scannedComments: comments.length,
        maxPages,
        perPage,
        endpoint: `repos/${repo}/issues/${issueNumber}/comments`,
        pagination_exhausted: true,
        lastPageSize: pageItems.length,
      }
    }
  }
  return {
    comments,
    pageCount: 0,
    scannedComments: comments.length,
    maxPages,
    perPage,
    endpoint: `repos/${repo}/issues/${issueNumber}/comments`,
    pagination_exhausted: false,
    lastPageSize: 0,
  }
}

function commentUrlFromResponse(response) {
  return response?.html_url ?? response?.url ?? null
}

export async function upsertAgentRunReportComment(client, {
  repo,
  targetNumber,
  issueNumber,
  prNumber = null,
  runId,
  payloadMarkdown,
  dryRun = false,
  maxBytes = MAX_GITHUB_COMMENT_BYTES,
}) {
  const ownership = buildOwnershipTuple({ repo, issueNumber, prNumber, runId })
  const candidate = buildAgentRunReportCommentBody({ ownership, payloadMarkdown })
  if (candidate.byteLength > maxBytes) {
    throw runtimeError('github_comments.body_too_large', `comment body exceeds ${maxBytes} UTF-8 bytes`)
  }

  const comments = await listAllIssueComments(client, { repo, issueNumber: targetNumber })
  const parsedComments = comments.map((comment) => parseMarkerComment(comment))
  const malformedMatch = parsedComments.find((entry) => entry.ownership && ownershipTupleEquals(entry.ownership, ownership) && entry.malformed)
  if (malformedMatch) {
    throw runtimeError('github_comments.existing_comment_malformed', 'existing marker comment is malformed; refusing to update')
  }

  const matches = parsedComments.filter((entry) => entry.ownership && ownershipTupleEquals(entry.ownership, ownership))
  if (matches.length >= 2) {
    throw runtimeError('github_comments.duplicate_marker', 'multiple existing comments match the stable ownership marker')
  }

  if (matches.length === 0) {
    if (dryRun) {
      return {
        action: 'create',
        repo,
        issue_number: ownership.issueNumber,
        pr_number: ownership.prNumber,
        run_id: ownership.runId,
        sha256: candidate.digest,
        byte_length: candidate.byteLength,
        comment_id: null,
        comment_url: null,
      }
    }
    const created = await client.createIssueComment({
      repo,
      issueNumber: targetNumber,
      body: candidate.body,
    })
    return {
      action: 'create',
      repo,
      issue_number: ownership.issueNumber,
      pr_number: ownership.prNumber,
      run_id: ownership.runId,
      sha256: candidate.digest,
      byte_length: candidate.byteLength,
      comment_id: created?.id ?? null,
      comment_url: commentUrlFromResponse(created),
    }
  }

  const [existing] = matches
  if (existing.digest === candidate.digest) {
    return {
      action: 'noop',
      repo,
      issue_number: ownership.issueNumber,
      pr_number: ownership.prNumber,
      run_id: ownership.runId,
      sha256: candidate.digest,
      byte_length: candidate.byteLength,
      comment_id: existing.comment?.id ?? null,
      comment_url: commentUrlFromResponse(existing.comment),
    }
  }

  if (dryRun) {
    return {
      action: 'update',
      repo,
      issue_number: ownership.issueNumber,
      pr_number: ownership.prNumber,
      run_id: ownership.runId,
      sha256: candidate.digest,
      byte_length: candidate.byteLength,
      comment_id: existing.comment?.id ?? null,
      comment_url: commentUrlFromResponse(existing.comment),
    }
  }

  const updated = await client.updateIssueComment({
    repo,
    commentId: existing.comment.id,
    body: candidate.body,
  })
  return {
    action: 'update',
    repo,
    issue_number: ownership.issueNumber,
    pr_number: ownership.prNumber,
    run_id: ownership.runId,
    sha256: candidate.digest,
    byte_length: candidate.byteLength,
    comment_id: updated?.id ?? existing.comment?.id ?? null,
    comment_url: commentUrlFromResponse(updated) ?? commentUrlFromResponse(existing.comment),
  }
}

export function summarizeGithubApiError(error) {
  if (!(error instanceof GithubApiError)) {
    return null
  }
  const sanitized = sanitizeGithubErrorBody(error.errorBody)
  return {
    status: 'failed',
    reason_code: error.reasonCode,
    http_status: error.httpStatus,
    message: sanitized.message,
    documentation_url: sanitized.documentation_url,
  }
}
