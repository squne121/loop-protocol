import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'
import {
  validateAgentOperationSessionIndex,
  validateAgentOperationSessionIndexSemantics,
} from '../scripts/check-agent-operation-session-index.mjs'

const FIXTURES_DIR = resolve(__dirname, 'fixtures/agent-operation-session-index')

function readFixture(name: string) {
  return JSON.parse(readFileSync(resolve(FIXTURES_DIR, name), 'utf-8'))
}

function cloneFixture(name: string) {
  return JSON.parse(JSON.stringify(readFixture(name)))
}

describe('agent_operation_session_index/v1 checker: valid fixtures (AC1, AC3, AC5, AC6)', () => {
  const validFixtures = ['valid-issue-operation.json', 'valid-pr-operation.json']

  for (const fixture of validFixtures) {
    it(`GIVEN ${fixture} WHEN validated THEN checker returns valid (AC5/AC6 positive fixture)`, () => {
      const result = validateAgentOperationSessionIndex(readFixture(fixture))
      expect(result.errors).toEqual([])
      expect(result.valid).toBe(true)
    })
  }

  it('GIVEN valid-issue-operation.json THEN target.kind is issue and operation.kind is issue_comment', () => {
    const payload = readFixture('valid-issue-operation.json')
    expect(payload.target.kind).toBe('issue')
    expect(payload.operation.kind).toBe('issue_comment')
  })

  it('GIVEN valid-pr-operation.json THEN target.kind is pull_request and operation.kind is pr_comment', () => {
    const payload = readFixture('valid-pr-operation.json')
    expect(payload.target.kind).toBe('pull_request')
    expect(payload.operation.kind).toBe('pr_comment')
  })
})

describe('agent_operation_session_index/v1 checker: negative fixtures (AC3 semantic invariants, fail-closed)', () => {
  it('GIVEN a missing required field THEN schema validation fails with schema.required', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    delete mutated.public_artifacts.chatgpt_marker_digest
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'schema.required')).toBe(true)
  })

  it('GIVEN an additional undeclared property THEN schema validation fails with schema.unevaluated_property', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.unexpected_extra_field = 'not allowed'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'schema.unevaluated_property')).toBe(true)
  })

  it('GIVEN a run_report_comment_url pointing at the wrong target kind THEN target.kind_mismatch is raised', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.public_artifacts.run_report_comment_url = 'https://github.com/squne121/loop-protocol/pull/1405#issuecomment-4930000001'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'target.kind_mismatch')).toBe(true)
  })

  it('GIVEN a retro_index_comment_url whose number matches neither target.number nor parent_issue THEN target.number_mismatch is raised', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.public_artifacts.retro_index_comment_url = 'https://github.com/squne121/loop-protocol/issues/9999#issuecomment-4930000002'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'target.number_mismatch')).toBe(true)
  })

  it('GIVEN an operation.kind / github_event_ref.kind combination outside the closed mapping THEN event_mapping.invalid is raised', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.operation.github_event_ref.kind = 'workflow_run'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'event_mapping.invalid')).toBe(true)
  })

  it('GIVEN agent_run.raw_values_emitted true (schema violation) THEN it fails with schema.invalid and raw_values_emitted.violation', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.agent_run.raw_values_emitted = true
    const schemaResult = validateAgentOperationSessionIndex(mutated)
    expect(schemaResult.valid).toBe(false)
    const semanticResult = validateAgentOperationSessionIndexSemantics(mutated)
    expect(semanticResult.errors.some((e) => e.code === 'raw_values_emitted.violation')).toBe(true)
  })

  it('GIVEN an evidence_mode outside the closed enum THEN schema validation fails', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.agent_run.evidence_mode = 'adopt_cloud'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
  })

  it('GIVEN a resolver_status outside "resolved" THEN schema still validates (resolver_status is a checker-observed field, not a positive-only value)', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.verification.resolver_status = 'stale'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(true)
  })

  it('GIVEN a run_id containing a local absolute path THEN public safety scan fails', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.agent_run.run_id = '/home/squne/projects/LOOP_PROTOCOL/run-1405'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code.startsWith('path.'))).toBe(true)
  })
})

describe('agent_operation_session_index/v1 schema: pr_review exclusion (AC6, OWNER review indication 2)', () => {
  it('GIVEN operation.kind = "pr_review" WHEN validated THEN schema rejects it (not in the closed enum)', () => {
    const mutated = cloneFixture('valid-pr-operation.json')
    mutated.operation.kind = 'pr_review'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
  })
})
