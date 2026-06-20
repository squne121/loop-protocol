import { describe, expect, it } from 'vitest'

import { applyBudget, SECTION_PRIORITY_ORDER } from '../../scripts/agent-logs/lib/chatgpt-context-budget.mjs'

function makeSection(id: string, chars: number) {
  return { id, content: 'x'.repeat(chars) }
}

describe('chatgpt-context budget (AC2)', () => {
  describe('SECTION_PRIORITY_ORDER', () => {
    it('GIVEN the priority order WHEN inspecting THEN safety_header is first', () => {
      expect(SECTION_PRIORITY_ORDER[0]).toBe('safety_header')
    })

    it('GIVEN the priority order WHEN inspecting THEN source_manifest is second', () => {
      expect(SECTION_PRIORITY_ORDER[1]).toBe('source_manifest')
    })

    it('GIVEN the priority order WHEN inspecting THEN omission_report is last', () => {
      const last = SECTION_PRIORITY_ORDER[SECTION_PRIORITY_ORDER.length - 1]
      expect(last).toBe('omission_report')
    })

    it('GIVEN the priority order WHEN inspecting THEN priority_signals comes before lower_priority_narrative', () => {
      const psIdx = SECTION_PRIORITY_ORDER.indexOf('priority_signals')
      const lpIdx = SECTION_PRIORITY_ORDER.indexOf('lower_priority_narrative')
      expect(psIdx).toBeLessThan(lpIdx)
    })

    it('GIVEN the priority order WHEN inspecting THEN evidence_refs comes before lower_priority_narrative', () => {
      const erIdx = SECTION_PRIORITY_ORDER.indexOf('evidence_refs')
      const lpIdx = SECTION_PRIORITY_ORDER.indexOf('lower_priority_narrative')
      expect(erIdx).toBeLessThan(lpIdx)
    })
  })

  describe('applyBudget', () => {
    it('GIVEN budget larger than all sections WHEN applying THEN all sections are kept', () => {
      const sections = [
        makeSection('safety_header', 100),
        makeSection('source_manifest', 100),
        makeSection('priority_signals', 100),
      ]
      const { kept, omitted } = applyBudget(sections, { maxChars: 10000, maxSections: 10 })
      expect(kept).toHaveLength(3)
      expect(omitted).toHaveLength(0)
    })

    it('GIVEN maxSections=1 WHEN applying THEN only highest priority section is kept', () => {
      const sections = [
        makeSection('lower_priority_narrative', 100),
        makeSection('safety_header', 100),
        makeSection('source_manifest', 100),
      ]
      const { kept, omitted } = applyBudget(sections, { maxChars: 10000, maxSections: 1 })
      expect(kept).toHaveLength(1)
      expect(kept[0].id).toBe('safety_header')
      expect(omitted).toHaveLength(2)
    })

    it('GIVEN maxChars constraint WHEN applying THEN lower priority sections are dropped first', () => {
      const sections = [
        makeSection('safety_header', 50),
        makeSection('source_manifest', 50),
        makeSection('lower_priority_narrative', 50),
      ]
      // Only room for 2 sections by chars
      const { kept, omitted } = applyBudget(sections, { maxChars: 110, maxSections: 10 })
      expect(kept).toHaveLength(2)
      expect(kept.map((s: { id: string }) => s.id)).toContain('safety_header')
      expect(kept.map((s: { id: string }) => s.id)).toContain('source_manifest')
      expect(omitted).toHaveLength(1)
      expect(omitted[0].section_id).toBe('lower_priority_narrative')
    })

    it('GIVEN omitted section WHEN checking result THEN omission has required fields', () => {
      const sections = [
        makeSection('safety_header', 10),
        makeSection('lower_priority_narrative', 10000),
      ]
      const { omitted } = applyBudget(sections, { maxChars: 100, maxSections: 10 })
      expect(omitted).toHaveLength(1)
      const o = omitted[0]
      expect(o).toHaveProperty('section_id')
      expect(o).toHaveProperty('reason')
      expect(o).toHaveProperty('priority')
      expect(o).toHaveProperty('original_chars')
      expect(o).toHaveProperty('omitted_digest')
      expect(typeof o.section_id).toBe('string')
      expect(typeof o.reason).toBe('string')
      expect(typeof o.priority).toBe('number')
      expect(typeof o.original_chars).toBe('number')
      expect(typeof o.omitted_digest).toBe('string')
    })
  })
})
