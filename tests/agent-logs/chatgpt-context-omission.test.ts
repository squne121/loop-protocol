import { describe, expect, it } from 'vitest'

import { applyBudget } from '../../scripts/agent-logs/lib/chatgpt-context-budget.mjs'

function makeSection(id: string, chars: number) {
  return { id, content: 'x'.repeat(chars) }
}

describe('chatgpt-context omission (AC3)', () => {
  it('GIVEN sections exceeding budget WHEN applying budget THEN omitted has section_id', () => {
    const sections = [
      makeSection('safety_header', 50),
      makeSection('lower_priority_narrative', 10000),
    ]
    const { omitted } = applyBudget(sections, { maxChars: 100, maxSections: 10 })
    expect(omitted).toHaveLength(1)
    expect(typeof omitted[0].section_id).toBe('string')
    expect(omitted[0].section_id).toBe('lower_priority_narrative')
  })

  it('GIVEN sections exceeding budget WHEN applying budget THEN omitted has reason', () => {
    const sections = [
      makeSection('safety_header', 50),
      makeSection('lower_priority_narrative', 10000),
    ]
    const { omitted } = applyBudget(sections, { maxChars: 100, maxSections: 10 })
    expect(omitted).toHaveLength(1)
    expect(typeof omitted[0].reason).toBe('string')
    expect(omitted[0].reason.length).toBeGreaterThan(0)
  })

  it('GIVEN sections exceeding budget WHEN applying budget THEN omitted has priority integer', () => {
    const sections = [
      makeSection('safety_header', 50),
      makeSection('lower_priority_narrative', 10000),
    ]
    const { omitted } = applyBudget(sections, { maxChars: 100, maxSections: 10 })
    expect(Number.isInteger(omitted[0].priority)).toBe(true)
  })

  it('GIVEN sections exceeding budget WHEN applying budget THEN omitted has original_chars', () => {
    const sections = [
      makeSection('safety_header', 50),
      makeSection('lower_priority_narrative', 10000),
    ]
    const { omitted } = applyBudget(sections, { maxChars: 100, maxSections: 10 })
    expect(omitted[0].original_chars).toBe(10000)
  })

  it('GIVEN sections exceeding budget WHEN applying budget THEN omitted has omitted_digest', () => {
    const sections = [
      makeSection('safety_header', 50),
      makeSection('lower_priority_narrative', 10000),
    ]
    const { omitted } = applyBudget(sections, { maxChars: 100, maxSections: 10 })
    expect(typeof omitted[0].omitted_digest).toBe('string')
    expect(omitted[0].omitted_digest).toMatch(/^sha256:/)
  })

  it('GIVEN different content WHEN omitted THEN omitted_digest differs', () => {
    const sections1 = [
      makeSection('safety_header', 50),
      makeSection('lower_priority_narrative', 200),
    ]
    const sections2 = [
      makeSection('safety_header', 50),
      makeSection('lower_priority_narrative', 200),
    ]
    // Override content to be different
    sections1[1].content = 'a'.repeat(200)
    sections2[1].content = 'b'.repeat(200)

    const { omitted: o1 } = applyBudget(sections1, { maxChars: 100, maxSections: 10 })
    const { omitted: o2 } = applyBudget(sections2, { maxChars: 100, maxSections: 10 })

    expect(o1[0].omitted_digest).not.toBe(o2[0].omitted_digest)
  })

  it('GIVEN no budget violation WHEN applying budget THEN omitted is empty', () => {
    const sections = [makeSection('safety_header', 50)]
    const { omitted } = applyBudget(sections, { maxChars: 10000, maxSections: 10 })
    expect(omitted).toHaveLength(0)
  })
})
