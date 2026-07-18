#!/usr/bin/env node
/**
 * Runtime sentinel probe for the Vitest test-discovery exclusion policy.
 *
 * Places a unique, self-owned sentinel probe directory (never the
 * `.claude/tmp/` / `tmp/` root itself, which is report-only under folder
 * policy) into both repo-approved temporary workspace roots, then verifies:
 *
 *   (a) Without the canonical `--exclude` flags, `vitest list --filesOnly
 *       --json` DOES discover exactly the two sentinel files (structural set
 *       equality, not substring matching).
 *   (b) With the canonical `--exclude` flags (parsed directly from the
 *       `package.json` "test" script, the single source of truth), `vitest
 *       list --filesOnly --json` discovers NEITHER sentinel file.
 *   (c) With the sentinel files still present, `pnpm test` (the real,
 *       unmodified package script) still succeeds. The sentinel test body
 *       always throws when actually executed, so if exclusion is broken and
 *       `pnpm test` collects it, this check fails for real (not a
 *       false-pass).
 *   (d) The sentinel probe directories (never the shared root) are removed
 *       whether the probe passes or fails. Each cleanup is attempted
 *       independently; failures are collected, not silently ignored.
 *
 * Every `vitest list` / `pnpm test` spawn's `status` / `signal` / `error` is
 * checked explicitly (assertSpawnSucceeded) — a non-zero exit, signal
 * termination, or spawn error always fails the probe instead of being
 * silently interpreted as "no sentinel found".
 *
 * Exit codes:
 *   0  - all checks passed.
 *   1  - one or more checks (including cleanup / artifact write) failed.
 *   3  - environment blocked (node_modules / vitest binary unavailable).
 *
 * A JSON artifact summarizing the run (including head_sha, tool versions,
 * full argv per spawn, per-spawn status/signal/error, resolved repo-relative
 * file paths, pre-existing root state, created probe directories, and
 * cleanup results/errors) is written to
 * `artifacts/verify-vitest-discovery-policy.json` (relative to CWD). Writing
 * this artifact is itself a required check: if it cannot be written, the
 * probe fails (exit 1), not a silent warning. SKIP is not a valid outcome
 * for this probe: the Issue's `Runtime Verification
 * Applicability.skip_conditions` explicitly requires environment-unavailable
 * cases to be treated as `environment blocked` (exit 3), never as a silent
 * pass.
 */

import { mkdirSync, readFileSync, rmSync, writeFileSync, existsSync, unlinkSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(__dirname, '..')

const VITEST_BIN = path.join(repoRoot, 'node_modules', '.bin', 'vitest')
const PACKAGE_JSON_PATH = path.join(repoRoot, 'package.json')
const ARTIFACT_DIR = path.join(repoRoot, 'artifacts')
const ARTIFACT_PATH = path.join(ARTIFACT_DIR, 'verify-vitest-discovery-policy.json')

const CLAUDE_TMP_ROOT = path.join(repoRoot, '.claude', 'tmp')
const TMP_ROOT = path.join(repoRoot, 'tmp')

function log(message) {
  process.stdout.write(`${message}\n`)
}

function fail(message) {
  process.stderr.write(`${message}\n`)
}

function toRepoRelativePosix(absolutePath) {
  return path.relative(repoRoot, absolutePath).split(path.sep).join('/')
}

function readCanonicalExcludes() {
  const raw = readFileSync(PACKAGE_JSON_PATH, 'utf-8')
  const manifest = JSON.parse(raw)
  const testScript = manifest.scripts && manifest.scripts.test
  if (typeof testScript !== 'string') {
    throw new Error('package.json scripts.test is missing or not a string')
  }
  const excludes = []
  const pattern = /--exclude '([^']+)'/g
  let match
  while ((match = pattern.exec(testScript)) !== null) {
    excludes.push(match[1])
  }
  if (excludes.length === 0) {
    throw new Error('no --exclude globs parsed from package.json scripts.test')
  }
  return excludes
}

/**
 * Blocker 3: verify a spawnSync result actually succeeded before trusting
 * its stdout. A non-zero status, non-null signal, or spawn error must always
 * be treated as a hard failure, never as "sentinel not found".
 */
