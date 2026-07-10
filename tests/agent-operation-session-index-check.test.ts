import { spawnSync } from 'child_process'
import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'
import {
  validateAgentOperationSessionIndex,
  validateAgentOperationSessionIndexSemantics,
} from '../scripts/check-agent-operation-session-index.mjs'

const FIXTURES_DIR = resolve(__dirname, 'fixtures/agent-operation-session-index')
const REPO_ROOT = resolve(__dirname, '..')
const CHECKER_SCRIPT = resolve(REPO_ROOT, 'scripts/check-agent-operation-session-index.mjs')

function readFixture(name: string) {
  return JSON.parse(readFileSync(resolve(FIXTURES_DIR, name), 'utf-8'))
}

function cloneFixture(name: string) {
  return JSON.parse(JSON.stringify(readFixture(name)))
}

describe('agent_operation_session_index/v1 checker: valid fixtures (AC1, AC3, AC5, AC6)', () => {
  const validFixtures = [
    'valid-issue-operation.json',
    'valid-pr-operation.json',
    'valid-pr-review-submitted.json',
    'valid-pr-review-comment-created.json',
    'valid-pr-review-thread-resolved.json',
  ]

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

  it('GIVEN valid-pr-review-submitted.json THEN operation.source.kind is github_pull_request_review', () => {
    const payload = readFixture('valid-pr-review-submitted.json')
    expect(payload.operation.kind).toBe('pr_review_submitted')
    expect(payload.operation.source.kind).toBe('github_pull_request_review')
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

  it('GIVEN a PR review operation whose top-level verification.resolver_status is not resolved THEN resolver.status_not_resolved is raised', () => {
    const mutated = cloneFixture('valid-pr-review-submitted.json')
    mutated.verification.resolver_status = 'stale'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'resolver.status_not_resolved')).toBe(true)
  })

  it('GIVEN a run_id containing a local absolute path THEN public safety scan fails', () => {
    const mutated = cloneFixture('valid-issue-operation.json')
    mutated.agent_run.run_id = '/home/squne/projects/LOOP_PROTOCOL/run-1405'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code.startsWith('path.'))).toBe(true)
  })

  it('GIVEN pr_review_submitted but operation.source.kind masquerades as a review comment THEN source.kind_mismatch is raised', () => {
    const mutated = cloneFixture('valid-pr-review-submitted.json')
    mutated.operation.source.kind = 'github_pull_request_review_comment'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'source.kind_mismatch')).toBe(true)
  })

  it('GIVEN pr_review_submitted references a pending review THEN review.state_pending is raised', () => {
    const mutated = cloneFixture('valid-pr-review-submitted.json')
    mutated.operation.source.state = 'PENDING'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'review.state_pending')).toBe(true)
  })

  it('GIVEN pr_review_thread_resolved references an unresolved thread THEN review_thread.unresolved is raised', () => {
    const mutated = cloneFixture('valid-pr-review-thread-resolved.json')
    mutated.operation.source.is_resolved = false
    mutated.operation.source.origin.is_resolved = false
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'review_thread.unresolved')).toBe(true)
  })

  it('GIVEN a PR review operation with operation_source_resolver pagination incomplete THEN resolver.pagination_incomplete is raised', () => {
    const mutated = cloneFixture('valid-pr-review-comment-created.json')
    mutated.verification.operation_source_resolver.pagination.review_comments_complete = false
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'resolver.pagination_incomplete')).toBe(true)
  })

  it('GIVEN a PR review operation with duplicate review comment IDs in source_catalog THEN resolver.duplicate_source_id is raised', () => {
    const mutated = cloneFixture('valid-pr-review-comment-created.json')
    mutated.verification.operation_source_resolver.source_catalog.review_comment_ids = [3558855703, 3558855703]
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'resolver.duplicate_source_id')).toBe(true)
  })

  it('GIVEN pr_review_comment_created with a pull number mismatch THEN source.target_mismatch is raised', () => {
    const mutated = cloneFixture('valid-pr-review-comment-created.json')
    mutated.operation.source.pull_number = 1412
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'source.target_mismatch')).toBe(true)
  })

  it('GIVEN pr_review_submitted with null source.digest THEN schema validation fails', () => {
    const mutated = cloneFixture('valid-pr-review-submitted.json')
    mutated.operation.source.digest = null
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'schema.invalid')).toBe(true)
  })

  it('GIVEN a PR review comment source path differs from the resolver object catalog THEN source.object_mismatch is raised', () => {
    const mutated = cloneFixture('valid-pr-review-comment-created.json')
    mutated.operation.source.path = 'docs/dev/agent-run-report.md'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'source.object_mismatch')).toBe(true)
  })

  it('GIVEN source.commit_id and resolver.target_commit are jointly mutated away from the resolver object catalog THEN source.object_mismatch is raised', () => {
    const mutated = cloneFixture('valid-pr-review-submitted.json')
    mutated.operation.source.commit_id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    mutated.verification.operation_source_resolver.target_commit = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    const result = validateAgentOperationSessionIndexSemantics(mutated)
    expect(result.valid).toBe(false)
    expect(result.errors.some((e) => e.code === 'source.object_mismatch')).toBe(true)
  })
})

describe('agent_operation_session_index/v1 checker CLI: process exit code (P0-1, Issue #1405 OWNER review)', () => {
  // Regression guard for a false-green class of bug: in-memory function-level tests
  // alone cannot catch a `failures` counter regression in main()'s CLI wiring. This
  // spawns the real CLI entry point (child_process.spawnSync) against fixture files
  // and asserts the actual process.exit code, not just the exported validator result.
  it('GIVEN a valid fixture file WHEN the CLI is invoked THEN it exits 0', () => {
    const result = spawnSync('node', [CHECKER_SCRIPT, resolve(FIXTURES_DIR, 'valid-issue-operation.json')], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
    })
    expect(result.status).toBe(0)
    expect(result.stdout).toContain('PASS')
  })

  it('GIVEN an invalid fixture file (missing required properties) WHEN the CLI is invoked THEN it exits non-zero (schema.required)', () => {
    const result = spawnSync(
      'node',
      [CHECKER_SCRIPT, resolve(FIXTURES_DIR, 'invalid-missing-required.json')],
      { cwd: REPO_ROOT, encoding: 'utf-8' },
    )
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('FAIL')
    expect(result.stderr).toContain('schema.required')
  })
})

describe('agent_operation_session_index/v1 schema: PR review operation gate', () => {
  it('GIVEN operation.kind = "pr_review" WHEN validated THEN schema rejects it (not in the closed enum)', () => {
    const mutated = cloneFixture('valid-pr-operation.json')
    mutated.operation.kind = 'pr_review'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
  })

  it('GIVEN operation.kind = "pr_review_addressed" WHEN validated THEN schema rejects it (out of scope derived operation)', () => {
    const mutated = cloneFixture('valid-pr-operation.json')
    mutated.operation.kind = 'pr_review_addressed'
    const result = validateAgentOperationSessionIndex(mutated)
    expect(result.valid).toBe(false)
  })
})
