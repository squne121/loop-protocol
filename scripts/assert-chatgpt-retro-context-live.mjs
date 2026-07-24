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
    // Check every component pagination-completeness field individually
    // (not just the aggregate `complete` value) -- an inconsistent producer
    // could report every component field incomplete-but-aggregate-true (or
    // vice versa) and a pure aggregate check would not catch it.
    const prReviewSurfacePagination = resolveResult?.pr_review_surface?.pagination
    paginationChecks.push(['pr_review_surface.pagination.reviews_complete', prReviewSurfacePagination?.reviews_complete])
    paginationChecks.push(['pr_review_surface.pagination.review_comments_complete', prReviewSurfacePagination?.review_comments_complete])
    paginationChecks.push(['pr_review_surface.pagination.review_threads_complete', prReviewSurfacePagination?.review_threads_complete])
    paginationChecks.push(['pr_review_surface.pagination.thread_comments_complete', prReviewSurfacePagination?.thread_comments_complete])
    paginationChecks.push(['pr_review_surface.pagination.complete', prReviewSurfacePagination?.complete])
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

  if (isPullRequestTarget) {
    const prReviewSurfacePagination = resolveResult?.pr_review_surface?.pagination ?? {}
    const recomputedComplete = prReviewSurfacePagination.reviews_complete === true
      && prReviewSurfacePagination.review_comments_complete === true
      && prReviewSurfacePagination.review_threads_complete === true
      && prReviewSurfacePagination.thread_comments_complete === true
    if (prReviewSurfacePagination.complete !== recomputedComplete) {
      errors.push({
        path: 'pr_review_surface.pagination.complete',
        code: 'chatgpt_retro_context_live_assertion.pagination_complete_recompute_mismatch',
        message: `pr_review_surface.pagination.complete is ${JSON.stringify(prReviewSurfacePagination.complete)} but recomputing from the four component completeness fields yields ${JSON.stringify(recomputedComplete)}`,
      })
    }
  }

  return {
    ok: errors.length === 0,
    errors,
  }
}

// Timeout classification must be based on Node's own ETIMEDOUT error code,
// never on the raw signal name. `child_process.spawnSync({ timeout })` sets
// BOTH `result.error.code === 'ETIMEDOUT'` AND `result.signal === killSignal`
// (SIGTERM by default) when the bounded timeout actually fires -- so the
// `spawnResult.error` branch below already reliably classifies real
// timeouts. Treating every SIGTERM/SIGKILL exit as a timeout (regardless of
// `error.code`) would misclassify an externally-sent SIGTERM/SIGKILL (e.g. a
// CI job cancellation, `kill -9`, an OOM killer) as a bounded-timeout
// exceeded, which is a different failure mode.
export function classifySpawnFailure(spawnResult) {
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
  // `detached: true` puts the resolver child in its own process group so a
  // bounded-timeout kill can be extended to any grandchild it spawns (the
  // resolver itself runs a synchronous `gh api` subprocess) by signalling
  // the negative pid (the whole process group) below, instead of relying on
  // Linux to automatically reap orphaned grandchildren.
  const spawnResult = spawnSync(process.execPath, args, {
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: timeoutMs,
    killSignal: 'SIGTERM',
    maxBuffer: DEFAULT_MAX_BUFFER,
    detached: true,
  })
  if (spawnResult.error?.code === 'ETIMEDOUT' && typeof spawnResult.pid === 'number') {
    // Best-effort defensive cleanup: spawnSync's own timeout-kill only
    // targets the immediate child pid. If the resolver was blocked inside
    // its own synchronous `gh` call when the timeout fired, that grandchild
    // process can be left running as an orphan; killing the process group
    // reaps it too.
    try {
      process.kill(-spawnResult.pid, 'SIGKILL')
    } catch {
      // ESRCH (already exited) / EPERM -- nothing more we can safely do.
    }
  }
  return spawnResult
}

const REPO_ROOT = resolvePath(SCRIPT_DIR, '..')

function isFortyHexCommit(value) {
  return typeof value === 'string' && /^[0-9a-f]{40}$/u.test(value)
}

function runGit(repoRoot, args) {
  return spawnSync('git', ['-C', repoRoot, ...args], { encoding: 'utf-8' })
}

/**
 * Determines whether a `resolver_commit` is trustworthy enough for an
 * otherwise-passing live assertion to be marked `live_evidence_eligible`.
 * A bare `git rev-parse HEAD` run against `process.cwd()` can report an
 * unrelated commit if the CLI happens to be invoked with a different
 * repository as cwd, and does not detect an uncommitted working tree.
 * This instead:
 * - always resolves against the resolver script's own directory (`git -C`),
 *   never the caller's `process.cwd()`
 * - requires a full 40-hex commit id (`rev-parse --verify HEAD^{commit}`)
 * - requires that directory's git toplevel to equal `repoRoot` exactly
 *   (guards against `repoRoot` being a non-root subdirectory nested inside
 *   an unrelated outer repository)
 * - requires a clean working tree and index (`git diff --quiet` /
 *   `git diff --cached --quiet`)
 */
