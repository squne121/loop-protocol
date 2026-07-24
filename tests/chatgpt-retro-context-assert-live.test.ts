import { describe, expect, it } from 'vitest'
import { spawnSync } from 'child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { resolve } from 'path'

import {
  assertChatgptRetroContextLiveResult,
  classifySpawnFailure,
  evaluateLiveEvidenceProvenance,
  evaluateResolverSpawnResult,
} from '../scripts/assert-chatgpt-retro-context-live.mjs'

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

  it('GIVEN a pull_request-target result where every individual pr_review_surface pagination field is explicitly true but the aggregate complete flag is inconsistent (false) WHEN asserting THEN it fails closed via the recomputed aggregate mismatch', () => {
    // Fix 2.1: previously only `pr_review_surface.pagination.complete` was
    // checked, so an inconsistent producer that reports every component
    // field true/complete but a wrong (false) aggregate would still be
    // caught only by luck. This asserts the explicit recompute-vs-reported
    // mismatch error fires even when all four component fields pass their
    // own individual `=== true` checks.
    const resolveResult = readFixture('resolved-pull-request.json')
    resolveResult.pr_review_surface = {
      ...resolveResult.pr_review_surface,
      pagination: { ...resolveResult.pr_review_surface.pagination, complete: false },
    }
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

  it('GIVEN a pull_request-target result whose pr_review_surface reports one component pagination field incomplete while the aggregate complete flag is (incorrectly) true WHEN asserting THEN both the individual field check and the recomputed aggregate mismatch fail closed', () => {
    // Reproduces the exact reviewer example: {reviews_complete:true,
    // review_comments_complete:false, review_threads_complete:true,
    // thread_comments_complete:true, complete:true}.
    const resolveResult = readFixture('resolved-pull-request.json')
    resolveResult.pr_review_surface = {
      ...resolveResult.pr_review_surface,
      pagination: { ...resolveResult.pr_review_surface.pagination, review_comments_complete: false, complete: true },
    }
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
      path: 'pr_review_surface.pagination.review_comments_complete',
      code: 'chatgpt_retro_context_live_assertion.pagination_incomplete',
    }))
    expect(assertion.errors).toContainEqual(expect.objectContaining({
      path: 'pr_review_surface.pagination.complete',
      code: 'chatgpt_retro_context_live_assertion.pagination_complete_recompute_mismatch',
    }))
  })
})

describe('classifySpawnFailure (deterministic subprocess failure classification)', () => {
  it('GIVEN error.code ENOBUFS WHEN classifying THEN it is buffer_exceeded', () => {
    expect(classifySpawnFailure({ error: { code: 'ENOBUFS', message: 'stdout maxBuffer exceeded' } })).toMatchObject({ code: 'buffer_exceeded' })
  })

  it('GIVEN error.code E2BIG WHEN classifying THEN it is buffer_exceeded', () => {
    expect(classifySpawnFailure({ error: { code: 'E2BIG', message: 'argument list too long' } })).toMatchObject({ code: 'buffer_exceeded' })
  })

  it('GIVEN error.code ENOENT (resolver script or node binary missing) WHEN classifying THEN it is spawn_failed', () => {
    expect(classifySpawnFailure({ error: { code: 'ENOENT', message: 'spawnSync node ENOENT' } })).toMatchObject({ code: 'spawn_failed' })
  })

  it('GIVEN error.code ETIMEDOUT WHEN classifying THEN it is timeout', () => {
    expect(classifySpawnFailure({ error: { code: 'ETIMEDOUT', message: 'spawnSync node ETIMEDOUT' }, signal: 'SIGTERM' })).toMatchObject({ code: 'timeout' })
  })

  it('GIVEN no error and an externally-sent SIGTERM (e.g. CI job cancellation, not a bounded timeout) WHEN classifying THEN it is signal_terminated, not timeout', () => {
    // Fix 4.1: before this fix, any SIGTERM/SIGKILL was blanket-classified
    // as `timeout` regardless of `error.code`, which would misclassify an
    // externally-sent signal as "our own bounded timeout fired".
    expect(classifySpawnFailure({ error: null, signal: 'SIGTERM' })).toMatchObject({ code: 'signal_terminated' })
  })

  it('GIVEN no error and an externally-sent SIGKILL (e.g. OOM killer, kill -9, not a bounded timeout) WHEN classifying THEN it is signal_terminated, not timeout', () => {
    expect(classifySpawnFailure({ error: null, signal: 'SIGKILL' })).toMatchObject({ code: 'signal_terminated' })
  })

  it('GIVEN no error and an unrelated signal (SIGINT) WHEN classifying THEN it is signal_terminated', () => {
    expect(classifySpawnFailure({ error: null, signal: 'SIGINT' })).toMatchObject({ code: 'signal_terminated' })
  })

  it('GIVEN neither error nor signal WHEN classifying THEN it is null (not a spawn failure)', () => {
    expect(classifySpawnFailure({ error: null, signal: null, status: 0 })).toBeNull()
  })
})

