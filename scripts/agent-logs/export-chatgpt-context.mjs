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
 *   AC4  — run reports sorted deterministically (by file path, then by run_id)
 *   AC5  — evidence ref dedupe via dedupeEvidenceRefs (canonical key)
 *   AC6  — priority_signals retained; budget.too_small fail if cannot hold safety+priority
 *   AC7  — transcript_hotspot_summary only allowed when public_safety.redaction_status === clean
 *   AC8  — source_manifest with source_kind/source_ref/canonical_digest/body_digest
 *   AC9  — SECURITY_BOUNDARY header; DATA fenced blocks; final rendered scan
 *   AC10 — machine-readable chatgpt_context_bundle/v1 YAML block at bundle head
 */

import { mkdir, open, rm } from 'fs/promises'
import { basename, dirname, resolve } from 'path'
import { randomUUID } from 'crypto'

import { printCliError, runtimeError } from './lib/args.mjs'
import { parseChatgptContextArgs } from './lib/chatgpt-context-args.mjs'
import { loadSources } from './lib/chatgpt-context-source-loader.mjs'
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
 * Primary sort: run_id (string ascending). Secondary: started_at ascending.
 * @param {object[]} reports
 * @returns {object[]}
 */
function sortRunReports(reports) {
  return [...reports].sort((a, b) => {
    const aId = String(a.run_id ?? a.draft?.run_id ?? '')
    const bId = String(b.run_id ?? b.draft?.run_id ?? '')
    if (aId < bId) return -1
    if (aId > bId) return 1
    const aAt = String(a.draft?.started_at ?? '')
    const bAt = String(b.draft?.started_at ?? '')
    return aAt < bAt ? -1 : aAt > bAt ? 1 : 0
  })
}

/**
 * Validate transcript_hotspot_summary references (AC7).
 * Only allowed when public_safety.redaction_status === 'clean'.
 * @param {object[]} runReports
 */
function validateTranscriptHotspots(runReports) {
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

  // Step 1+2: Load and validate all sources (forbidden field scan + digest)
  const { sources, manifest } = await loadSources(options)

  // Step 3: Validate transcript hotspots (AC7)
  validateTranscriptHotspots(sources.run_reports)

  // Step 4: Evidence canonicalization + dedupe (AC5)
  const dedupedRefs = dedupeEvidenceRefs(sources.evidence_refs)

  // Step 5: Sort run reports deterministically (AC4)
  const sortedRunReports = sortRunReports(sources.run_reports)

  // Determine target/parent issue numbers for header
  const targetIssueNumber = sources.target_issue_json?.number ?? sources.target_issue_json?.issue_number ?? ''
  const parentIssueNumber = sources.parent_issue_json?.number ?? sources.parent_issue_json?.issue_number ?? ''

  const headerMeta = {
    generated_at: options.generatedAt,
    issue: `#${targetIssueNumber}`,
    parent_issue: `#${parentIssueNumber}`,
  }

  // Step 6: Build section graph
  const sectionBuilders = [
    {
      id: 'safety_header',
      content: renderSafetyHeader(headerMeta),
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

  // Step 6b: Validate budget can hold minimum required sections (AC6)
  assertBudgetSufficient(sectionBuilders, budget)

  // Step 7: Priority-aware budget allocation (AC2, AC3)
  const { kept, omitted } = applyBudget(sectionBuilders, budget)

  // Always add omission report at end if there are omissions (not subject to budget cut)
  const omissionContent = renderOmissionReport(omitted)
  if (omissionContent) {
    kept.push({ id: 'omission_report', content: omissionContent })
  }

  // Step 8: Render final Markdown
  const renderedSections = kept
    .sort((a, b) => {
      const ai = SECTION_PRIORITY_ORDER.indexOf(a.id)
      const bi = SECTION_PRIORITY_ORDER.indexOf(b.id)
      const aIdx = ai === -1 ? SECTION_PRIORITY_ORDER.length : ai
      const bIdx = bi === -1 ? SECTION_PRIORITY_ORDER.length : bi
      return aIdx - bIdx
    })
    .map((s) => s.content)

  const bundleMarkdown = renderedSections.join('\n')

  // Step 9: Final rendered Markdown scan (AC9)
  scanRenderedMarkdown(bundleMarkdown)

  // Step 10: Atomic no-overwrite output (AC1, AC9)
  await writeFileAtomic(options.outputPath, bundleMarkdown)

  // Step 11: Compact summary JSON output
  const uniqueRefs = dedupedRefs.filter((r) => !r.duplicate_of)
  const duplicateRefs = dedupedRefs.filter((r) => r.duplicate_of)

  const summary = {
    schema: 'chatgpt_context_bundle_summary/v1',
    generated_at: options.generatedAt,
    issue: `#${targetIssueNumber}`,
    parent_issue: `#${parentIssueNumber}`,
    output_path: resolve(options.outputPath),
    budget: {
      max_chars: budget.maxChars,
      max_sections: budget.maxSections,
      used_chars: kept.reduce((sum, s) => sum + s.content.length, 0),
      used_sections: kept.filter((s) => s.id !== 'omission_report').length,
    },
    sections_kept: kept.map((s) => s.id),
    omitted_sections: omitted,
    source_manifest: manifest,
    evidence_refs: {
      total: dedupedRefs.length,
      unique: uniqueRefs.length,
      duplicates: duplicateRefs.length,
    },
  }

  await writeSummaryJson(options.summaryJsonOut, summary)

  console.log('chatgpt-context: bundle written')
}

main().catch((error) => {
  process.exit(printCliError('chatgpt-context:export', error))
})