function assertSpawnSucceeded(result, label) {
  if (result.error || result.signal !== null || result.status !== 0) {
    throw new Error(
      `${label} failed: status=${result.status} signal=${result.signal} ` +
        `error=${result.error?.message ?? 'none'} stderr=${result.stderr ?? ''}`,
    )
  }
}

let jsonOutputCounter = 0

/**
 * Run `vitest list --filesOnly --json <tmpOutputFile> <filter> [--exclude ...]`
 * and return the parsed list of repo-relative file paths.
 *
 * `--json` writes to a file (not stdout) when given a truthy value; we give
 * it a unique output file path under `artifacts/` and parse+delete it
 * afterward. The `filter` positional argument scopes discovery to the probe
 * run (via the shared runId substring in both probe directory names),
 * avoiding an unfiltered full-repo scan (which can hit unrelated Playwright
 * config collection errors in `tests/e2e/**`).
 */
function runVitestListJson(label, filter, excludes, spawnLog) {
  jsonOutputCounter += 1
  const outputFile = path.join(ARTIFACT_DIR, `.probe-list-${label}-${jsonOutputCounter}.json`)
  const argv = [VITEST_BIN, 'list', '--filesOnly', '--json', outputFile, filter]
  for (const glob of excludes) {
    argv.push('--exclude', glob)
  }
  const result = spawnSync(argv[0], argv.slice(1), {
    cwd: repoRoot,
    encoding: 'utf-8',
    timeout: 120_000,
  })
  spawnLog.push({
    label,
    argv,
    status: result.status,
    signal: result.signal,
    error: result.error ? result.error.message : null,
    stderr_tail: (result.stderr || '').slice(-2000),
  })
  assertSpawnSucceeded(result, label)

  let parsed = []
  let rawOutput = null
  try {
    if (existsSync(outputFile)) {
      rawOutput = readFileSync(outputFile, 'utf-8')
      parsed = JSON.parse(rawOutput)
    }
  } finally {
    if (existsSync(outputFile)) {
      unlinkSync(outputFile)
    }
  }
  if (!Array.isArray(parsed)) {
    throw new Error(`${label}: unexpected vitest --json output shape (not an array): ${rawOutput}`)
  }
  const relativePaths = parsed.map((entry) => {
    if (!entry || typeof entry.file !== 'string') {
      throw new Error(`${label}: unexpected vitest --json entry shape: ${JSON.stringify(entry)}`)
    }
    return toRepoRelativePosix(entry.file)
  })
  const uniquePaths = new Set(relativePaths)
  if (uniquePaths.size !== relativePaths.length) {
    throw new Error(`${label}: duplicate file paths in vitest --json output: ${relativePaths.join(', ')}`)
  }
  return relativePaths
}

function runPnpmTest(spawnLog) {
  const argv = ['pnpm', 'test']
  const result = spawnSync(argv[0], argv.slice(1), {
    cwd: repoRoot,
    encoding: 'utf-8',
    timeout: 600_000,
  })
  spawnLog.push({
    label: 'pnpm_test',
    argv,
    status: result.status,
    signal: result.signal,
    error: result.error ? result.error.message : null,
    stderr_tail: (result.stderr || '').slice(-2000),
  })
  return result
}

function ensureEnvironment() {
  if (!existsSync(VITEST_BIN)) {
    fail(
      `environment blocked: ${VITEST_BIN} not found. Run "pnpm install" in the repository before executing this probe.`,
    )
    process.exit(3)
  }
}

function getToolVersions() {
  const node = process.version
  let pnpm = null
  let vitest = null
  const pnpmResult = spawnSync('pnpm', ['--version'], { encoding: 'utf-8', timeout: 30_000 })
  if (pnpmResult.status === 0) {
    pnpm = (pnpmResult.stdout || '').trim()
  }
  const vitestResult = spawnSync(VITEST_BIN, ['--version'], { encoding: 'utf-8', timeout: 30_000 })
  if (vitestResult.status === 0) {
    vitest = (vitestResult.stdout || '').trim()
  }
  return { node, pnpm, vitest }
}

function getHeadSha() {
  const result = spawnSync('git', ['rev-parse', 'HEAD'], {
    cwd: repoRoot,
    encoding: 'utf-8',
    timeout: 30_000,
  })
  if (result.status === 0) {
    return (result.stdout || '').trim()
  }
  return null
}