describe('evaluateResolverSpawnResult (deterministic resolver subprocess output handling, dependency-injected spawnResult)', () => {
  const expected = {
    repo: 'squne121/loop-protocol',
    targetType: 'issue',
    targetNumber: 1224,
    parentIssue: 1153,
    markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-41',
    digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    payloadDigest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    matchedCommentCount: 1,
  }

  it('GIVEN a spawn failure (ENOBUFS) WHEN evaluating THEN it exits 2 with assertion_status error and buffer_exceeded', () => {
    const { output, exitCode } = evaluateResolverSpawnResult(
      { error: { code: 'ENOBUFS', message: 'stdout maxBuffer exceeded' }, signal: null, status: null, stdout: '', stderr: '' },
      { expected, executionProfile: 'live' },
    )
    expect(exitCode).toBe(2)
    expect(output.assertion_status).toBe('error')
    expect(output.error_code).toBe('chatgpt_retro_context_live_assertion.buffer_exceeded')
  })

  it('GIVEN the resolver process exits non-zero with a structured machine-readable error on stdout (an ordinary domain failure, not a spawn failure) WHEN evaluating THEN it exits 2 and forwards the resolver-reported error_code', () => {
    const resolverErrorJson = JSON.stringify({ command: 'resolve-live', status: 'error', error_code: 'chatgpt_retro_context.repo', error_message: 'repo must be an owner/name string' })
    const { output, exitCode } = evaluateResolverSpawnResult(
      { error: null, signal: null, status: 1, stdout: resolverErrorJson, stderr: '' },
      { expected, executionProfile: 'live' },
    )
    expect(exitCode).toBe(2)
    expect(output.assertion_status).toBe('error')
    expect(output.error_code).toBe('chatgpt_retro_context.repo')
  })

  it('GIVEN the resolver process exits non-zero with unparsable stdout WHEN evaluating THEN it exits 2 with a resolver_nonzero_exit fallback error_code', () => {
    const { output, exitCode } = evaluateResolverSpawnResult(
      { error: null, signal: null, status: 1, stdout: 'not json', stderr: 'boom' },
      { expected, executionProfile: 'live' },
    )
    expect(exitCode).toBe(2)
    expect(output.assertion_status).toBe('error')
    expect(output.error_code).toBe('chatgpt_retro_context_live_assertion.resolver_nonzero_exit')
    expect(output.error_message).toContain('boom')
  })

  it('GIVEN stdout containing two concatenated JSON objects (multiple JSON, not a single object) WHEN evaluating THEN it exits 2 with invalid_json_output instead of parsing either one', () => {
    const stdout = '{"a":1}\n{"b":2}'
    const { output, exitCode } = evaluateResolverSpawnResult(
      { error: null, signal: null, status: 0, stdout, stderr: '' },
      { expected, executionProfile: 'live' },
    )
    expect(exitCode).toBe(2)
    expect(output.assertion_status).toBe('error')
    expect(output.error_code).toBe('chatgpt_retro_context_live_assertion.invalid_json_output')
  })

  it('GIVEN stdout with extra non-JSON text surrounding a single JSON object WHEN evaluating THEN it exits 2 with invalid_json_output instead of extracting the embedded object', () => {
    const stdout = 'debug: starting resolve-live\n{"status":"resolved"}\ndebug: done'
    const { output, exitCode } = evaluateResolverSpawnResult(
      { error: null, signal: null, status: 0, stdout, stderr: '' },
      { expected, executionProfile: 'live' },
    )
    expect(exitCode).toBe(2)
    expect(output.assertion_status).toBe('error')
    expect(output.error_code).toBe('chatgpt_retro_context_live_assertion.invalid_json_output')
  })

  it('GIVEN stdout with exactly one valid JSON object that fails the domain assertion WHEN evaluating THEN it exits 1 with assertion_status fail (not converted to a pass or an error)', () => {
    const stdout = JSON.stringify({ status: 'missing', repo: 'squne121/loop-protocol', target: { type: 'issue', number: 1224 }, parent_issue: 1153, marker_comment_url: null, comment_chain: { status: 'missing', pagination: { comments_complete: true, reference_comments_complete: null }, digest: null, payload_digest: null, matched_comment_count: 0 }, pr_review_surface: { status: 'not_applicable', pagination: null } })
    const { output, exitCode } = evaluateResolverSpawnResult(
      { error: null, signal: null, status: 0, stdout, stderr: '' },
      { expected, executionProfile: 'live' },
    )
    expect(exitCode).toBe(1)
    expect(output.assertion_status).toBe('fail')
  })
})

