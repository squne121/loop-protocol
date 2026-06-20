import { describe, expect, it } from 'vitest'

import { applyBudget, assertBudgetSufficient } from '../../scripts/agent-logs/lib/chatgpt-context-budget.mjs'
import { renderPrioritySignals } from '../../scripts/agent-logs/lib/chatgpt-context-renderer.mjs'

function makeSection(id: string, chars: number) {
  return { id, content: 'x'.repeat(chars) }
}

describe('chatgpt-context budget too small (AC6)', () => {
  describe('assertBudgetSufficient', () => {
    it('GIVEN adequate budget WHEN asserting THEN does not throw', () => {
      const sections = [
        makeSection('safety_header', 100),
        makeSection('source_manifest', 100),
        makeSection('priority_signals', 100),
        makeSection('lower_priority_narrative', 100),
      ]
      expect(() => assertBudgetSufficient(sections, { maxChars: 10000, maxSections: 10 })).not.toThrow()
    })

    it('GIVEN budget too small for safety_header WHEN asserting THEN throws budget.too_small', () => {
      const sections = [
        makeSection('safety_header', 5000),
        makeSection('source_manifest', 5000),
        makeSection('priority_signals', 5000),
      ]
      expect(() => assertBudgetSufficient(sections, { maxChars: 100, maxSections: 10 }))
        .toThrow()
    })

    it('GIVEN budget too small for safety_header WHEN asserting THEN error code is budget.too_small', () => {
      const sections = [
        makeSection('safety_header', 5000),
        makeSection('source_manifest', 5000),
        makeSection('priority_signals', 5000),
      ]
      let caughtCode: string | undefined
      try {
        assertBudgetSufficient(sections, { maxChars: 100, maxSections: 10 })
      } catch (err) {
        caughtCode = (err as { code?: string }).code
      }
      expect(caughtCode).toBe('budget.too_small')
    })

    it('GIVEN maxSections=0 WHEN asserting THEN throws budget.too_small', () => {
      const sections = [
        makeSection('safety_header', 10),
        makeSection('source_manifest', 10),
        makeSection('priority_signals', 10),
      ]
      let caughtCode: string | undefined
      try {
        assertBudgetSufficient(sections, { maxChars: 10000, maxSections: 0 })
      } catch (err) {
        caughtCode = (err as { code?: string }).code
      }
      expect(caughtCode).toBe('budget.too_small')
    })

    it('GIVEN budget with room for only 1 section when 3 required WHEN asserting THEN throws', () => {
      const sections = [
        makeSection('safety_header', 10),
        makeSection('source_manifest', 10),
        makeSection('priority_signals', 10),
      ]
      let threw = false
      try {
        assertBudgetSufficient(sections, { maxChars: 10000, maxSections: 1 })
      } catch {
        threw = true
      }
      expect(threw).toBe(true)
    })

    // Blocker 2: source_manifest is now part of required minimum frame
    it('GIVEN sections without source_manifest WHEN budget fits safety_header+priority_signals only THEN does not throw', () => {
      // If source_manifest is not in sections list, assertBudgetSufficient only counts what's present
      const sections = [
        makeSection('safety_header', 10),
        makeSection('priority_signals', 10),
        // no source_manifest
      ]
      // Should pass since we only have 2 required sections and budget is big enough
      expect(() => assertBudgetSufficient(sections, { maxChars: 10000, maxSections: 10 })).not.toThrow()
    })

    it('GIVEN sections with source_manifest WHEN budget too small for 3 sections WHEN asserting THEN throws', () => {
      const sections = [
        makeSection('safety_header', 100),
        makeSection('source_manifest', 100),
        makeSection('priority_signals', 100),
      ]
      let caughtCode: string | undefined
      try {
        // maxSections=2 is too small for 3 required sections
        assertBudgetSufficient(sections, { maxChars: 10000, maxSections: 2 })
      } catch (err) {
        caughtCode = (err as { code?: string }).code
      }
      expect(caughtCode).toBe('budget.too_small')
    })
  })

  describe('priority signals retained (AC6)', () => {
    it('GIVEN large budget WHEN applying budget THEN priority_signals is retained', () => {
      const sections = [
        makeSection('safety_header', 50),
        makeSection('priority_signals', 50),
        makeSection('lower_priority_narrative', 50),
      ]
      const { kept } = applyBudget(sections, { maxChars: 10000, maxSections: 10 })
      expect(kept.map((s: { id: string }) => s.id)).toContain('priority_signals')
    })

    it('GIVEN tight budget WHEN applying budget THEN priority_signals is kept before lower_priority_narrative', () => {
      const sections = [
        makeSection('safety_header', 50),
        makeSection('priority_signals', 50),
        makeSection('lower_priority_narrative', 50),
      ]
      // Budget fits exactly 2 sections by count
      const { kept, omitted } = applyBudget(sections, { maxChars: 10000, maxSections: 2 })
      expect(kept.map((s: { id: string }) => s.id)).toContain('priority_signals')
      expect(omitted.map((o: { section_id: string }) => o.section_id)).toContain('lower_priority_narrative')
    })

    it('GIVEN friction_signals in retro_index WHEN rendering priority signals THEN Friction Signals heading appears in content', () => {
      const retroIndex = {
        friction_signals: [{ kind: 'retry', count: 3 }],
        context_pollution_signals: [],
      }
      const content = renderPrioritySignals(retroIndex)
      // The renderer outputs a heading "Friction Signals" and a DATA block with the JSON
      expect(content).toContain('Friction Signals')
      expect(content).toContain('## Priority Signals')
    })
  })
})
