import { createHash } from 'crypto'

import {
  extractPayloadFromMarkdown,
  validateAgentRunReport,
} from '../../lib/agent-run-report-validation.mjs'
import { parseMarkerComment } from './github-comments.mjs'

export const RETRO_INDEX_ALGORITHM = 'retro-index-builder@1'
export const SCHEMA_MIGRATION_FOLLOW_UP = 'follow-up Issue for docs/schemas/agent-retro-index.schema.json key set migration'

function sha256Hex(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex')
}

function sha256Digest(text) {
  return `sha256:${sha256Hex(text)}`
}

function uniqueNumbers(pattern, text) {
  if (typeof text !== 'string') {
    return []
  }
  const found = new Set()
  for (const match of text.matchAll(pattern)) {
    found.add(Number(match[1]))
  }
  return [...found]
}

function parseIssueNumbers(text) {
  return uniqueNumbers(/#([0-9]+)/gu, text)
}

function parseClosingIssueNumbers(text) {
  return uniqueNumbers(/\b(?:closes|fixes|resolves)\s+#([0-9]+)/giu, text)
}

function parsePullRequestNumbers(text) {
  return uniqueNumbers(/(?:\bPR\b|pull(?:\s+request)?)\s*#([0-9]+)/giu, text)
}

function parseIssueCommentUrl(url) {
  if (typeof url !== 'string') {
    return null
  }
  const match = url.match(/^https:\/\/github\.com\/squne121\/loop-protocol\/(?<kind>issues|pull)\/(?<number>[0-9]+)#issuecomment-(?<commentId>[0-9]+)$/u)
  if (!match?.groups) {
    return null
  }
  return {
    kind: match.groups.kind,
    number: Number(match.groups.number),
    commentId: Number(match.groups.commentId),
  }
}

function parseGithubResourceUrl(url, kind) {
  if (typeof url !== 'string') {
    return null
  }
  const match = url.match(new RegExp(`^https://github\\.com/squne121/loop-protocol/${kind}/(?<number>[0-9]+)(?:#.*)?$`, 'u'))
  if (!match?.groups) {
    return null
  }
  return Number(match.groups.number)
}

function parseBranchHintIssue(branchName) {
  if (typeof branchName !== 'string') {
    return null
  }
  const match = branchName.match(/(?:issue-|\/)([0-9]{1,6})(?:\b|-|\/)/u)
  return match ? Number(match[1]) : null
}

function summarizeCommand(report) {
  const chosen = (report.commands_summary ?? []).find((command) => command.verdict !== 'pass')
    ?? report.commands_summary?.[0]
  return chosen?.summary ?? 'public-safe report aggregated'
}

function buildTags(report) {
  const tags = new Set(['agent-run-report'])
  if (report.actor?.type) {
    tags.add(report.actor.type)
  }
  for (const ref of report.docs_read_refs ?? []) {
    if (ref.ref_kind === 'doc_path' && typeof ref.ref === 'string') {
      const leaf = ref.ref.split('/').pop()?.replace(/\.md$/u, '')
      if (leaf) {
        tags.add(leaf.slice(0, 60))
      }
    }
  }
  return [...tags].slice(0, 6)
}

function buildQualitySignals(report) {
  const signals = new Set()
  if (report.public_safety?.redaction_status === 'clean') {
    signals.add('public_safe')
  }
  if (report.public_safety?.verdict === 'pass') {
    signals.add('validator_pass')
  }
  for (const command of report.commands_summary ?? []) {
    if (command.verdict === 'pass') {
      signals.add(command.command_label.toLowerCase().replace(/[^a-z0-9]+/gu, '_').slice(0, 40))
    }
  }
  return [...signals].slice(0, 6)
}

function buildFollowUpIssues(report, primaryIssue) {
  const numbers = new Set()
  for (const ref of report.docs_read_refs ?? []) {
    for (const number of parseIssueNumbers(`${ref.ref ?? ''}\n${ref.summary ?? ''}`)) {
      if (number !== primaryIssue) {
        numbers.add(number)
      }
    }
    const issueFromUrl = parseGithubResourceUrl(ref.ref, 'issues')
    if (issueFromUrl && issueFromUrl !== primaryIssue) {
      numbers.add(issueFromUrl)
    }
  }
  return [...numbers].slice(0, 8)
}

function collectMachineRefs(report, sourceComment) {
  const issueRefs = new Set()
  const prRefs = new Set()

  for (const ref of report.docs_read_refs ?? []) {
    const text = `${ref.ref ?? ''}\n${ref.summary ?? ''}`
    for (const number of parseIssueNumbers(text)) {
      issueRefs.add(number)
    }
    for (const number of parsePullRequestNumbers(text)) {
      prRefs.add(number)
    }
    if (ref.ref_kind === 'pull_request') {
      const first = parseGithubResourceUrl(ref.ref, 'pull') ?? parseIssueNumbers(ref.ref ?? '')[0]
      if (first) {
        prRefs.add(first)
      }
    }
    if (ref.ref_kind === 'issue') {
      const first = parseGithubResourceUrl(ref.ref, 'issues')
      if (first) {
        issueRefs.add(first)
      }
    }
  }

  for (const number of sourceComment.linkedIssueHints ?? []) {
    issueRefs.add(number)
  }
  for (const number of sourceComment.linkedPrHints ?? []) {
    prRefs.add(number)
  }

  return {
    issueRefs: [...issueRefs],
    prRefs: [...prRefs],
  }
}

function buildSourceCommentRef(sourceComment) {
  return {
    comment_url: sourceComment.html_url,
    source_kind: sourceComment.parsedUrl.kind,
    source_number: sourceComment.parsedUrl.number,
    body_digest: sourceComment.reportDigest,
  }
}

function canonicalizeSourceCommentSet(sourceCommentRefs) {
  return JSON.stringify(
    sourceCommentRefs
      .map((ref) => ({
        comment_url: ref.comment_url,
        source_kind: ref.source_kind,
        source_number: ref.source_number,
        body_digest: ref.body_digest,
      }))
      .sort((left, right) => left.comment_url.localeCompare(right.comment_url)),
    null,
    2
  )
}

function normalizeSourceComment(sourceComment) {
  const parsedUrl = parseIssueCommentUrl(sourceComment.html_url)
  if (!parsedUrl) {
    return { kind: 'ignored' }
  }

  const marker = parseMarkerComment({ body: sourceComment.body })
  if (!marker.ownership) {
    return { kind: 'ignored' }
  }
  if (marker.malformed) {
    return {
      kind: 'blocked',
      reportDigest: marker.digest ? `sha256:${marker.digest}` : 'sha256:malformed',
      reason: 'report_marker_malformed',
    }
  }

  const extraction = extractPayloadFromMarkdown(sourceComment.body, 'agent_run_report/v1')
  if (!extraction.ok) {
    return {
      kind: 'blocked',
      reportDigest: marker.digest ? `sha256:${marker.digest}` : 'sha256:invalid',
      reason: 'report_markdown_extract_failed',
    }
  }

  const reportValidation = validateAgentRunReport(extraction.payload)
  if (!reportValidation.valid) {
    return {
      kind: 'blocked',
      reportDigest: marker.digest ? `sha256:${marker.digest}` : 'sha256:invalid',
      reason: 'report_validator_failed',
    }
  }

  return {
    kind: 'report',
    parsedUrl,
    html_url: sourceComment.html_url,
    reportDigestHex: marker.digest,
    reportDigest: `sha256:${marker.digest}`,
    report: extraction.payload,
    linkedIssueHints: sourceComment.linkedIssueHints ?? [],
    linkedPrHints: sourceComment.linkedPrHints ?? [],
    branchHint: sourceComment.branchHint ?? null,
  }
}

function resolvePrCandidate(normalized, machineRefs, prMetadataByNumber, associatedPrByMergeSha) {
  const candidates = []

  if (normalized.parsedUrl.kind === 'pull') {
    candidates.push(normalized.parsedUrl.number)
  }
  candidates.push(...machineRefs.prRefs)

  const uniqueCandidates = [...new Set(candidates)]
  if (uniqueCandidates.length !== 1) {
    return {
      status: uniqueCandidates.length > 1 ? 'ambiguous' : 'orphan',
      prNumber: null,
      prMeta: null,
    }
  }

  let prNumber = uniqueCandidates[0]
  let prMeta = prMetadataByNumber.get(prNumber) ?? null
  const mergeSha = prMeta?.mergeSha ?? null
  if (mergeSha && associatedPrByMergeSha.has(mergeSha)) {
    prNumber = associatedPrByMergeSha.get(mergeSha)
    prMeta = prMetadataByNumber.get(prNumber) ?? prMeta
  }

  return {
    status: 'resolved',
    prNumber,
    prMeta,
  }
}

function resolveIssueCandidate(normalized, machineRefs, prMeta, parentChildIssueNumbers) {
  const closingIssues = parseClosingIssueNumbers(prMeta?.body ?? '')
  if (closingIssues.length > 1) {
    return { status: 'ambiguous', issueNumber: null }
  }
  if (closingIssues.length === 1) {
    return { status: 'resolved', issueNumber: closingIssues[0] }
  }

  if (machineRefs.issueRefs.length > 1) {
    return { status: 'ambiguous', issueNumber: null }
  }
  if (machineRefs.issueRefs.length === 1) {
    return { status: 'resolved', issueNumber: machineRefs.issueRefs[0] }
  }

  if (normalized.parsedUrl.kind === 'issues' && parentChildIssueNumbers.has(normalized.parsedUrl.number)) {
    return { status: 'resolved', issueNumber: normalized.parsedUrl.number }
  }

  const branchHintIssue = parseBranchHintIssue(normalized.branchHint ?? prMeta?.headRefName ?? '')
  if (branchHintIssue) {
    return { status: 'resolved', issueNumber: branchHintIssue }
  }

  if (normalized.parsedUrl.kind === 'issues') {
    return { status: 'resolved', issueNumber: normalized.parsedUrl.number }
  }

  return { status: 'orphan', issueNumber: null }
}

function summarizeResult(index, sourceCommentRefs, canonicalIndexDigest, sourceCommentSetDigest) {
  return {
    status: index.generation_verdict === 'blocked' ? 'blocked' : 'ok',
    generation_verdict: index.generation_verdict,
    entry_count: index.entries.length,
    orphan_count: index.orphan_reports.length,
    ambiguous_count: index.ambiguous_links.length,
    canonical_index_digest: canonicalIndexDigest,
    source_comment_set_digest: sourceCommentSetDigest,
    source_comment_refs: sourceCommentRefs.map((ref) => ref.comment_url),
  }
}

export function buildRetroIndex({
  sourceComments,
  parentIssue,
  prMetadataByNumber = new Map(),
  associatedPrByMergeSha = new Map(),
  parentChildIssueNumbers = [],
}) {
  const entries = []
  const orphanReports = []
  const ambiguousLinks = []
  const blockedReasons = []
  const sourceCommentRefs = []
  const parentChildSet = new Set(parentChildIssueNumbers)

  for (const rawComment of sourceComments) {
    const normalized = normalizeSourceComment(rawComment)
    if (normalized.kind === 'ignored') {
      continue
    }
    if (normalized.kind === 'blocked') {
      blockedReasons.push({
        report_digest: normalized.reportDigest,
        reason: normalized.reason,
      })
      continue
    }

    sourceCommentRefs.push(buildSourceCommentRef(normalized))

    const machineRefs = collectMachineRefs(normalized.report, normalized)
    const prResolution = resolvePrCandidate(normalized, machineRefs, prMetadataByNumber, associatedPrByMergeSha)
    if (prResolution.status === 'ambiguous') {
      ambiguousLinks.push({
        report_digest: normalized.reportDigest,
        reason: 'multiple pull request candidates matched',
      })
      continue
    }
    if (prResolution.status === 'orphan' || !prResolution.prMeta?.mergeSha) {
      orphanReports.push({
        report_digest: normalized.reportDigest,
        reason: 'pull request unresolved',
      })
      continue
    }

    const issueResolution = resolveIssueCandidate(normalized, machineRefs, prResolution.prMeta, parentChildSet)
    if (issueResolution.status === 'ambiguous') {
      ambiguousLinks.push({
        report_digest: normalized.reportDigest,
        reason: 'multiple issue candidates matched',
      })
      continue
    }
    if (issueResolution.status === 'orphan' || !issueResolution.issueNumber) {
      orphanReports.push({
        report_digest: normalized.reportDigest,
        reason: 'issue unresolved',
      })
      continue
    }

    entries.push({
      report_comment_url: normalized.html_url,
      report_digest: normalized.reportDigest,
      issue: issueResolution.issueNumber,
      pr: prResolution.prNumber,
      merge_sha: prResolution.prMeta.mergeSha,
      tags: buildTags(normalized.report),
      friction_summary: summarizeCommand(normalized.report),
      quality_signals: buildQualitySignals(normalized.report),
      follow_up_issues: buildFollowUpIssues(normalized.report, issueResolution.issueNumber),
    })
  }

  const retroPayload = {
    schema: 'agent_retro_index/v1',
    generation_verdict: blockedReasons.length > 0
      ? 'blocked'
      : (entries.length === 0 || orphanReports.length > 0 || ambiguousLinks.length > 0 ? 'partial' : 'complete'),
    entries,
    orphan_reports: orphanReports,
    ambiguous_links: ambiguousLinks,
  }

  const canonicalIndexJson = JSON.stringify(retroPayload, null, 2)
  const canonicalIndexDigest = sha256Digest(canonicalIndexJson)
  const sourceCommentSetDigest = sha256Digest(canonicalizeSourceCommentSet(sourceCommentRefs))

  return {
    parentIssue,
    algorithmVersion: RETRO_INDEX_ALGORITHM,
    index: retroPayload,
    sourceCommentRefs,
    canonicalIndexDigest,
    sourceCommentSetDigest,
    blockedReasons,
    summary: summarizeResult(retroPayload, sourceCommentRefs, canonicalIndexDigest, sourceCommentSetDigest),
  }
}

export function detectSchemaMigrationRequirement(indexCandidate) {
  const allowedKeys = new Set(['schema', 'generation_verdict', 'entries', 'orphan_reports', 'ambiguous_links'])
  const extraKeys = Object.keys(indexCandidate).filter((key) => !allowedKeys.has(key))
  if (extraKeys.length === 0) {
    return null
  }
  return {
    status: 'blocked',
    reason: `${SCHEMA_MIGRATION_FOLLOW_UP}: ${extraKeys.join(', ')}`,
  }
}

export { canonicalizeSourceCommentSet, sha256Digest, sha256Hex }
