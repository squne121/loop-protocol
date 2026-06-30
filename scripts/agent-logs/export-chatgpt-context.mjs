#!/usr/bin/env node
/**
 * export-chatgpt-context.mjs
 *
 * Generate a public-safe, deterministic, budget-controlled Markdown context bundle
 * for ChatGPT consumption from agent run artifacts.
 *
 * AC compliance summary:
 *   AC1  — forbidden field scan in loadSources (raw_transcript, stdout, etc.)
 *   AC2  — priority-aware budget via applyBudget / SECTION_PRIORITY_ORDER
 *   AC3  — omission report with section_id, reason, priority, original_chars, omitted_digest
 *   AC4  — run reports sorted deterministically (by basename path, then run_id, then started_at)
 *   AC5  — evidence ref dedupe via dedupeEvidenceRefs (canonical key); duplicate_of +
 *           used_by_sections in both bundle and header (Blocker 6)
 *   AC6  — priority_signals retained; budget.too_small fail if cannot hold
 *           safety_header + source_manifest + priority_signals (Blocker 2)
 *   AC7  — transcript_hotspot_summary only allowed when public_safety.redaction_status === clean
 *           AND evidence_digest must be sha256:<64hex> (Blocker 7)
 *   AC8  — source_manifest with source_kind/source_ref(logical)/canonical_digest/body_digest
 *   AC9  — SECURITY_BOUNDARY header; DATA fenced blocks; enhanced final rendered scan (Blocker 5)
 *   AC10 — machine-readable chatgpt_context_bundle/v1 YAML block at bundle head with all
 *           required fields via 2-pass render (Blocker 1)
 */

import { mkdir, open, rm } from 'fs/promises'
import { basename, dirname, resolve } from 'path'
import { randomUUID } from 'crypto'

import { printCliError, runtimeError } from './lib/args.mjs'
import { parseChatgptContextArgs } from './lib/chatgpt-context-args.mjs'
import { loadSources } from './lib/chatgpt-context-source-loader.mjs'
import { resolveChatgptRetroContextFromFixtures } from './lib/chatgpt-retro-context-marker-helper.mjs'
import { scanRenderedMarkdown } from './lib/chatgpt-context-safety-scan.mjs'
import { dedupeEvidenceRefs } from './lib/chatgpt-context-dedupe.mjs'
import { applyBudget, assertBudgetSufficient, SECTION_PRIORITY_ORDER } from './lib/chatgpt-context-budget.mjs'
import {
  renderSafetyHeader,
  renderSourceManifest,
  renderParentGoal,
  renderPrioritySignals,
  renderCiReviewLoops,
  renderEvidenceRefs,
  renderLowerPriorityNarrative,
  renderOmissionReport,
} from './lib/chatgpt-context-renderer.mjs'

/**
 * Sort run reports deterministically.
 * Primary sort: source_ref/path (basename ascending).
 * Secondary sort: run_id ascending.
 * Tertiary: started_at ascending.
 *
 * Blocker "High: deterministic sort" fix: sort contract is now
 * source_ref/path → run_id → started_at (matching this comment and implementation).
 *
 * @param {object[]} reports
 * @returns {object[]}
 */
function sortRunReports(reports) {
  return [...reports].sort((a, b) => {
    // Primary: source_ref basename (already sorted by loadSources, but normalise here too)
    const aRef = String(a._source_ref ?? '')
    const bRef = String(b._source_ref ?? '')
    if (aRef < bRef) return -1
    if (aRef > bRef) return 1
    // Secondary: run_id
    const aId = String(a.run_id ?? a.draft?.run_id ?? '')
    const bId = String(b.run_id ?? b.draft?.run_id ?? '')
    if (aId < bId) return -1
    if (aId > bId) return 1
    // Tertiary: started_at
    const aAt = String(a.draft?.started_at ?? '')
    const bAt = String(b.draft?.started_at ?? '')
    return aAt < bAt ? -1 : aAt > bAt ? 1 : 0
  })
}

/**
 * Validate transcript_hotspot_summary references (AC7).
 * Only allowed when public_safety.redaction_status === 'clean'.
 * When present, evidence_digest must be sha256:<64 hex chars> format (Blocker 7).
 * @param {object[]} runReports
 */
