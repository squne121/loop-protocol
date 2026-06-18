import { Buffer } from 'buffer'

import { validateMarkdownCandidate } from '../../lib/agent-run-report-validation.mjs'
import {
  GhCliIssueCommentsClient,
  GithubApiError,
  listAllIssueComments,
  summarizeGithubApiError,
} from './github-comments.mjs'
import { runtimeError } from './args.mjs'

export const RETRO_OWNERSHIP_MARKER_PATTERN = /^<!--\s*agent_retro_index:v1 repo=(?<repo>[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+) parent_issue=(?<parentIssue>[0-9]+) algorithm=(?<algorithm>[A-Za-z0-9@._:-]+)\s*-->$/u
export const RETRO_DIGEST_MARKER_PATTERN = /^<!--\s*agent_retro_index_digest:v1 sha256=(?<digest>[a-f0-9]{64}) source_set_sha256=(?<sourceSetDigest>[a-f0-9]{64})\s*-->$/iu

const RETRO_OWNERSHIP_SCAN = /<!--\s*agent_retro_index:v1\b(?!\s+(?:start|end)\b)[^>]*-->/giu
const RETRO_DIGEST_SCAN = /<!--\s*agent_retro_index_digest:v1\b[^>]*-->/giu
const MAX_GITHUB_COMMENT_BYTES = 65536

function countMatches(text, pattern) {
  return Array.from(text.matchAll(pattern)).length
}

function parseBooleanFlag(value, flagName) {
  if (value === 'true') {
    return true
  }
  if (value === 'false') {
    return false
  }
  throw runtimeError('retro_index.invalid_flag', `${flagName} must be true or false`)
}

function normalizeParentIssue(value) {
  const numberValue = Number(value)
  if (!Number.isInteger(numberValue) || numberValue <= 0) {
    throw runtimeError('retro_index.parent_issue', 'parent issue must be a positive integer')
  }
  return numberValue
}

function markerSafeRepo(repo) {
  return typeof repo === 'string' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/u.test(repo)
}

function markerSafeAlgorithm(value) {
  return typeof value === 'string' && /^[A-Za-z0-9@._:-]+$/u.test(value)
}

export function formatRetroOwnershipMarker({ repo, parentIssue, algorithm }) {
  if (!markerSafeRepo(repo)) {
    throw runtimeError('retro_index.repo', 'repo must be an owner/name string')
  }
  if (!markerSafeAlgorithm(algorithm)) {
    throw runtimeError('retro_index.algorithm', 'algorithm must be marker-safe')
  }
  return `<!-- agent_retro_index:v1 repo=${repo} parent_issue=${normalizeParentIssue(parentIssue)} algorithm=${algorithm} -->`
}

export function formatRetroDigestMarker({ canonicalDigest, sourceSetDigest }) {
  const digestPattern = /^[a-f0-9]{64}$/iu
  if (typeof canonicalDigest !== 'string' || !digestPattern.test(canonicalDigest)) {
    throw runtimeError('retro_index.digest', 'canonical digest must be 64 hex characters')
  }
  if (typeof sourceSetDigest !== 'string' || !digestPattern.test(sourceSetDigest)) {
    throw runtimeError('retro_index.source_set_digest', 'source-set digest must be 64 hex characters')
  }
  return `<!-- agent_retro_index_digest:v1 sha256=${canonicalDigest.toLowerCase()} source_set_sha256=${sourceSetDigest.toLowerCase()} -->`
}

export function parseRetroOwnershipMarker(line) {
  const match = line.trim().match(RETRO_OWNERSHIP_MARKER_PATTERN)
  if (!match?.groups) {
    return null
  }
  return {
    repo: match.groups.repo,
    parentIssue: Number(match.groups.parentIssue),
    algorithm: match.groups.algorithm,
  }
}

export function parseRetroDigestMarker(line) {
  const match = line.trim().match(RETRO_DIGEST_MARKER_PATTERN)
  if (!match?.groups) {
    return null
  }
  return {
    canonicalDigest: match.groups.digest.toLowerCase(),
    sourceSetDigest: match.groups.sourceSetDigest.toLowerCase(),
  }
}

function ownershipEquals(left, right) {
  return left.repo === right.repo
    && left.parentIssue === right.parentIssue
    && left.algorithm === right.algorithm
}

export function validateRetroCommentBody(body, {
  expectedOwnership = null,
  expectedCanonicalDigest = null,
  expectedSourceSetDigest = null,
  maxBytes = MAX_GITHUB_COMMENT_BYTES,
} = {}) {
  const errors = []
  const byteLength = Buffer.byteLength(body, 'utf8')
  const ownershipCount = countMatches(body, RETRO_OWNERSHIP_SCAN)
  const digestCount = countMatches(body, RETRO_DIGEST_SCAN)
  const lines = body.split('\n')
  const nonEmptyLines = lines.map((line) => line.trim()).filter((line) => line.length > 0)
  const ownershipLine = nonEmptyLines[0] ?? ''
  const digestLine = nonEmptyLines[1] ?? ''
  const ownership = parseRetroOwnershipMarker(ownershipLine)
  const digest = parseRetroDigestMarker(digestLine)

  if (ownershipCount !== 1) {
    errors.push({
      path: 'body',
      code: 'retro_index.ownership_marker_count',
      message: `ownership marker must appear exactly once (found ${ownershipCount})`,
    })
  }
  if (digestCount !== 1) {
    errors.push({
      path: 'body',
      code: 'retro_index.digest_marker_count',
      message: `digest marker must appear exactly once (found ${digestCount})`,
    })
  }
  if (!ownership) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'retro_index.ownership_marker_position',
      message: 'first non-empty line must be a valid retro index ownership marker',
    })
  }
  if (!digest) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'retro_index.digest_marker_position',
      message: 'second non-empty line must be a valid retro index digest marker',
    })
  }
  if (expectedOwnership && ownership && !ownershipEquals(ownership, expectedOwnership)) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'retro_index.ownership_mismatch',
      message: 'ownership marker does not match the expected repo / parent issue / algorithm tuple',
    })
  }
  if (expectedCanonicalDigest && digest && digest.canonicalDigest !== expectedCanonicalDigest.toLowerCase()) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'retro_index.digest_mismatch',
      message: 'canonical digest marker does not match the rendered retro index payload digest',
    })
  }
  if (expectedSourceSetDigest && digest && digest.sourceSetDigest !== expectedSourceSetDigest.toLowerCase()) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'retro_index.source_set_digest_mismatch',
      message: 'source-set digest marker does not match the canonical source-comment-set digest',
    })
  }
  if (byteLength > maxBytes) {
    errors.push({
      path: 'body',
      code: 'retro_index.body_too_large',
      message: `comment body exceeds ${maxBytes} UTF-8 bytes`,
    })
  }

  return {
    valid: errors.length === 0,
    errors,
    byteLength,
    ownership,
    digest,
  }
}

