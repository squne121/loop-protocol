import { describe, expect, it } from 'vitest'
import { createValidReport } from './agent-run-report-test-helpers'
import { validateAgentRunReport } from '../scripts/lib/agent-run-report-validation.mjs'

describe('authority semantic validation', () => {
  it('GIVEN actor.type ai_agent and authority.level authoritative WHEN validated THEN report is rejected', () => {
    const report = createValidReport()
    report.authority.level = 'authoritative'
    report.authority.basis = 'human_attestation'
    report.authority.evidence_refs = ['comment:1']
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'semantic.ai_authority_level')).toBe(true)
  })

  it('GIVEN authority.level derived without evidence refs WHEN validated THEN report is rejected', () => {
    const report = createValidReport()
    report.actor.type = 'github_action'
    report.authority.level = 'derived'
    report.authority.basis = 'github_action_check'
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'semantic.authority_evidence_refs_required')).toBe(true)
  })

  it('GIVEN authority.level derived with non-deterministic evidence ref WHEN validated THEN report is rejected', () => {
    const report = createValidReport()
    report.actor.type = 'github_action'
    report.authority.level = 'derived'
    report.authority.basis = 'github_action_check'
    report.authority.evidence_refs = [
      {
        kind: 'workflow_run',
        artifact_id: null,
        artifact_digest: null,
        workflow_run_url: null,
        schema_ref: null,
        ref: 'trust me',
        digest: null,
        validation_verdict: 'unknown',
      },
    ]
    const result = validateAgentRunReport(report)
    expect(result.valid).toBe(false)
    expect(result.errors.some((error) => error.code === 'semantic.opaque_ref_not_deterministic')).toBe(true)
  })
})
