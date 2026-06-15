import { describe, expect, it } from 'vitest'
import { createValidRetroIndex } from './agent-run-report-test-helpers'
import { validateAgentRetroIndex } from '../scripts/lib/agent-run-report-validation.mjs'

describe('agent_retro_index validation', () => {
  it('GIVEN a valid retro index WHEN validated THEN it passes', () => {
    const result = validateAgentRetroIndex(createValidRetroIndex())
    expect(result.valid).toBe(true)
  })

  it('GIVEN friction_summary containing agent_run_report/v1 WHEN validated THEN semantic validation fails', () => {
    const retro = createValidRetroIndex()
    retro.entries[0].friction_summary = 'agent_run_report/v1 full body copied'
    const result = validateAgentRetroIndex(retro)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'semantic.inline_report_copy')).toBe(true)
  })
})