function validateTranscriptHotspots(runReports) {
  const EVIDENCE_DIGEST_RE = /^sha256:[0-9a-f]{64}$/

  for (const report of runReports) {
    const hotspot = report.transcript_hotspot_summary
    if (hotspot !== undefined && hotspot !== null) {
      const redactionStatus = report.public_safety?.redaction_status
      if (redactionStatus !== 'clean') {
        throw runtimeError(
          'ac7.transcript_hotspot_not_clean',
          `transcript_hotspot_summary present in run report but public_safety.redaction_status is not 'clean' (got: ${redactionStatus})`
        )
      }

      // Blocker 7: evidence_digest must be present and well-formed when transcript_hotspot_summary exists
      const evidenceDigest = report.evidence_digest
      if (evidenceDigest === undefined || evidenceDigest === null) {
        throw runtimeError(
          'ac7.transcript_hotspot_missing_evidence_digest',
          `transcript_hotspot_summary present but evidence_digest is missing (must be sha256:<64hex>)`
        )
      }
      if (!EVIDENCE_DIGEST_RE.test(String(evidenceDigest))) {
        throw runtimeError(
          'ac7.transcript_hotspot_invalid_evidence_digest',
          `transcript_hotspot_summary present but evidence_digest does not match sha256:<64hex> format (got: ${evidenceDigest})`
        )
      }
    }
  }
}

/**
 * Write a file atomically (no-overwrite).
 * @param {string} outputPath
 * @param {string} content
 */
async function writeFileAtomic(outputPath, content) {
  const resolvedPath = resolve(outputPath)
  const outputDir = dirname(resolvedPath)
  const tmpPath = resolve(outputDir, `.${basename(resolvedPath)}.${process.pid}.${randomUUID()}.tmp`)

  await mkdir(outputDir, { recursive: true })

  let handle
  try {
    handle = await open(tmpPath, 'wx', 0o600)
    await handle.writeFile(content, { encoding: 'utf-8' })
    await handle.sync()
    await handle.close()
    handle = null

    const { link } = await import('fs/promises')
    await link(tmpPath, resolvedPath)
    await rm(tmpPath, { force: true })
  } catch (err) {
    await handle?.close().catch(() => {})
    await rm(tmpPath, { force: true }).catch(() => {})
    if (err && typeof err === 'object' && err.code === 'EEXIST') {
      throw runtimeError('output.exists', 'refusing to overwrite an existing output file')
    }
    throw err
  }
}

/**
 * Write JSON atomically using the existing writeJsonAtomic helper.
 */
async function writeSummaryJson(summaryJsonOut, summary) {
  const { writeJsonAtomic } = await import('./lib/atomic-json.mjs')
  await writeJsonAtomic(summaryJsonOut, summary)
}

