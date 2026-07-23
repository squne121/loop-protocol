import { describe, expect, it } from 'vitest'
import { spawnSync } from 'child_process'
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { assertChatgptRetroContextLiveResult } from '../scripts/assert-chatgpt-retro-context-live.mjs'

function readFixture(fileName: string) {
  return JSON.parse(readFileSync(resolve(FIXTURE_DIR, fileName), 'utf-8'))
}

const SCRIPT_PATH = resolve(process.cwd(), 'scripts/assert-chatgpt-retro-context-live.mjs')
const FIXTURE_DIR = resolve(process.cwd(), 'tests/fixtures/chatgpt-retro-context')

function runCli(args: string[]) {
  const result = spawnSync(process.execPath, [SCRIPT_PATH, ...args], {
    encoding: 'utf-8',
  })
  return result
}

const ISSUE_EXPECTED_ARGS = [
  '--repo', 'squne121/loop-protocol',
  '--target-type', 'issue',
  '--target-number', '1224',
  '--parent-issue', '1153',
  '--marker-comment-url', 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
  '--expected-digest', 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  '--expected-payload-digest', 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  '--expected-matched-comment-count', '1',
]

const PULL_REQUEST_EXPECTED_ARGS = [
  '--repo', 'squne121/loop-protocol',
  '--target-type', 'pull_request',
  '--target-number', '1224',
  '--parent-issue', '1153',
  '--marker-comment-url', 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
  '--expected-digest', 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  '--expected-payload-digest', 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  '--expected-matched-comment-count', '1',
]

describe('assertChatgptRetroContextLiveResult (pure domain assertion)', () => {
  it('GIVEN a fully resolved issue-target result matching expectations WHEN asserting THEN it passes', () => {
    const resolveResult = readFixture('resolved-issue.json')
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
      digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      matchedCommentCount: 1,
    })
    expect(assertion).toEqual({ ok: true, errors: [] })
  })

  it('GIVEN a resolved result with a mismatched digest WHEN asserting THEN it fails closed with an identity_mismatch error', () => {
    const resolveResult = readFixture('resolved-issue.json')
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
      digest: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
      payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      matchedCommentCount: 1,
    })
    expect(assertion.ok).toBe(false)
    expect(assertion.errors).toContainEqual(expect.objectContaining({
      path: 'comment_chain.digest',
      code: 'chatgpt_retro_context_live_assertion.identity_mismatch',
    }))
  })

  it('GIVEN a missing marker result WHEN asserting THEN comment_chain_not_resolved is reported', () => {
    const resolveResult = readFixture('missing-issue.json')
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
      digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      matchedCommentCount: 1,
    })
    expect(assertion.ok).toBe(false)
    expect(assertion.errors).toContainEqual(expect.objectContaining({
      path: 'comment_chain.status',
      code: 'chatgpt_retro_context_live_assertion.comment_chain_not_resolved',
    }))
  })

  it('GIVEN an issue-target result whose comment-chain reference pagination is not explicitly true WHEN asserting THEN it fails closed', () => {
    const resolveResult = readFixture('pagination-incomplete-issue.json')
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
      digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      matchedCommentCount: 1,
    })
    expect(assertion.ok).toBe(false)
    expect(assertion.errors).toContainEqual(expect.objectContaining({
      path: 'comment_chain.pagination.reference_comments_complete',
      code: 'chatgpt_retro_context_live_assertion.pagination_incomplete',
    }))
  })

  it('GIVEN a pull_request-target result whose pr_review_surface pagination is not explicitly complete WHEN asserting THEN it fails closed', () => {
    const resolveResult = readFixture('pagination-incomplete-pull-request.json')
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, {
      repo: 'squne121/loop-protocol',
      targetType: 'pull_request',
      targetNumber: 1224,
      parentIssue: 1153,
      markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
      digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      matchedCommentCount: 1,
    })
    expect(assertion.ok).toBe(false)
    expect(assertion.errors).toContainEqual(expect.objectContaining({
      path: 'pr_review_surface.pagination.complete',
      code: 'chatgpt_retro_context_live_assertion.pagination_incomplete',
    }))
  })

  it('GIVEN a fully resolved pull_request-target result matching expectations WHEN asserting THEN it passes', () => {
    const resolveResult = readFixture('resolved-pull-request.json')
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, {
      repo: 'squne121/loop-protocol',
      targetType: 'pull_request',
      targetNumber: 1224,
      parentIssue: 1153,
      markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
      digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      matchedCommentCount: 1,
    })
    expect(assertion).toEqual({ ok: true, errors: [] })
  })

  it('GIVEN an issue-target result with a resolved pr_review_surface WHEN asserting THEN pr_review_surface_unexpected is reported', () => {
    const resolveResult = readFixture('resolved-issue.json')
    resolveResult.pr_review_surface = { ...resolveResult.pr_review_surface, status: 'resolved' }
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, {
      repo: 'squne121/loop-protocol',
      targetType: 'issue',
      targetNumber: 1224,
      parentIssue: 1153,
      markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
      digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      matchedCommentCount: 1,
    })
    expect(assertion.ok).toBe(false)
    expect(assertion.errors).toContainEqual(expect.objectContaining({
      path: 'pr_review_surface.status',
      code: 'chatgpt_retro_context_live_assertion.pr_review_surface_unexpected',
    }))
  })
})

