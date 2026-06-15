import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'
import { createValidReport, REPORT_FIXTURES_DIR } from './agent-run-report-test-helpers'
import {
  renderPublicMarkdown,
  validateMarkdownCandidate,
} from '../scripts/lib/agent-run-report-validation.mjs'

describe('markdown candidate validation', () => {
  it('GIVEN a valid report WHEN rendered to markdown THEN markdown candidate passes', () => {
    const markdown = renderPublicMarkdown(createValidReport())
    const result = validateMarkdownCandidate(markdown)
    expect(result.valid).toBe(true)
  })

  it('GIVEN duplicate markers markdown WHEN validated THEN candidate fails', () => {
    const markdown = readFileSync(resolve(REPORT_FIXTURES_DIR, 'invalid-duplicate-markers.md'), 'utf-8')
    const result = validateMarkdownCandidate(markdown)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'markdown.duplicate_start_marker')).toBe(true)
  })

  it('GIVEN fence breakout markdown WHEN validated THEN candidate fails', () => {
    const markdown = readFileSync(resolve(REPORT_FIXTURES_DIR, 'invalid-fence-breakout.md'), 'utf-8')
    const result = validateMarkdownCandidate(markdown)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'markdown.fence_mismatch')).toBe(true)
  })
})