async function main() {
  const options = parseChatgptContextArgs(process.argv.slice(2))

  const budget = {
    maxChars: options.maxChars,
    maxSections: options.maxSections,
  }

  // Step 1+2: Load and validate all sources (forbidden field scan + digest + public safety)
  const { sources, manifest } = options.sourceMode === 'marker_comment'
    ? await resolveChatgptRetroContextFromFixtures({
        markerCommentJson: options.markerCommentJson,
        githubCommentsJson: options.githubCommentsJson,
      })
    : await loadSources(options)

  // Step 3: Validate transcript hotspots (AC7 + Blocker 7)
  validateTranscriptHotspots(sources.run_reports)

  // Step 4: Evidence canonicalization + dedupe (AC5)
  const dedupedRefs = dedupeEvidenceRefs(sources.evidence_refs)

  // Step 5: Sort run reports deterministically (AC4)
  // Sort: source_ref/path → run_id → started_at
  const sortedRunReports = sortRunReports(sources.run_reports)

  // Determine target/parent issue numbers for header
  const targetIssueNumber = sources.target_issue_json?.number ?? sources.target_issue_json?.issue_number ?? ''
  const parentIssueNumber = sources.parent_issue_json?.number ?? sources.parent_issue_json?.issue_number ?? ''

  const headerMetaBase = {
    generated_at: options.generatedAt,
    issue: `#${targetIssueNumber}`,
    parent_issue: `#${parentIssueNumber}`,
    max_chars: budget.maxChars,
    max_sections: budget.maxSections,
  }

  // Step 6: Build section graph (1st pass — safety_header uses placeholder rendered_chars=0)
  const sectionBuilders = [
    {
      id: 'safety_header',
      // 1st pass: rendered_chars unknown yet; will be replaced in 2nd pass
      content: renderSafetyHeader(headerMetaBase, { renderedChars: 0, sources: [], omittedSections: [] }),
    },
    {
      id: 'source_manifest',
      content: renderSourceManifest(manifest),
    },
    {
      id: 'parent_goal',
      content: renderParentGoal(sources.parent_issue_json, sources.target_issue_json),
    },
    {
      id: 'priority_signals',
      content: renderPrioritySignals(sources.retro_index_json),
    },
    {
      id: 'ci_review_loops',
      content: renderCiReviewLoops(sources.retro_index_json),
    },
    {
      id: 'evidence_refs',
      content: renderEvidenceRefs(dedupedRefs),
    },
    {
      id: 'lower_priority_narrative',
      content: renderLowerPriorityNarrative(sortedRunReports),
    },
  ]

  // Step 6b: Validate budget can hold minimum required sections (AC6 / Blocker 2)
  assertBudgetSufficient(sectionBuilders, budget)

  // Step 7: Priority-aware budget allocation (AC2, AC3)
  const { kept, omitted } = applyBudget(sectionBuilders, budget)

  // Always add omission report at end if there are omissions (not subject to budget cut)
  const omissionContent = renderOmissionReport(omitted)
  if (omissionContent) {
    kept.push({ id: 'omission_report', content: omissionContent })
  }

  // Step 8: Render all sections except safety_header (1st pass)
  const sortedKept = kept.sort((a, b) => {
    const ai = SECTION_PRIORITY_ORDER.indexOf(a.id)
    const bi = SECTION_PRIORITY_ORDER.indexOf(b.id)
    const aIdx = ai === -1 ? SECTION_PRIORITY_ORDER.length : ai
    const bIdx = bi === -1 ? SECTION_PRIORITY_ORDER.length : bi
    return aIdx - bIdx
  })

  const nonHeaderSections = sortedKept.filter((s) => s.id !== 'safety_header')
  const bodyMarkdown = nonHeaderSections.map((s) => s.content).join('\n')

  // Step 8b: 2nd pass — regenerate safety_header with actual rendered_chars and full stats
  const omittedSectionIds = omitted.map((o) => o.section_id)
  const sourcesForHeader = manifest.map((m) => ({ kind: m.source_kind, digest: m.body_digest }))

  const finalHeader = renderSafetyHeader(headerMetaBase, {
    renderedChars: bodyMarkdown.length,
    sources: sourcesForHeader,
    omittedSections: omittedSectionIds,
  })

  // Step 8c: Compose final bundle
  const bundleMarkdown = [finalHeader, bodyMarkdown].join('\n')

  // Step 8d: Blocker 2 — verify final bundle does not exceed max_chars after omission report
  if (bundleMarkdown.length > budget.maxChars) {
    throw runtimeError(
      'budget.too_small',
      `final bundle (${bundleMarkdown.length} chars) exceeds max_chars (${budget.maxChars}) after adding required sections`
    )
  }

  // Step 9: Final rendered Markdown scan (AC9 / Blocker 5)
  // Also scan summary JSON to avoid absolute paths leaking there
  scanRenderedMarkdown(bundleMarkdown)

  // Step 10: Atomic no-overwrite output (AC1, AC9)
  await writeFileAtomic(options.outputPath, bundleMarkdown)

  // Step 11: Compact summary JSON output
  // Blocker "High: summary no absolute path" fix: output_path removed
  const uniqueRefs = dedupedRefs.filter((r) => !r.duplicate_of)
  const duplicateRefs = dedupedRefs.filter((r) => r.duplicate_of)

  // Build duplicate ref detail for AC5 machine-readable header (Blocker 6)
  const duplicateRefDetail = duplicateRefs.map((r) => ({
    kind: r.kind ?? 'unknown',
    duplicate_of: r.duplicate_of,
    canonical_key_digest: r.canonical_key_digest ?? null,
    used_by_sections: r.used_by_sections ?? [],
  }))

  const summary = {
    schema: 'chatgpt_context_bundle_summary/v1',
    generated_at: options.generatedAt,
    issue: `#${targetIssueNumber}`,
    parent_issue: `#${parentIssueNumber}`,
    // output_path intentionally omitted — no absolute paths in summary (Blocker High)
    output_basename: basename(options.outputPath),
    budget: {
      max_chars: budget.maxChars,
      max_sections: budget.maxSections,
      used_chars: bundleMarkdown.length,
      used_sections: kept.filter((s) => s.id !== 'omission_report').length,
    },
    sections_kept: kept.map((s) => s.id),
    omitted_sections: omitted,
    source_manifest: manifest,
    evidence_refs: {
      total: dedupedRefs.length,
      unique: uniqueRefs.length,
      duplicates: duplicateRefs.length,
      duplicate_detail: duplicateRefDetail,
    },
  }

  // Scan summary JSON serialization for path leakage
  scanRenderedMarkdown(JSON.stringify(summary))

  await writeSummaryJson(options.summaryJsonOut, summary)

  console.log('chatgpt-context: bundle written')
}

main().catch((error) => {
  process.exit(printCliError('chatgpt-context:export', error))
})
