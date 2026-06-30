import { readFile } from 'fs/promises'
import { Buffer } from 'buffer'
import { fileURLToPath } from 'url'

import {
  extractPayloadFromMarkdown,
  renderPublicMarkdown,
  scanPublicSafety,
  validateAgentRetroIndex,
  validateAgentRunReport,
  validateChatgptRetroContextMarker,
  validateMarkdownCandidate,
} from '../../lib/agent-run-report-validation.mjs'
import {
  GhCliIssueCommentsClient,
  listAllIssueComments,
  listAllIssueCommentsStructured,
  parseMarkerComment,
  sha256Hex,
} from './github-comments.mjs'
import { buildSourceCommentSetDigest } from './retro-index-builder.mjs'
import {
  parseRetroDigestMarker,
  parseRetroOwnershipMarker,
  validateRetroCommentBody,
} from './retro-index-comment-helper.mjs'
import { parseArgs, printCliError, runtimeError } from './args.mjs'

export const CHATGPT_RETRO_CONTEXT_OWNERSHIP_PATTERN = /^<!--\s*CHATGPT_RETRO_CONTEXT_V1 repo=(?<repo>[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+) target=(?<targetType>issue|pull_request):(?<targetNumber>[0-9]+) parent_issue=(?<parentIssue>[0-9]+)\s*-->$/u
export const CHATGPT_RETRO_CONTEXT_DIGEST_PATTERN = /^<!--\s*CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=(?<digest>[a-f0-9]{64})\s*-->$/iu

const OWNERSHIP_SCAN = /<!--\s*CHATGPT_RETRO_CONTEXT_V1\s+repo=[^>]*-->/gu
const DIGEST_SCAN = /<!--\s*CHATGPT_RETRO_CONTEXT_DIGEST_V1\b[^>]*-->/giu
const MAX_GITHUB_COMMENT_BYTES = 65536
const CLI_OPTION_SPEC = {
  '--command': { key: 'command', required: true },
  '--repo': { key: 'repo' },
  '--target-type': { key: 'targetType' },
  '--target-number': { key: 'targetNumber' },
  '--parent-issue': { key: 'parentIssue' },
  '--payload-markdown-file': { key: 'payloadMarkdownFile' },
  '--marker-comment-json': { key: 'markerCommentJson' },
  '--github-comments-json': { key: 'githubCommentsJson', multiple: true },
  '--marker-comment-url': { key: 'markerCommentUrl' },
  '--expected-supersedes-digest': { key: 'expectedSupersedesDigest' },
  '--dry-run': { key: 'dryRun', defaultValue: 'true' },
  '--confirm-live': { key: 'confirmLive', defaultValue: 'false' },
}

function countMatches(text, pattern) {
  return Array.from(text.matchAll(pattern)).length
}

function normalizeIssueNumber(value, fieldName) {
  const numberValue = Number(value)
  if (!Number.isInteger(numberValue) || numberValue <= 0) {
    throw runtimeError(`chatgpt_retro_context.${fieldName}`, `${fieldName} must be a positive integer`)
  }
  return numberValue
}

function normalizeTargetType(value) {
  if (value !== 'issue' && value !== 'pull_request') {
    throw runtimeError('chatgpt_retro_context.target_type', 'target type must be issue or pull_request')
  }
  return value
}

function normalizeRepo(repo) {
  if (typeof repo !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/u.test(repo)) {
    throw runtimeError('chatgpt_retro_context.repo', 'repo must be an owner/name string')
  }
  return repo
}

function parseBooleanFlag(value, optionName) {
  if (value === 'true') {
    return true
  }
  if (value === 'false') {
    return false
  }
  throw runtimeError('chatgpt_retro_context.invalid_flag', `${optionName} must be true or false`)
}

export function buildChatgptRetroContextOwnership(input) {
  return {
    repo: normalizeRepo(input.repo),
    targetType: normalizeTargetType(input.targetType),
    targetNumber: normalizeIssueNumber(input.targetNumber, 'target_number'),
    parentIssue: normalizeIssueNumber(input.parentIssue, 'parent_issue'),
  }
}

export function formatChatgptRetroContextOwnershipMarker(input) {
  const ownership = buildChatgptRetroContextOwnership(input)
  return `<!-- CHATGPT_RETRO_CONTEXT_V1 repo=${ownership.repo} target=${ownership.targetType}:${ownership.targetNumber} parent_issue=${ownership.parentIssue} -->`
}

export function formatChatgptRetroContextDigestMarker(digest) {
  if (typeof digest !== 'string' || !/^[a-f0-9]{64}$/iu.test(digest)) {
    throw runtimeError('chatgpt_retro_context.digest', 'sha256 digest must be 64 hex characters')
  }
  return `<!-- CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=${digest.toLowerCase()} -->`
}

export function parseChatgptRetroContextOwnershipMarker(line) {
  const match = line.trim().match(CHATGPT_RETRO_CONTEXT_OWNERSHIP_PATTERN)
  if (!match?.groups) {
    return null
  }
  return {
    repo: match.groups.repo,
    targetType: match.groups.targetType,
    targetNumber: Number(match.groups.targetNumber),
    parentIssue: Number(match.groups.parentIssue),
  }
}

export function parseChatgptRetroContextDigestMarker(line) {
  const match = line.trim().match(CHATGPT_RETRO_CONTEXT_DIGEST_PATTERN)
  if (!match?.groups) {
    return null
  }
  return match.groups.digest.toLowerCase()
}

function ownershipEquals(left, right) {
  return left.repo === right.repo
    && left.targetType === right.targetType
    && left.targetNumber === right.targetNumber
    && left.parentIssue === right.parentIssue
}

export function validateChatgptRetroContextCommentBody(body, {
  expectedOwnership = null,
  expectedDigest = null,
  maxBytes = MAX_GITHUB_COMMENT_BYTES,
} = {}) {
  const lines = body.split('\n')
  const nonEmptyLines = lines.map((line) => line.trim()).filter((line) => line.length > 0)
  const ownership = parseChatgptRetroContextOwnershipMarker(nonEmptyLines[0] ?? '')
  const digest = parseChatgptRetroContextDigestMarker(nonEmptyLines[1] ?? '')
  const errors = []

  if (countMatches(body, OWNERSHIP_SCAN) !== 1) {
    errors.push({
      path: 'body',
      code: 'chatgpt_retro_context.ownership_marker_count',
      message: 'ownership marker must appear exactly once',
    })
  }
  if (countMatches(body, DIGEST_SCAN) !== 1) {
    errors.push({
      path: 'body',
      code: 'chatgpt_retro_context.digest_marker_count',
      message: 'digest marker must appear exactly once',
    })
  }
  if (!ownership) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'chatgpt_retro_context.ownership_marker_position',
      message: 'first non-empty line must be a valid context ownership marker',
    })
  }
  if (!digest) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'chatgpt_retro_context.digest_marker_position',
      message: 'second non-empty line must be a valid context digest marker',
    })
  }
  if (expectedOwnership && ownership && !ownershipEquals(ownership, buildChatgptRetroContextOwnership(expectedOwnership))) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'chatgpt_retro_context.ownership_mismatch',
      message: 'ownership marker does not match the expected repo / target / parent issue tuple',
    })
  }
  if (expectedDigest && digest && digest !== expectedDigest.toLowerCase()) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'chatgpt_retro_context.digest_mismatch',
      message: 'digest marker does not match the canonical payload markdown digest',
    })
  }

  const byteLength = Buffer.byteLength(body, 'utf8')
  if (byteLength > maxBytes) {
    errors.push({
      path: 'body',
      code: 'chatgpt_retro_context.body_too_large',
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

export function computeChatgptRetroContextPayloadDigest(payload) {
  const normalizedPayload = JSON.parse(JSON.stringify(payload))
  if (normalizedPayload?.canonicalization) {
    normalizedPayload.canonicalization.payload_digest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
  }
  return `sha256:${sha256Hex(stableStringify(normalizedPayload))}`
}

function stableStringify(value) {
  if (Array.isArray(value)) {
    return `[${value.map((entry) => stableStringify(entry)).join(',')}]`
  }
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function buildChatgptRetroContextCommentBody({ ownership, payloadMarkdown }) {
  const validation = validateMarkdownCandidate(payloadMarkdown, 'chatgpt_retro_context_marker/v1')
  if (!validation.valid) {
    const [firstError] = validation.errors
    throw runtimeError(firstError.code, firstError.message)
  }

  const payloadDigest = sha256Hex(payloadMarkdown)
  const body = [
    formatChatgptRetroContextOwnershipMarker(ownership),
    formatChatgptRetroContextDigestMarker(payloadDigest),
    '',
    payloadMarkdown,
  ].join('\n')

  const bodyValidation = validateChatgptRetroContextCommentBody(body, {
    expectedOwnership: ownership,
    expectedDigest: payloadDigest,
  })
  if (!bodyValidation.valid) {
    const [firstError] = bodyValidation.errors
    throw runtimeError(firstError.code, firstError.message)
  }

  return {
    body,
    digest: `sha256:${payloadDigest}`,
    byteLength: bodyValidation.byteLength,
    ownership: buildChatgptRetroContextOwnership(ownership),
  }
}

export function parseChatgptRetroContextComment(comment) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const validation = validateChatgptRetroContextCommentBody(body)
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
    digest: validation.digest ? `sha256:${validation.digest}` : null,
    byteLength: validation.byteLength,
    errors: validation.errors,
  }
}

export async function upsertChatgptRetroContextComment(client, {
  repo,
  targetType,
  targetNumber,
  parentIssue,
  payloadMarkdown,
  dryRun = true,
  expectedSupersedesDigest = null,
}) {
  const ownership = buildChatgptRetroContextOwnership({ repo, targetType, targetNumber, parentIssue })
  const candidate = buildChatgptRetroContextCommentBody({ ownership, payloadMarkdown })
  const comments = await listAllIssueComments(client, {
    repo,
    issueNumber: ownership.targetNumber,
  })
  const parsedComments = comments.map((comment) => parseChatgptRetroContextComment(comment))
  const malformedMatch = parsedComments.find((entry) => entry.ownership && ownershipEquals(entry.ownership, ownership) && entry.malformed)
  if (malformedMatch) {
    throw runtimeError('chatgpt_retro_context.blocked_malformed', 'existing context marker comment is malformed; refusing to update')
  }
  const matches = parsedComments.filter((entry) => entry.ownership && ownershipEquals(entry.ownership, ownership))
  if (matches.length >= 2) {
    throw runtimeError('chatgpt_retro_context.blocked_duplicate', 'multiple existing context marker comments match the stable ownership marker')
  }
  if (matches.length === 0) {
    if (dryRun) {
      return { action: 'create', digest: candidate.digest, comment_id: null, comment_url: null }
    }
    const created = await client.createIssueComment({
      repo,
      issueNumber: ownership.targetNumber,
      body: candidate.body,
    })
    return { action: 'create', digest: candidate.digest, comment_id: created?.id ?? null, comment_url: created?.html_url ?? created?.url ?? null }
  }

  const [existing] = matches
  if (existing.digest === candidate.digest) {
    return {
      action: 'noop',
      digest: candidate.digest,
      comment_id: existing.comment?.id ?? null,
      comment_url: existing.comment?.html_url ?? existing.comment?.url ?? null,
    }
  }
  if (expectedSupersedesDigest === null) {
    throw runtimeError('chatgpt_retro_context.blocked_missing_supersedes_digest', 'expectedSupersedesDigest is required before updating an existing context marker')
  }
  if (existing.digest !== expectedSupersedesDigest) {
    throw runtimeError('chatgpt_retro_context.blocked_stale_write', 'existing context marker digest changed before update')
  }

  if (dryRun) {
    return {
      action: 'supersede',
      digest: candidate.digest,
      comment_id: existing.comment?.id ?? null,
      comment_url: existing.comment?.html_url ?? existing.comment?.url ?? null,
    }
  }
  const updated = await client.updateIssueComment({
    repo,
    commentId: existing.comment.id,
    body: candidate.body,
  })
  return {
    action: 'supersede',
    digest: candidate.digest,
    comment_id: updated?.id ?? existing.comment?.id ?? null,
    comment_url: updated?.html_url ?? updated?.url ?? existing.comment?.html_url ?? null,
  }
}

function commentUrlFromResponse(response) {
  return response?.html_url ?? response?.url ?? null
}

export async function resolveChatgptRetroContextLive(client, {
  repo,
  targetType,
  targetNumber,
  parentIssue,
  markerCommentUrl = null,
}) {
  const ownership = buildChatgptRetroContextOwnership({ repo, targetType, targetNumber, parentIssue })
  const listed = await listAllIssueCommentsStructured(client, {
    repo: ownership.repo,
    issueNumber: ownership.targetNumber,
  })
  const target = {
    type: ownership.targetType,
    number: ownership.targetNumber,
    endpoint_kind: ownership.targetType === 'pull_request' ? 'issue_comments_for_pull_request' : 'issue_comments_for_issue',
  }
  const pagination = {
    page_count: listed.pageCount,
    scanned_comments: listed.scannedComments,
    max_pages: listed.maxPages,
    per_page: listed.perPage,
    endpoint: listed.endpoint,
  }

  if (listed.pagination_exhausted) {
    return {
      status: 'blocked_pagination_exhausted',
      repo: ownership.repo,
      target,
      parent_issue: ownership.parentIssue,
      marker_comment_url: markerCommentUrl,
      pagination,
      comment_count: listed.comments.length,
      matched_comment_count: 0,
      marker_comment: null,
      digest: null,
    }
  }

  const parsedComments = listed.comments.map((comment) => parseChatgptRetroContextComment(comment))
  const matches = parsedComments.filter((entry) => entry.ownership && ownershipEquals(entry.ownership, ownership))
  const malformed = matches.find((entry) => entry.malformed)
  if (malformed) {
    return {
      status: 'blocked_malformed',
      repo: ownership.repo,
      target,
      parent_issue: ownership.parentIssue,
      marker_comment_url: commentUrlFromResponse(malformed.comment),
      pagination,
      comment_count: listed.comments.length,
      matched_comment_count: matches.length,
      marker_comment: {
        id: malformed.comment?.id ?? null,
        url: commentUrlFromResponse(malformed.comment),
      },
      digest: malformed.digest,
    }
  }

  if (matches.length >= 2) {
    return {
      status: 'blocked_duplicate',
      repo: ownership.repo,
      target,
      parent_issue: ownership.parentIssue,
      marker_comment_url: markerCommentUrl,
      pagination,
      comment_count: listed.comments.length,
      matched_comment_count: matches.length,
      marker_comment: null,
      digest: null,
    }
  }

  const [match] = matches
  if (!match) {
    return {
      status: 'missing',
      repo: ownership.repo,
      target,
      parent_issue: ownership.parentIssue,
      marker_comment_url: markerCommentUrl,
      pagination,
      comment_count: listed.comments.length,
      matched_comment_count: 0,
      marker_comment: null,
      digest: null,
    }
  }

  const resolvedUrl = commentUrlFromResponse(match.comment)
  if (markerCommentUrl !== null && resolvedUrl !== markerCommentUrl) {
    return {
      status: 'blocked_stale_write',
      repo: ownership.repo,
      target,
      parent_issue: ownership.parentIssue,
      marker_comment_url: markerCommentUrl,
      pagination,
      comment_count: listed.comments.length,
      matched_comment_count: 1,
      marker_comment: {
        id: match.comment?.id ?? null,
        url: resolvedUrl,
      },
      digest: match.digest,
    }
  }

  return {
    status: 'resolved',
    repo: ownership.repo,
    target,
    parent_issue: ownership.parentIssue,
    marker_comment_url: resolvedUrl,
    pagination,
    comment_count: listed.comments.length,
    matched_comment_count: 1,
    marker_comment: {
      id: match.comment?.id ?? null,
      url: resolvedUrl,
    },
    digest: match.digest,
  }
}

function normalizeCommentListPayload(payload) {
  if (Array.isArray(payload)) {
    return payload
  }
  if (payload && typeof payload === 'object' && typeof payload.body === 'string') {
    return [payload]
  }
  throw runtimeError('chatgpt_retro_context.invalid_comment_fixture', 'comment fixture must be an object or array of comment objects')
}

async function readJsonFile(filePath, code) {
  try {
    return JSON.parse(await readFile(filePath, 'utf-8'))
  } catch (error) {
    throw runtimeError(code, `failed to parse JSON file: ${filePath}`)
  }
}

function findCommentByUrl(comments, url) {
  return comments.find((comment) => comment?.html_url === url || comment?.url === url) ?? null
}

function buildSyntheticSourceSet(payload) {
  return {
    schema: 'source_set/v1',
    sources: [
      ...payload.refs.run_reports.map((entry) => entry.comment_url),
      payload.refs.retro_index.comment_url,
    ],
  }
}

function assertValidation(result, code) {
  if (!result.valid) {
    const [firstError] = result.errors
    throw runtimeError(code, firstError?.message ?? code)
  }
}

function parseRetroMarkerComment(comment) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const validation = validateRetroCommentBody(body)
  const lines = body.split('\n').map((line) => line.trim()).filter((line) => line.length > 0)
  const ownership = parseRetroOwnershipMarker(lines[0] ?? '')
  const digest = parseRetroDigestMarker(lines[1] ?? '')
  if (!ownership) {
    return { ok: false, malformed: false, comment, body }
  }
  return {
    ok: validation.valid,
    malformed: !validation.valid,
    comment,
    body,
    ownership,
    digest: digest
      ? {
          canonicalDigest: `sha256:${digest.canonicalDigest}`,
          sourceSetDigest: `sha256:${digest.sourceSetDigest}`,
        }
      : null,
  }
}

export async function resolveChatgptRetroContextFromFixtures({
  markerCommentJson,
  githubCommentsJson = [],
}) {
  const markerComment = await readJsonFile(markerCommentJson, 'chatgpt_retro_context.marker_comment_parse')
  const commentPages = await Promise.all(githubCommentsJson.map((filePath) => readJsonFile(filePath, 'chatgpt_retro_context.comments_parse')))
  const comments = commentPages.flatMap((payload) => normalizeCommentListPayload(payload))

  const parsedMarker = parseChatgptRetroContextComment(markerComment)
  if (!parsedMarker.ok) {
    throw runtimeError('chatgpt_retro_context.marker_invalid', 'marker comment fixture must contain a valid context marker')
  }
  const extraction = extractPayloadFromMarkdown(parsedMarker.body, 'chatgpt_retro_context_marker/v1')
  if (!extraction.ok) {
    throw runtimeError(extraction.error.code, extraction.error.message)
  }
  const payload = extraction.payload
  assertValidation(validateChatgptRetroContextMarker(payload), 'chatgpt_retro_context.marker_payload_invalid')
  const expectedPayloadDigest = computeChatgptRetroContextPayloadDigest(payload)
  if (payload.canonicalization?.payload_digest !== expectedPayloadDigest) {
    throw runtimeError('chatgpt_retro_context.payload_digest_mismatch', 'payload canonicalization digest does not match the canonical marker payload')
  }
  if (
    payload.repo !== parsedMarker.ownership.repo
    || payload.target?.type !== parsedMarker.ownership.targetType
    || payload.target?.number !== parsedMarker.ownership.targetNumber
    || payload.parent_issue !== parsedMarker.ownership.parentIssue
  ) {
    throw runtimeError('chatgpt_retro_context.payload_ownership_mismatch', 'marker ownership comment and embedded payload ownership must match')
  }

  const reportPayloads = []
  const evidenceRefs = []
  const sourceCommentRefs = []
  for (const reportRef of payload.refs.run_reports) {
    const reportComment = findCommentByUrl(comments, reportRef.comment_url)
    if (!reportComment) {
      throw runtimeError('chatgpt_retro_context.report_comment_missing', `referenced run report comment not found: ${reportRef.comment_url}`)
    }
    const parsedReport = parseMarkerComment(reportComment)
    if (!parsedReport.ok) {
      throw runtimeError('chatgpt_retro_context.report_comment_invalid', `referenced run report comment is invalid: ${reportRef.comment_url}`)
    }
    const normalizedReportDigest = parsedReport.digest ? `sha256:${parsedReport.digest}` : null
    if (normalizedReportDigest !== reportRef.payload_digest) {
      throw runtimeError('chatgpt_retro_context.report_digest_mismatch', `run report digest mismatch: ${reportRef.comment_url}`)
    }
    const reportExtraction = extractPayloadFromMarkdown(parsedReport.body, 'agent_run_report/v1')
    if (!reportExtraction.ok) {
      throw runtimeError(reportExtraction.error.code, reportExtraction.error.message)
    }
    assertValidation(validateAgentRunReport(reportExtraction.payload), 'chatgpt_retro_context.report_payload_invalid')
    reportPayloads.push(reportExtraction.payload)
    sourceCommentRefs.push({
      comment_url: reportRef.comment_url,
      source_kind: 'issues',
      source_number: payload.target.number,
      body_digest: reportRef.payload_digest,
    })
    evidenceRefs.push({
      kind: 'github_comment',
      ref: reportRef.comment_url,
      digest: reportRef.payload_digest,
      validation_verdict: reportRef.validation_verdict,
    })
  }

  const retroComment = findCommentByUrl(comments, payload.refs.retro_index.comment_url)
  if (!retroComment) {
    throw runtimeError('chatgpt_retro_context.retro_comment_missing', `referenced retro index comment not found: ${payload.refs.retro_index.comment_url}`)
  }
  const parsedRetro = parseRetroMarkerComment(retroComment)
  if (!parsedRetro.ok) {
    throw runtimeError('chatgpt_retro_context.retro_comment_invalid', 'referenced retro index comment is invalid')
  }
  if (parsedRetro.digest?.canonicalDigest !== payload.refs.retro_index.payload_digest) {
    throw runtimeError('chatgpt_retro_context.retro_digest_mismatch', 'retro index payload digest mismatch')
  }
  if (parsedRetro.digest?.sourceSetDigest !== payload.refs.retro_index.source_set_digest) {
    throw runtimeError('chatgpt_retro_context.source_set_digest_mismatch', 'retro index source-set digest mismatch')
  }
  if (
    parsedRetro.ownership?.repo !== payload.repo
    || parsedRetro.ownership?.parentIssue !== payload.parent_issue
  ) {
    throw runtimeError('chatgpt_retro_context.retro_ownership_mismatch', 'retro index ownership must match marker repo / parent issue')
  }
  const retroExtraction = extractPayloadFromMarkdown(parsedRetro.body, 'agent_retro_index/v1')
  if (!retroExtraction.ok) {
    throw runtimeError(retroExtraction.error.code, retroExtraction.error.message)
  }
  assertValidation(validateAgentRetroIndex(retroExtraction.payload), 'chatgpt_retro_context.retro_payload_invalid')
  sourceCommentRefs.push({
    comment_url: payload.refs.retro_index.comment_url,
    source_kind: 'issues',
    source_number: payload.parent_issue,
    body_digest: payload.refs.retro_index.payload_digest,
  })
  const recomputedSourceSetDigest = buildSourceCommentSetDigest(sourceCommentRefs)
  if (recomputedSourceSetDigest !== payload.refs.retro_index.source_set_digest) {
    throw runtimeError('chatgpt_retro_context.source_set_digest_recompute_mismatch', 'retro index source-set digest must match the recomputed referenced comment set')
  }

  const safetyScan = scanPublicSafety(payload)
  if (!safetyScan.valid) {
    const [firstError] = safetyScan.errors
    throw runtimeError(firstError.code, firstError.message)
  }

  return {
    sources: {
      parent_issue_json: { number: payload.parent_issue, title: `Parent Issue #${payload.parent_issue}` },
      target_issue_json: { number: payload.target.number, title: `Target ${payload.target.type} #${payload.target.number}` },
      retro_index_json: retroExtraction.payload,
      source_set_json: buildSyntheticSourceSet(payload),
      run_reports: reportPayloads,
      evidence_refs: evidenceRefs,
    },
    manifest: [
      { source_kind: 'chatgpt_retro_context_marker', source_ref: 'marker-comment', canonical_digest: parsedMarker.digest, body_digest: parsedMarker.digest },
      ...payload.refs.run_reports.map((entry, index) => ({
        source_kind: 'run_report_comment',
        source_ref: `run_report_comment[${index}]`,
        canonical_digest: entry.payload_digest,
        body_digest: entry.payload_digest,
      })),
      {
        source_kind: 'retro_index_comment',
        source_ref: 'retro_index_comment[0]',
        canonical_digest: payload.refs.retro_index.payload_digest,
        body_digest: payload.refs.retro_index.payload_digest,
      },
    ],
  }
}

async function runCli() {
  const options = parseArgs(process.argv.slice(2), CLI_OPTION_SPEC)

  if (options.command === 'resolve-fixture') {
    const result = await resolveChatgptRetroContextFromFixtures({
      markerCommentJson: options.markerCommentJson,
      githubCommentsJson: options.githubCommentsJson ?? [],
    })
    console.log(JSON.stringify(result))
    return
  }

  if (options.command === 'resolve-live') {
    const result = await resolveChatgptRetroContextLive(new GhCliIssueCommentsClient(), {
      repo: options.repo,
      targetType: options.targetType,
      targetNumber: options.targetNumber,
      parentIssue: options.parentIssue,
      markerCommentUrl: options.markerCommentUrl ?? null,
    })
    console.log(JSON.stringify(result))
    return
  }

  if (options.command === 'post') {
    const dryRun = parseBooleanFlag(options.dryRun, '--dry-run')
    const confirmLive = parseBooleanFlag(options.confirmLive, '--confirm-live')
    if (!dryRun && !confirmLive) {
      throw runtimeError('chatgpt_retro_context.live_confirmation_required', 'live posting requires --dry-run false and --confirm-live true')
    }
    const payloadMarkdown = await readFile(options.payloadMarkdownFile, 'utf-8')
    const result = await upsertChatgptRetroContextComment(new GhCliIssueCommentsClient(), {
      repo: options.repo,
      targetType: options.targetType,
      targetNumber: options.targetNumber,
      parentIssue: options.parentIssue,
      payloadMarkdown,
      dryRun,
      expectedSupersedesDigest: options.expectedSupersedesDigest ?? null,
    })
    console.log(JSON.stringify(result))
    return
  }

  throw runtimeError('chatgpt_retro_context.unknown_command', 'command must be post, resolve-fixture, or resolve-live')
}

export {
  GhCliIssueCommentsClient,
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  runCli().catch((error) => {
    process.exit(printCliError('chatgpt-retro-context', error))
  })
}
