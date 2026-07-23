import { spawnSync } from 'child_process'
import { createHash } from 'crypto'
import { readFile } from 'fs/promises'
import { fileURLToPath } from 'url'
import { dirname, resolve as resolvePath } from 'path'

import { CliError, parseArgs, usageError } from './agent-logs/lib/args.mjs'

const SCHEMA = 'chatgpt_retro_context_live_assertion/v1'
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const RESOLVER_SCRIPT_PATH = resolvePath(SCRIPT_DIR, 'agent-logs/lib/chatgpt-retro-context-marker-helper.mjs')
const DEFAULT_TIMEOUT_MS = 30000
const DEFAULT_MAX_BUFFER = 10 * 1024 * 1024

const CLI_OPTION_SPEC = {
  '--repo': { key: 'repo', required: true },
  '--target-type': { key: 'targetType', required: true },
  '--target-number': { key: 'targetNumber', required: true },
  '--parent-issue': { key: 'parentIssue', required: true },
  '--marker-comment-url': { key: 'markerCommentUrl', required: true },
  '--expected-digest': { key: 'expectedDigest', required: true },
  '--expected-payload-digest': { key: 'expectedPayloadDigest', required: true },
  '--expected-matched-comment-count': { key: 'expectedMatchedCommentCount', required: true },
  '--execution-profile': { key: 'executionProfile', defaultValue: 'live' },
  '--fixture-resolve-result-json': { key: 'fixtureResolveResultJson' },
  '--timeout-ms': { key: 'timeoutMs', defaultValue: String(DEFAULT_TIMEOUT_MS) },
}

function normalizeTargetType(value) {
  if (value !== 'issue' && value !== 'pull_request') {
    throw usageError('chatgpt_retro_context_live_assertion.target_type', 'target type must be issue or pull_request')
  }
  return value
}

function normalizePositiveInteger(value, code) {
  if (!/^[1-9][0-9]*$/u.test(String(value))) {
    throw usageError(code, `${code} must be a positive integer`)
  }
  return Number(value)
}

function normalizeNonNegativeInteger(value, code) {
  if (!/^(0|[1-9][0-9]*)$/u.test(String(value))) {
    throw usageError(code, `${code} must be a non-negative integer`)
  }
  return Number(value)
}

function normalizeExecutionProfile(value) {
  if (value !== 'live' && value !== 'fixture') {
    throw usageError('chatgpt_retro_context_live_assertion.execution_profile', 'execution-profile must be live or fixture')
  }
  return value
}

/**
 * Pure domain assertion. Given a `resolveChatgptRetroContextLive` result
 * (or an equivalent fixture-loaded object with the same shape) and the
 * caller's expected identity, checks:
 * - repo / target / parent_issue / marker_comment_url identity
 * - comment_chain digest / payload_digest / matched_comment_count identity
 * - comment_chain.status and (for pull_request targets) pr_review_surface.status
 *   are both `resolved`
 * - every applicable pagination completeness field is explicitly `true`
 *
 * Never converts a domain failure, component mismatch, or pagination
 * incompleteness into a passing assertion.
 */
export function assertChatgptRetroContextLiveResult(resolveResult, expected) {
  const errors = []

  function check(path, actual, expectedValue) {
    if (actual !== expectedValue) {
      errors.push({
        path,
        code: 'chatgpt_retro_context_live_assertion.identity_mismatch',
        message: `expected ${path} to equal ${JSON.stringify(expectedValue)} but got ${JSON.stringify(actual)}`,
      })
    }
  }

  check('repo', resolveResult?.repo, expected.repo)
  check('target.type', resolveResult?.target?.type, expected.targetType)
  check('target.number', resolveResult?.target?.number, expected.targetNumber)
  check('parent_issue', resolveResult?.parent_issue, expected.parentIssue)
  check('marker_comment_url', resolveResult?.marker_comment_url, expected.markerCommentUrl)
  check('comment_chain.digest', resolveResult?.comment_chain?.digest, expected.digest)
  check('comment_chain.payload_digest', resolveResult?.comment_chain?.payload_digest, expected.payloadDigest)
  check('comment_chain.matched_comment_count', resolveResult?.comment_chain?.matched_comment_count, expected.matchedCommentCount)

  const isPullRequestTarget = expected.targetType === 'pull_request'
  const commentChainStatus = resolveResult?.comment_chain?.status
  if (commentChainStatus !== 'resolved') {
    errors.push({
      path: 'comment_chain.status',
      code: 'chatgpt_retro_context_live_assertion.comment_chain_not_resolved',
      message: `comment_chain.status is ${JSON.stringify(commentChainStatus)}, expected "resolved"`,
    })
  }

  const prReviewSurfaceStatus = resolveResult?.pr_review_surface?.status
  if (isPullRequestTarget && prReviewSurfaceStatus !== 'resolved') {
    errors.push({
      path: 'pr_review_surface.status',
      code: 'chatgpt_retro_context_live_assertion.pr_review_surface_not_resolved',
      message: `pr_review_surface.status is ${JSON.stringify(prReviewSurfaceStatus)}, expected "resolved"`,
    })
  }
  if (!isPullRequestTarget && prReviewSurfaceStatus !== 'not_applicable') {
    errors.push({
      path: 'pr_review_surface.status',
      code: 'chatgpt_retro_context_live_assertion.pr_review_surface_unexpected',
      message: `pr_review_surface.status is ${JSON.stringify(prReviewSurfaceStatus)}, expected "not_applicable" for an issue target`,
    })
  }

  const paginationChecks = [
    ['comment_chain.pagination.comments_complete', resolveResult?.comment_chain?.pagination?.comments_complete],
    ['comment_chain.pagination.reference_comments_complete', resolveResult?.comment_chain?.pagination?.reference_comments_complete],
  ]
  if (isPullRequestTarget) {
    paginationChecks.push(['pr_review_surface.pagination.complete', resolveResult?.pr_review_surface?.pagination?.complete])
  }
  for (const [path, value] of paginationChecks) {
    if (value !== true) {
      errors.push({
        path,
        code: 'chatgpt_retro_context_live_assertion.pagination_incomplete',
        message: `${path} is ${JSON.stringify(value)}, expected explicit boolean true`,
      })
    }
  }

  return {
    ok: errors.length === 0,
    errors,
  }
}