export function buildRetroIndexCommentBody({
  repo,
  parentIssue,
  algorithm,
  payloadMarkdown,
  canonicalIndexDigest,
  sourceCommentSetDigest,
}) {
  const ownership = {
    repo,
    parentIssue: normalizeParentIssue(parentIssue),
    algorithm,
  }
  const validation = validateMarkdownCandidate(payloadMarkdown, 'agent_retro_index/v1')
  if (!validation.valid) {
    const [firstError] = validation.errors
    throw runtimeError(firstError.code, firstError.message)
  }
  const body = [
    formatRetroOwnershipMarker(ownership),
    formatRetroDigestMarker({
      canonicalDigest: canonicalIndexDigest.replace(/^sha256:/u, ''),
      sourceSetDigest: sourceCommentSetDigest.replace(/^sha256:/u, ''),
    }),
    '',
    payloadMarkdown,
  ].join('\n')
  const bodyValidation = validateRetroCommentBody(body, {
    expectedOwnership: ownership,
    expectedCanonicalDigest: canonicalIndexDigest.replace(/^sha256:/u, ''),
    expectedSourceSetDigest: sourceCommentSetDigest.replace(/^sha256:/u, ''),
  })
  if (!bodyValidation.valid) {
    const [firstError] = bodyValidation.errors
    throw runtimeError(firstError.code, firstError.message)
  }
  return {
    body,
    byteLength: bodyValidation.byteLength,
    ownership,
  }
}