describe('evaluateLiveEvidenceProvenance (git commit / dirty-state authenticity for live_evidence_eligible)', () => {
  function initGitRepo() {
    const repoRoot = mkdtempSync(resolve(tmpdir(), 'chatgpt-retro-context-provenance-'))
    const run = (args: string[]) => spawnSync('git', ['-C', repoRoot, ...args], { encoding: 'utf-8' })
    run(['init', '--quiet'])
    run(['config', 'user.email', 'test@example.com'])
    run(['config', 'user.name', 'Test'])
    writeFileSync(resolve(repoRoot, 'file.txt'), 'hello\n')
    run(['add', 'file.txt'])
    run(['commit', '--quiet', '-m', 'initial commit'])
    const commit = run(['rev-parse', 'HEAD']).stdout.trim()
    return { repoRoot, run, commit }
  }

  it('GIVEN a clean repository at a known commit WHEN evaluating provenance THEN it reports eligible:true with that 40-hex commit', () => {
    const { repoRoot, commit } = initGitRepo()
    try {
      expect(evaluateLiveEvidenceProvenance({ repoRoot })).toEqual({ commit, eligible: true })
      expect(commit).toMatch(/^[0-9a-f]{40}$/)
    } finally {
      rmSync(repoRoot, { recursive: true, force: true })
    }
  })

  it('GIVEN a dirty (uncommitted, tracked-file) working tree WHEN evaluating provenance THEN eligible is false even though HEAD resolves to a valid 40-hex commit', () => {
    const { repoRoot, commit } = initGitRepo()
    try {
      writeFileSync(resolve(repoRoot, 'file.txt'), 'hello, modified\n')
      const provenance = evaluateLiveEvidenceProvenance({ repoRoot })
      expect(provenance.commit).toBe(commit)
      expect(provenance.eligible).toBe(false)
    } finally {
      rmSync(repoRoot, { recursive: true, force: true })
    }
  })

  it('GIVEN staged (index) changes not yet committed WHEN evaluating provenance THEN eligible is false', () => {
    const { repoRoot, run } = initGitRepo()
    try {
      writeFileSync(resolve(repoRoot, 'staged.txt'), 'staged content\n')
      run(['add', 'staged.txt'])
      const provenance = evaluateLiveEvidenceProvenance({ repoRoot })
      expect(provenance.eligible).toBe(false)
    } finally {
      rmSync(repoRoot, { recursive: true, force: true })
    }
  })

  it('GIVEN a repoRoot that is a non-toplevel subdirectory of an unrelated outer git repository WHEN evaluating provenance THEN eligible is false due to a repository-root mismatch', () => {
    // Simulates the exact bug this fix closes: the resolver's own directory
    // is not the actual git repository root that HEAD/dirty-state were
    // resolved against.
    const outerRoot = mkdtempSync(resolve(tmpdir(), 'chatgpt-retro-context-provenance-outer-'))
    try {
      const run = (args: string[]) => spawnSync('git', ['-C', outerRoot, ...args], { encoding: 'utf-8' })
      run(['init', '--quiet'])
      run(['config', 'user.email', 'test@example.com'])
      run(['config', 'user.name', 'Test'])
      writeFileSync(resolve(outerRoot, 'root.txt'), 'root\n')
      run(['add', 'root.txt'])
      run(['commit', '--quiet', '-m', 'outer root commit'])
      const nestedDir = resolve(outerRoot, 'scripts')
      mkdirSync(nestedDir)
      const provenance = evaluateLiveEvidenceProvenance({ repoRoot: nestedDir })
      expect(provenance.eligible).toBe(false)
    } finally {
      rmSync(outerRoot, { recursive: true, force: true })
    }
  })

  it('GIVEN a directory that is not a git repository at all WHEN evaluating provenance THEN commit is null and eligible is false', () => {
    const nonGitDir = mkdtempSync(resolve(tmpdir(), 'chatgpt-retro-context-provenance-nongit-'))
    try {
      const provenance = evaluateLiveEvidenceProvenance({ repoRoot: nonGitDir })
      expect(provenance).toEqual({ commit: null, eligible: false })
    } finally {
      rmSync(nonGitDir, { recursive: true, force: true })
    }
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

  it('GIVEN a fixture profile run against the real repository checkout WHEN executing the CLI in the fixture profile THEN live_evidence_eligible is always false even though the checkout itself may be clean (fixture never counts as live evidence)', () => {
    const result = runCli([
      ...ISSUE_EXPECTED_ARGS,
      '--execution-profile', 'fixture',
      '--fixture-resolve-result-json', resolve(FIXTURE_DIR, 'resolved-issue.json'),
    ])
    expect(result.status).toBe(0)
    const parsed = JSON.parse(result.stdout.trim())
    expect(parsed.assertion_status).toBe('pass')
    expect(parsed.execution_profile).toBe('fixture')
    expect(parsed.live_evidence_eligible).toBe(false)
  })

  it('GIVEN a multi-megabyte fixture resolve-result piped through stdout WHEN executing the fixture profile CLI THEN the full JSON payload is written and parsable without truncation (exitWith no longer relies on process.exit() to flush a pipe)', () => {
    // Fix 4.4: `console.log(...); process.exit(exitCode)` can truncate
    // stdout that has not finished flushing when stdout is a pipe (which is
    // exactly how spawnSync captures this CLI's output). This stress test
    // embeds several MB of padding inside a valid resolve-result fixture so
    // a truncated write would produce a JSON.parse failure or a byte-length
    // mismatch, not just a "close enough" result.
    const tempDir = mkdtempSync(resolve(tmpdir(), 'chatgpt-retro-context-stress-'))
    try {
      const baseFixture = readFixture('resolved-issue.json')
      const paddingBytes = 4 * 1024 * 1024
      const stressFixture = {
        ...baseFixture,
        comment_chain: {
          ...baseFixture.comment_chain,
          _stress_padding: 'x'.repeat(paddingBytes),
        },
      }
      const fixturePath = resolve(tempDir, 'stress-resolved-issue.json')
      writeFileSync(fixturePath, JSON.stringify(stressFixture))

      // The default Node spawnSync stdout buffer (1 MiB) is smaller than
      // this stress payload; the harness must raise it explicitly the same
      // way the CLI itself does for the real resolver subprocess, otherwise
      // the *test harness's own* capture (not the CLI under test) would be
      // the one truncating output.
      const result = spawnSync(process.execPath, [SCRIPT_PATH,
        ...ISSUE_EXPECTED_ARGS,
        '--execution-profile', 'fixture',
        '--fixture-resolve-result-json', fixturePath,
      ], {
        encoding: 'utf-8',
        maxBuffer: 32 * 1024 * 1024,
      })

      expect(result.status).toBe(0)
      expect(result.signal).toBeNull()
      const stdoutLines = result.stdout.trim().split('\n')
      expect(stdoutLines.length).toBe(1)
      const parsed = JSON.parse(stdoutLines[0])
      expect(parsed.assertion_status).toBe('pass')
      expect(parsed.resolve_result.comment_chain._stress_padding.length).toBe(paddingBytes)
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  }, 20000)
})