function classifySpawnFailure(spawnResult) {
  if (spawnResult.error) {
    if (spawnResult.error.code === 'ENOBUFS' || spawnResult.error.code === 'E2BIG') {
      return { code: 'buffer_exceeded', message: `resolver subprocess output exceeded the buffer limit: ${spawnResult.error.message}` }
    }
    if (spawnResult.error.code === 'ETIMEDOUT') {
      return { code: 'timeout', message: `resolver subprocess timed out: ${spawnResult.error.message}` }
    }
    return { code: 'spawn_failed', message: `resolver subprocess failed to spawn: ${spawnResult.error.message}` }
  }
  if (spawnResult.signal) {
    if (spawnResult.signal === 'SIGTERM' || spawnResult.signal === 'SIGKILL') {
      return { code: 'timeout', message: `resolver subprocess was terminated by signal ${spawnResult.signal} (bounded timeout exceeded)` }
    }
    return { code: 'signal_terminated', message: `resolver subprocess was terminated by signal ${spawnResult.signal}` }
  }
  return null
}

function runResolverSubprocess({ repo, targetType, targetNumber, parentIssue, markerCommentUrl, timeoutMs }) {
  const args = [
    RESOLVER_SCRIPT_PATH,
    '--command', 'resolve-live',
    '--repo', repo,
    '--target-type', targetType,
    '--target-number', String(targetNumber),
    '--parent-issue', String(parentIssue),
  ]
  if (markerCommentUrl) {
    args.push('--marker-comment-url', markerCommentUrl)
  }
  return spawnSync(process.execPath, args, {
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: timeoutMs,
    killSignal: 'SIGTERM',
    maxBuffer: DEFAULT_MAX_BUFFER,
  })
}

function resolveGitCommit() {
  const result = spawnSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf-8' })
  if (result.status !== 0 || typeof result.stdout !== 'string') {
    return null
  }
  return result.stdout.trim() || null
}

function computeCommandArgsDigest(expected) {
  const canonical = JSON.stringify(expected, Object.keys(expected).sort())
  return `sha256:${createHash('sha256').update(canonical, 'utf8').digest('hex')}`
}

function buildOutput({
  assertionStatus,
  executionProfile,
  expected,
  resolveResult = null,
  domainErrors = [],
  errorCode = null,
  errorMessage = null,
  includeLiveMetadata = false,
}) {
  const output = {
    schema: SCHEMA,
    assertion_status: assertionStatus,
    execution_profile: executionProfile,
    live_evidence_eligible: assertionStatus === 'pass' && executionProfile === 'live',
    repo: expected.repo,
    target: { type: expected.targetType, number: expected.targetNumber },
    parent_issue: expected.parentIssue,
    resolve_result: resolveResult,
    errors: domainErrors,
    error_code: errorCode,
    error_message: errorMessage,
    checked_at: null,
    resolver_commit: null,
    command_args_digest: null,
  }
  if (includeLiveMetadata) {
    output.checked_at = new Date().toISOString()
    output.resolver_commit = resolveGitCommit()
    output.command_args_digest = computeCommandArgsDigest(expected)
  }
  return output
}

function exitWith(output, exitCode) {
  console.log(JSON.stringify(output))
  process.exit(exitCode)
}

