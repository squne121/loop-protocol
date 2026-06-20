import { createHash } from 'crypto'

import { runtimeError } from './args.mjs'

/**
 * Section priority order (fixed).
 * Lower index = higher priority.
 */
export const SECTION_PRIORITY_ORDER = [
  'safety_header',
  'source_manifest',
  'parent_goal',
  'priority_signals',
  'ci_review_loops',
  'evidence_refs',
  'lower_priority_narrative',
  'omission_report',
]

/**
 * Compute a short digest of section content for omission report.
 * @param {string} content
 * @returns {string}
 */
function sectionDigest(content) {
  return `sha256:${createHash('sha256').update(content).digest('hex').slice(0, 32)}`
}

/**
 * Apply budget constraints to a set of sections.
 *
 * @param {object[]} sections Array of { id, priority, content } objects
 * @param {object} budget { maxChars, maxSections }
 * @returns {{ kept: object[], omitted: object[] }}
 */
export function applyBudget(sections, budget) {
  const { maxChars, maxSections } = budget

  // Sort by priority (fixed order)
  const sorted = [...sections].sort((a, b) => {
    const ai = SECTION_PRIORITY_ORDER.indexOf(a.id)
    const bi = SECTION_PRIORITY_ORDER.indexOf(b.id)
    // Unknown sections go to the end
    const aIdx = ai === -1 ? SECTION_PRIORITY_ORDER.length : ai
    const bIdx = bi === -1 ? SECTION_PRIORITY_ORDER.length : bi
    return aIdx - bIdx
  })

  const kept = []
  const omitted = []
  let totalChars = 0
  let totalSections = 0

  for (const section of sorted) {
    const sectionChars = section.content.length

    // Check if adding this section would exceed budget
    if (totalSections >= maxSections || totalChars + sectionChars > maxChars) {
      omitted.push({
        section_id: section.id,
        reason: totalSections >= maxSections ? 'max_sections_exceeded' : 'max_chars_exceeded',
        priority: SECTION_PRIORITY_ORDER.indexOf(section.id),
        original_chars: sectionChars,
        omitted_digest: sectionDigest(section.content),
      })
    } else {
      kept.push(section)
      totalChars += sectionChars
      totalSections += 1
    }
  }

  return { kept, omitted }
}

/**
 * Validate that the budget is sufficient to hold the minimum required sections:
 * safety_header, source_manifest, and priority_signals.
 *
 * Blocker 2 fix: source_manifest is now part of the required minimum frame.
 *
 * @param {object[]} sections
 * @param {object} budget
 * @throws {CliError} with code 'budget.too_small'
 */
export function assertBudgetSufficient(sections, budget) {
  const required = sections.filter(
    (s) => s.id === 'safety_header' || s.id === 'source_manifest' || s.id === 'priority_signals'
  )

  const requiredChars = required.reduce((sum, s) => sum + s.content.length, 0)
  const requiredSections = required.length

  if (requiredSections > budget.maxSections || requiredChars > budget.maxChars) {
    throw runtimeError(
      'budget.too_small',
      `budget too small to hold safety_header + source_manifest + priority_signals: ` +
        `need ${requiredChars} chars / ${requiredSections} sections, ` +
        `budget is ${budget.maxChars} chars / ${budget.maxSections} sections`
    )
  }
}
