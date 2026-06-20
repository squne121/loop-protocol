import { describe, expect, it } from 'vitest'

import { buildDedupeKey, dedupeEvidenceRefs } from '../../scripts/agent-logs/lib/chatgpt-context-dedupe.mjs'

function makeRef(overrides: Record<string, unknown> = {}) {
  return {
    kind: 'workflow_run',
    ref: 'https://github.com/squne121/loop-protocol/actions/runs/123',
    digest: 'sha256:aabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccdd',
    ...overrides,
  }
}

describe('chatgpt-context evidence ref dedupe (AC5)', () => {
  describe('buildDedupeKey', () => {
    it('GIVEN two identical refs WHEN building keys THEN keys are equal', () => {
      const ref = makeRef()
      expect(buildDedupeKey(ref)).toBe(buildDedupeKey(ref))
    })

    it('GIVEN refs with different kinds WHEN building keys THEN keys differ', () => {
      const ref1 = makeRef({ kind: 'workflow_run' })
      const ref2 = makeRef({ kind: 'pr_comment' })
      expect(buildDedupeKey(ref1)).not.toBe(buildDedupeKey(ref2))
    })

    it('GIVEN refs with same URL but tracking params WHEN building keys THEN keys are equal', () => {
      const ref1 = makeRef({ ref: 'https://github.com/org/repo?utm_source=email' })
      const ref2 = makeRef({ ref: 'https://github.com/org/repo?utm_medium=social' })
      expect(buildDedupeKey(ref1)).toBe(buildDedupeKey(ref2))
    })

    it('GIVEN refs with different digests WHEN building keys THEN keys differ', () => {
      const ref1 = makeRef({ digest: 'sha256:' + 'aa'.repeat(32) })
      const ref2 = makeRef({ digest: 'sha256:' + 'bb'.repeat(32) })
      expect(buildDedupeKey(ref1)).not.toBe(buildDedupeKey(ref2))
    })

    it('GIVEN refs with different comment_ids WHEN building keys THEN keys differ', () => {
      const ref1 = makeRef({ comment_id: '123' })
      const ref2 = makeRef({ comment_id: '456' })
      expect(buildDedupeKey(ref1)).not.toBe(buildDedupeKey(ref2))
    })

    it('GIVEN refs with different artifact_ids WHEN building keys THEN keys differ', () => {
      const ref1 = makeRef({ artifact_id: 'art-001' })
      const ref2 = makeRef({ artifact_id: 'art-002' })
      expect(buildDedupeKey(ref1)).not.toBe(buildDedupeKey(ref2))
    })
  })

  describe('dedupeEvidenceRefs', () => {
    it('GIVEN empty array WHEN deduping THEN returns empty array', () => {
      expect(dedupeEvidenceRefs([])).toHaveLength(0)
    })

    it('GIVEN unique refs WHEN deduping THEN all refs are kept with duplicate_of null', () => {
      const refs = [
        makeRef({ kind: 'workflow_run', ref: 'https://github.com/a', digest: 'sha256:' + 'aa'.repeat(32) }),
        makeRef({ kind: 'pr_comment', ref: 'https://github.com/b', digest: 'sha256:' + 'bb'.repeat(32) }),
      ]
      const result = dedupeEvidenceRefs(refs)
      expect(result).toHaveLength(2)
      expect(result.every((r: { duplicate_of: unknown }) => r.duplicate_of === null)).toBe(true)
    })

    it('GIVEN duplicate refs WHEN deduping THEN duplicate has duplicate_of set', () => {
      const ref = makeRef()
      const refs = [ref, { ...ref }]
      const result = dedupeEvidenceRefs(refs)
      expect(result).toHaveLength(2)
      expect(result[0].duplicate_of).toBeNull()
      expect(result[1].duplicate_of).not.toBeNull()
    })

    it('GIVEN all refs WHEN deduping THEN all refs have used_by_sections array', () => {
      const refs = [makeRef(), makeRef({ kind: 'pr_review' })]
      const result = dedupeEvidenceRefs(refs)
      expect(result.every((r: { used_by_sections: unknown }) => Array.isArray(r.used_by_sections))).toBe(true)
    })

    it('GIVEN duplicate ref WHEN deduping THEN duplicate_of is a non-empty string', () => {
      const ref = makeRef()
      const refs = [ref, { ...ref }]
      const result = dedupeEvidenceRefs(refs)
      const dup = result.find((r: { duplicate_of: unknown }) => r.duplicate_of !== null)
      expect(typeof dup?.duplicate_of).toBe('string')
      expect((dup?.duplicate_of as string).length).toBeGreaterThan(0)
    })

    it('GIVEN refs with tracking URLs WHEN deduping THEN refs are identified as duplicates', () => {
      const ref1 = makeRef({ ref: 'https://github.com/org/repo?utm_source=email' })
      const ref2 = makeRef({ ref: 'https://github.com/org/repo?utm_medium=social' })
      const result = dedupeEvidenceRefs([ref1, ref2])
      expect(result).toHaveLength(2)
      const dup = result.find((r: { duplicate_of: unknown }) => r.duplicate_of !== null)
      expect(dup).toBeDefined()
    })
  })
})