async function loadFixtureResolveResult(filePath) {
  let raw
  try {
    raw = await readFile(filePath, 'utf-8')
  } catch (error) {
    throw new CliError('chatgpt_retro_context_live_assertion.fixture_read_failed', `failed to read fixture-resolve-result-json: ${error.message}`, 2)
  }
  try {
    return JSON.parse(raw)
  } catch (error) {
    throw new CliError('chatgpt_retro_context_live_assertion.fixture_json_invalid', `fixture-resolve-result-json is not valid JSON: ${error.message}`, 2)
  }
}

async function runCli() {
  const options = parseArgs(process.argv.slice(2), CLI_OPTION_SPEC)

  const expected = {
    repo: options.repo,
    targetType: normalizeTargetType(options.targetType),
    targetNumber: normalizePositiveInteger(options.targetNumber, 'chatgpt_retro_context_live_assertion.target_number'),
    parentIssue: normalizePositiveInteger(options.parentIssue, 'chatgpt_retro_context_live_assertion.parent_issue'),
    markerCommentUrl: options.markerCommentUrl,
    digest: options.expectedDigest,
    payloadDigest: options.expectedPayloadDigest,
    matchedCommentCount: normalizeNonNegativeInteger(options.expectedMatchedCommentCount, 'chatgpt_retro_context_live_assertion.expected_matched_comment_count'),
  }
  const executionProfile = normalizeExecutionProfile(options.executionProfile)
  const timeoutMs = normalizePositiveInteger(options.timeoutMs, 'chatgpt_retro_context_live_assertion.timeout_ms')

  if (executionProfile === 'fixture') {
    if (!options.fixtureResolveResultJson) {
      throw usageError('chatgpt_retro_context_live_assertion.fixture_resolve_result_json_required', '--fixture-resolve-result-json is required when --execution-profile is fixture')
    }
    const resolveResult = await loadFixtureResolveResult(options.fixtureResolveResultJson)
    const assertion = assertChatgptRetroContextLiveResult(resolveResult, expected)
    return exitWith(buildOutput({
      assertionStatus: assertion.ok ? 'pass' : 'fail',
      executionProfile,
      expected,
      resolveResult,
      domainErrors: assertion.errors,
    }), assertion.ok ? 0 : 1)
  }

  const spawnResult = runResolverSubprocess({ ...expected, timeoutMs })
  const spawnFailure = classifySpawnFailure(spawnResult)
  if (spawnFailure) {
    return exitWith(buildOutput({
      assertionStatus: 'error',
      executionProfile,
      expected,
      errorCode: `chatgpt_retro_context_live_assertion.${spawnFailure.code}`,
      errorMessage: spawnFailure.message,
      includeLiveMetadata: true,
    }), 2)
  }

  const stdout = typeof spawnResult.stdout === 'string' ? spawnResult.stdout.trim() : ''
  let parsedStdout = null
  if (stdout.length > 0) {
    try {
      parsedStdout = JSON.parse(stdout)
    } catch {
      parsedStdout = null
    }
  }

  if (spawnResult.status !== 0) {
    const errorCode = parsedStdout?.error_code ?? 'chatgpt_retro_context_live_assertion.resolver_nonzero_exit'
    const errorMessage = parsedStdout?.error_message
      ?? `resolver subprocess exited with status ${spawnResult.status}: ${(spawnResult.stderr ?? '').trim() || stdout}`
    return exitWith(buildOutput({
      assertionStatus: 'error',
      executionProfile,
      expected,
      errorCode,
      errorMessage,
      includeLiveMetadata: true,
    }), 2)
  }

  if (parsedStdout === null) {
    return exitWith(buildOutput({
      assertionStatus: 'error',
      executionProfile,
      expected,
      errorCode: 'chatgpt_retro_context_live_assertion.invalid_json_output',
      errorMessage: 'resolver subprocess did not emit a single parsable JSON object on stdout',
      includeLiveMetadata: true,
    }), 2)
  }

  const assertion = assertChatgptRetroContextLiveResult(parsedStdout, expected)
  return exitWith(buildOutput({
    assertionStatus: assertion.ok ? 'pass' : 'fail',
    executionProfile,
    expected,
    resolveResult: parsedStdout,
    domainErrors: assertion.errors,
    includeLiveMetadata: true,
  }), assertion.ok ? 0 : 1)
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  runCli().catch((error) => {
    const isCliError = error instanceof CliError
    console.log(JSON.stringify({
      schema: SCHEMA,
      assertion_status: 'error',
      execution_profile: null,
      live_evidence_eligible: false,
      error_code: isCliError ? error.code : 'chatgpt_retro_context_live_assertion.unexpected_error',
      error_message: error?.message ?? 'unexpected runtime failure',
      errors: [],
      resolve_result: null,
      checked_at: null,
      resolver_commit: null,
      command_args_digest: null,
    }))
    process.exit(2)
  })
}