export function evaluateLiveEvidenceProvenance({ repoRoot }) {
  const commitResult = runGit(repoRoot, ['rev-parse', '--verify', 'HEAD^{commit}'])
  if (commitResult.status !== 0 || typeof commitResult.stdout !== 'string') {
    return { commit: null, eligible: false }
  }
  const commit = commitResult.stdout.trim()
  if (!isFortyHexCommit(commit)) {
    return { commit: null, eligible: false }
  }

  const toplevelResult = runGit(repoRoot, ['rev-parse', '--show-toplevel'])
  if (toplevelResult.status !== 0 || typeof toplevelResult.stdout !== 'string') {
    return { commit, eligible: false }
  }
  if (resolvePath(toplevelResult.stdout.trim()) !== resolvePath(repoRoot)) {
    return { commit, eligible: false }
  }

  const workingTreeClean = runGit(repoRoot, ['diff', '--quiet']).status === 0
  const indexClean = runGit(repoRoot, ['diff', '--cached', '--quiet']).status === 0
  if (!workingTreeClean || !indexClean) {
    return { commit, eligible: false }
  }

  return { commit, eligible: true }
}

function computeLiveEvidenceProvenance() {
  return evaluateLiveEvidenceProvenance({ repoRoot: REPO_ROOT })
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
  const provenance = includeLiveMetadata ? computeLiveEvidenceProvenance() : null
  const output = {
    schema: SCHEMA,
    assertion_status: assertionStatus,
    execution_profile: executionProfile,
    live_evidence_eligible: assertionStatus === 'pass' && executionProfile === 'live' && provenance?.eligible === true,
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
    output.resolver_commit = provenance.commit
    output.command_args_digest = computeCommandArgsDigest(expected)
  }
  return output
}

function exitWith(output, exitCode) {
  // `process.exit()` can truncate stdout before it finishes flushing,
  // especially when stdout is a pipe (exactly how a test harness / CI job
  // captures this CLI's output) rather than a TTY. Setting
  // `process.exitCode` and letting the event loop drain naturally instead
  // guarantees the full JSON payload is flushed before the process exits.
  process.stdout.write(`${JSON.stringify(output)}\n`)
  process.exitCode = exitCode
}

/**
 * Pure(ish) evaluation of an already-completed resolver `spawnSync` result:
 * classifies spawn failure, parses stdout, and runs the domain assertion.
 * Exported (dependency-injectable input) so subprocess failure modes
 * (ENOBUFS/E2BIG, spawn ENOENT, external signal termination, ordinary
 * resolver non-zero exit, multiple/extra-text stdout) can be tested
 * deterministically against synthetic `spawnResult` objects without
 * actually spawning an OS subprocess.
 */
export function evaluateResolverSpawnResult(spawnResult, { expected, executionProfile }) {
  const spawnFailure = classifySpawnFailure(spawnResult)
  if (spawnFailure) {
    return {
      output: buildOutput({
        assertionStatus: 'error',
        executionProfile,
        expected,
        errorCode: `chatgpt_retro_context_live_assertion.${spawnFailure.code}`,
        errorMessage: spawnFailure.message,
        includeLiveMetadata: true,
      }),
      exitCode: 2,
    }
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
    return {
      output: buildOutput({
        assertionStatus: 'error',
        executionProfile,
        expected,
        errorCode,
        errorMessage,
        includeLiveMetadata: true,
      }),
      exitCode: 2,
    }
  }

  if (parsedStdout === null) {
    return {
      output: buildOutput({
        assertionStatus: 'error',
        executionProfile,
        expected,
        errorCode: 'chatgpt_retro_context_live_assertion.invalid_json_output',
        errorMessage: 'resolver subprocess did not emit a single parsable JSON object on stdout',
        includeLiveMetadata: true,
      }),
      exitCode: 2,
    }
  }

  const assertion = assertChatgptRetroContextLiveResult(parsedStdout, expected)
  return {
    output: buildOutput({
      assertionStatus: assertion.ok ? 'pass' : 'fail',
      executionProfile,
      expected,
      resolveResult: parsedStdout,
      domainErrors: assertion.errors,
      includeLiveMetadata: true,
    }),
    exitCode: assertion.ok ? 0 : 1,
  }
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
  const { output, exitCode } = evaluateResolverSpawnResult(spawnResult, { expected, executionProfile })
  return exitWith(output, exitCode)
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  runCli().catch((error) => {
    const isCliError = error instanceof CliError
    process.stdout.write(`${JSON.stringify({
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
    })}\n`)
    process.exitCode = 2
  })
}