function main() {
  ensureEnvironment()

  const runId = `${Date.now()}-${process.pid}`
  const filterToken = `vitest-discovery-probe-${runId}`
  const claudeTmpProbeDir = path.join(CLAUDE_TMP_ROOT, `${filterToken}-claude-tmp`)
  const tmpProbeDir = path.join(TMP_ROOT, `${filterToken}-tmp`)
  const sentinelFileName = 'sentinel.test.ts'
  const claudeTmpSentinel = path.join(claudeTmpProbeDir, sentinelFileName)
  const tmpSentinel = path.join(tmpProbeDir, sentinelFileName)
  const expectedRelativePaths = [
    toRepoRelativePosix(claudeTmpSentinel),
    toRepoRelativePosix(tmpSentinel),
  ].sort()

  // Blocker 2: the sentinel must fail hard if it is ever actually executed
  // (only `vitest list` discovery is expected to touch it; `pnpm test`
  // must never collect it while canonical excludes are intact).
  const sentinelBody = [
    "import { it } from 'vitest'",
    '',
    "it('temporary-workspace sentinel must be excluded', () => {",
    "  throw new Error('temporary-workspace discovery sentinel was executed')",
    '})',
    '',
  ].join('\n')

  const spawnLog = []
  const preExisting = {
    claude_tmp_root: existsSync(CLAUDE_TMP_ROOT),
    tmp_root: existsSync(TMP_ROOT),
  }
  const createdProbeDirs = []
  const cleanup = { attempted: [], removed: [], errors: [] }
  const artifact = {
    schema: 'verify-vitest-discovery-policy/v2',
    run_id: runId,
    head_sha: getHeadSha(),
    tool_versions: getToolVersions(),
    pre_existing_roots: preExisting,
    expected_relative_paths: expectedRelativePaths,
    canonical_excludes: null,
    spawns: spawnLog,
    checks: {},
    created_probe_dirs: createdProbeDirs,
    cleanup,
    artifact_write: { attempted: true, succeeded: null },
    status: 'unknown',
  }

  let exitCode = 0

  try {
    mkdirSync(claudeTmpProbeDir, { recursive: true })
    createdProbeDirs.push(claudeTmpProbeDir)
    mkdirSync(tmpProbeDir, { recursive: true })
    createdProbeDirs.push(tmpProbeDir)
    writeFileSync(claudeTmpSentinel, sentinelBody, 'utf-8')
    writeFileSync(tmpSentinel, sentinelBody, 'utf-8')

    const canonicalExcludes = readCanonicalExcludes()
    artifact.canonical_excludes = canonicalExcludes
    log(`canonical excludes parsed from package.json: ${JSON.stringify(canonicalExcludes)}`)

    // (a) Without the canonical excludes, discovery must find exactly the
    // two sentinel files (structural set equality).
    let withoutExcludePaths
    let withoutExcludeError = null
    try {
      withoutExcludePaths = runVitestListJson('without_exclude', filterToken, [], spawnLog)
    } catch (error) {
      withoutExcludeError = String(error)
      withoutExcludePaths = null
    }
    const withoutExcludeSorted = withoutExcludePaths ? [...withoutExcludePaths].sort() : null
    const foundExactly = withoutExcludeError === null &&
      JSON.stringify(withoutExcludeSorted) === JSON.stringify(expectedRelativePaths)
    artifact.checks.a_discovered_without_exclude = {
      error: withoutExcludeError,
      discovered_paths: withoutExcludeSorted,
      expected_paths: expectedRelativePaths,
      matches_exactly: foundExactly,
    }
    if (!foundExactly) {
      exitCode = 1
      fail(
        `FAIL (a): discovery without excludes did not find exactly the expected sentinel set. ` +
          `error=${withoutExcludeError} discovered=${JSON.stringify(withoutExcludeSorted)}`,
      )
    } else {
      log('PASS (a): sentinel discovered in both .claude/tmp/ and tmp/ without excludes (exact set match)')
    }

    // (b) With the canonical excludes, discovery must find neither sentinel.
    let withExcludePaths
    let withExcludeError = null
    try {
      withExcludePaths = runVitestListJson('with_exclude', filterToken, canonicalExcludes, spawnLog)
    } catch (error) {
      withExcludeError = String(error)
      withExcludePaths = null
    }
    const excludedCompletely = withExcludeError === null &&
      Array.isArray(withExcludePaths) && withExcludePaths.length === 0
    artifact.checks.b_excluded_with_canonical = {
      error: withExcludeError,
      discovered_paths: withExcludePaths,
      matches_exactly: excludedCompletely,
    }
    if (!excludedCompletely) {
      exitCode = 1
      fail(
        `FAIL (b): sentinel still discovered (or vitest failed) with canonical excludes. ` +
          `error=${withExcludeError} discovered=${JSON.stringify(withExcludePaths)}`,
      )
    } else {
      log('PASS (b): sentinel excluded from discovery in both .claude/tmp/ and tmp/ with canonical excludes')
    }

    // (c) With the sentinel present, the real "pnpm test" script must still
    // succeed. Because the sentinel body always throws when executed, this
    // is a real end-to-end guard against exclusion regressions, not a
    // false-pass sentinel.
    const testRun = runPnpmTest(spawnLog)
    const testPassed = testRun.error === undefined && testRun.signal === null && testRun.status === 0
    artifact.checks.c_pnpm_test_with_sentinel_present = {
      status: testRun.status,
      signal: testRun.signal,
      error: testRun.error ? testRun.error.message : null,
      stdout_tail: (testRun.stdout || '').slice(-4000),
      stderr_tail: (testRun.stderr || '').slice(-4000),
      passed: testPassed,
    }
    if (!testPassed) {
      exitCode = 1
      fail(
        `FAIL (c): "pnpm test" did not succeed with sentinel files present ` +
          `(status=${testRun.status} signal=${testRun.signal})`,
      )
    } else {
      log('PASS (c): "pnpm test" succeeded with sentinel files present')
    }
  } catch (error) {
    exitCode = 1
    fail(`FAIL: probe raised an exception: ${error && error.stack ? error.stack : String(error)}`)
    artifact.error = String(error)
  } finally {
    // (d) Cleanup must always run, regardless of pass/fail. Each probe
    // directory is cleaned up independently; one failure must not prevent
    // attempting the other, and any remaining/failed cleanup fails the
    // overall probe. The shared root (`.claude/tmp/` / `tmp/`) itself is
    // NEVER a cleanup target (folder policy: report-only).
    for (const probeDir of createdProbeDirs) {
      cleanup.attempted.push(probeDir)
      try {
        rmSync(probeDir, { recursive: true, force: true })
        if (existsSync(probeDir)) {
          cleanup.errors.push(`cleanup failed: ${probeDir} (still exists after rmSync)`)
        } else {
          cleanup.removed.push(probeDir)
        }
      } catch (error) {
        cleanup.errors.push(`cleanup failed: ${probeDir} (${error && error.message ? error.message : error})`)
      }
    }
    if (cleanup.errors.length > 0) {
      exitCode = 1
      for (const message of cleanup.errors) {
        fail(`FAIL (d): ${message}`)
      }
    } else {
      log(`cleanup: removed=${JSON.stringify(cleanup.removed)}`)
    }
  }

  artifact.status = exitCode === 0 ? 'pass' : 'fail'

  try {
    mkdirSync(ARTIFACT_DIR, { recursive: true })
    artifact.artifact_write.succeeded = true
    writeFileSync(ARTIFACT_PATH, JSON.stringify(artifact, null, 2), 'utf-8')
    log(`artifact written to ${ARTIFACT_PATH}`)
  } catch (error) {
    artifact.artifact_write.succeeded = false
    exitCode = 1
    fail(`FAIL: artifact write failed: ${error}`)
    // Best-effort: still try to persist what we have under a fallback name
    // so the failure itself is diagnosable, but this must not mask the
    // exit-1 failure above.
    try {
      writeFileSync(
        path.join(ARTIFACT_DIR, 'verify-vitest-discovery-policy.write-failed.json'),
        JSON.stringify(artifact, null, 2),
        'utf-8',
      )
    } catch {
      // Nothing further we can do; the exit code already reflects failure.
    }
  }

  process.exit(exitCode)
}

main()