describe('chatgpt-retro-context:assert-live CLI (subprocess regression)', () => {
  it('GIVEN a valid resolved issue fixture WHEN executing the fixture profile THEN it exits 0 with assertion_status pass and live_evidence_eligible false', () => {
    const result = runCli([
      ...ISSUE_EXPECTED_ARGS,
      '--execution-profile', 'fixture',
      '--fixture-resolve-result-json', resolve(FIXTURE_DIR, 'resolved-issue.json'),
    ])
    expect(result.status).toBe(0)
    expect(result.signal).toBeNull()
    const stdoutLines = result.stdout.trim().split('\n')
    expect(stdoutLines.length).toBe(1)
    const parsed = JSON.parse(stdoutLines[0])
    expect(parsed).toMatchObject({
      schema: 'chatgpt_retro_context_live_assertion/v1',
      assertion_status: 'pass',
      execution_profile: 'fixture',
      live_evidence_eligible: false,
    })
  })

  it('GIVEN a pagination-incomplete fixture WHEN executing the fixture profile THEN it exits 1 with assertion_status fail', () => {
    const result = runCli([
      ...ISSUE_EXPECTED_ARGS,
      '--execution-profile', 'fixture',
      '--fixture-resolve-result-json', resolve(FIXTURE_DIR, 'pagination-incomplete-issue.json'),
    ])
    expect(result.status).toBe(1)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('fail')
    expect(parsed.errors.length).toBeGreaterThan(0)
  })

  it('GIVEN a missing required option WHEN executing the CLI THEN it exits 2 with assertion_status error', () => {
    const result = runCli([
      '--repo', 'squne121/loop-protocol',
      '--target-type', 'issue',
      '--target-number', '1224',
      '--parent-issue', '1153',
    ])
    expect(result.status).toBe(2)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('error')
    expect(parsed.error_code).toBe('cli.required_option')
  })

  it('GIVEN an execution-profile of fixture without --fixture-resolve-result-json WHEN executing the CLI THEN it exits 2', () => {
    const result = runCli([
      ...ISSUE_EXPECTED_ARGS,
      '--execution-profile', 'fixture',
    ])
    expect(result.status).toBe(2)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('error')
    expect(parsed.error_code).toBe('chatgpt_retro_context_live_assertion.fixture_resolve_result_json_required')
  })

  it('GIVEN a fixture file that is not valid JSON WHEN executing the CLI THEN it exits 2 with a fixture_json_invalid error', () => {
    const result = runCli([
      ...ISSUE_EXPECTED_ARGS,
      '--execution-profile', 'fixture',
      '--fixture-resolve-result-json', resolve(FIXTURE_DIR, 'invalid.json'),
    ])
    expect(result.status).toBe(2)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('error')
    expect(parsed.error_code).toBe('chatgpt_retro_context_live_assertion.fixture_json_invalid')
  })

  it('GIVEN an unreadable execution-profile value WHEN executing the CLI THEN it exits 2', () => {
    const result = runCli([
      ...ISSUE_EXPECTED_ARGS,
      '--execution-profile', 'not-a-real-profile',
    ])
    expect(result.status).toBe(2)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('error')
  })

  it('GIVEN a resolved pull-request fixture WHEN executing the fixture profile THEN it exits 0', () => {
    const result = runCli([
      ...PULL_REQUEST_EXPECTED_ARGS,
      '--execution-profile', 'fixture',
      '--fixture-resolve-result-json', resolve(FIXTURE_DIR, 'resolved-pull-request.json'),
    ])
    expect(result.status).toBe(0)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('pass')
  })

  it('GIVEN a pull-request pagination-incomplete fixture WHEN executing the fixture profile THEN it exits 1', () => {
    const result = runCli([
      ...PULL_REQUEST_EXPECTED_ARGS,
      '--execution-profile', 'fixture',
      '--fixture-resolve-result-json', resolve(FIXTURE_DIR, 'pagination-incomplete-pull-request.json'),
    ])
    expect(result.status).toBe(1)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('fail')
  })

  it('GIVEN an execution-profile of live with an unreachably small timeout WHEN executing the CLI THEN the resolver subprocess is bounded and classified as a timeout error', () => {
    const result = runCli([
      ...ISSUE_EXPECTED_ARGS,
      '--execution-profile', 'live',
      '--timeout-ms', '1',
    ])
    expect(result.status).toBe(2)
    expect(result.signal).toBeNull()
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('error')
    expect(parsed.execution_profile).toBe('live')
    expect(parsed.live_evidence_eligible).toBe(false)
    expect(parsed.error_code).toBe('chatgpt_retro_context_live_assertion.timeout')
    expect(parsed.checked_at).toEqual(expect.any(String))
    expect(parsed.command_args_digest).toMatch(/^sha256:[0-9a-f]{64}$/)
  }, 10000)
})