function parseRetroMarkerComment(comment) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const validation = validateRetroCommentBody(body)
  if (!validation.ownership) {
    return {
      ok: false,
      malformed: false,
      comment,
      body,
    }
  }
  return {
    ok: validation.valid,
    malformed: !validation.valid,
    comment,
    body,
    ownership: validation.ownership,
    digest: validation.digest,
  }
}

export async function upsertRetroIndexComment(client, {
  repo,
  parentIssue,
  algorithm,
  payloadMarkdown,
  canonicalIndexDigest,
  sourceCommentSetDigest,
  dryRun = true,
  maxBytes = MAX_GITHUB_COMMENT_BYTES,
}) {
  const candidate = buildRetroIndexCommentBody({
    repo,
    parentIssue,
    algorithm,
    payloadMarkdown,
    canonicalIndexDigest,
    sourceCommentSetDigest,
  })
  if (candidate.byteLength > maxBytes) {
    throw runtimeError('retro_index.body_too_large', `comment body exceeds ${maxBytes} UTF-8 bytes`)
  }

  const comments = await listAllIssueComments(client, {
    repo,
    issueNumber: normalizeParentIssue(parentIssue),
  })
  const parsedComments = comments.map((comment) => parseRetroMarkerComment(comment))
  const malformedMatch = parsedComments.find((entry) => entry.ownership && ownershipEquals(entry.ownership, candidate.ownership) && entry.malformed)
  if (malformedMatch) {
    throw runtimeError('retro_index.existing_comment_malformed', 'existing retro index marker comment is malformed; refusing to update')
  }

  const matches = parsedComments.filter((entry) => entry.ownership && ownershipEquals(entry.ownership, candidate.ownership))
  if (matches.length >= 2) {
    throw runtimeError('retro_index.duplicate_marker', 'multiple existing retro index comments match the stable ownership marker')
  }

  const responseShape = {
    repo,
    parent_issue: candidate.ownership.parentIssue,
    algorithm,
    canonical_index_digest: canonicalIndexDigest,
    source_comment_set_digest: sourceCommentSetDigest,
    byte_length: candidate.byteLength,
  }

  if (matches.length === 0) {
    if (dryRun) {
      return {
        action: 'create',
        comment_id: null,
        comment_url: null,
        ...responseShape,
      }
    }
    const created = await client.createIssueComment({
      repo,
      issueNumber: candidate.ownership.parentIssue,
      body: candidate.body,
    })
    return {
      action: 'create',
      comment_id: created?.id ?? null,
      comment_url: created?.html_url ?? created?.url ?? null,
      ...responseShape,
    }
  }

  const [existing] = matches
  if (
    existing.digest?.canonicalDigest === canonicalIndexDigest.replace(/^sha256:/u, '')
    && existing.digest?.sourceSetDigest === sourceCommentSetDigest.replace(/^sha256:/u, '')
  ) {
    return {
      action: 'noop',
      comment_id: existing.comment?.id ?? null,
      comment_url: existing.comment?.html_url ?? existing.comment?.url ?? null,
      ...responseShape,
    }
  }

  if (dryRun) {
    return {
      action: 'update',
      comment_id: existing.comment?.id ?? null,
      comment_url: existing.comment?.html_url ?? existing.comment?.url ?? null,
      ...responseShape,
    }
  }

  const updated = await client.updateIssueComment({
    repo,
    commentId: existing.comment.id,
    body: candidate.body,
  })
  return {
    action: 'update',
    comment_id: updated?.id ?? existing.comment?.id ?? null,
    comment_url: updated?.html_url ?? updated?.url ?? existing.comment?.html_url ?? null,
    ...responseShape,
  }
}

export {
  GhCliIssueCommentsClient,
  GithubApiError,
  parseBooleanFlag,
  summarizeGithubApiError,
}
