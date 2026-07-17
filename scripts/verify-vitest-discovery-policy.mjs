#!/usr/bin/env node
/**
 * Runtime sentinel probe for the Vitest test-discovery exclusion policy.
 *
 * Places a unique sentinel test file into both `.claude/tmp/` and `tmp/`
 * (the two repo-approved temporary workspace classes), then verifies:
 *
 *   (a) Without the canonical `--exclude` flags, `vitest list` DOES discover
 *       both sentinel files (proves the sentinel is a real discoverable test).
 *   (b) With the canonical `--exclude` flags (parsed directly from the
 *       `package.json` "test" script, the single source of truth), `vitest
 *       list` does NOT discover either sentinel file.
 *   (c) With the sentinel files still present, `pnpm test` (the real,
 *       unmodified package script) still succeeds.
 *   (d) The sentinel files and any directories created for them are removed
 *       whether the probe passes or fails.
 *
 * Exit codes:
 *   0  - all checks passed.
 *   1  - one or more checks failed.
 *   3  - environment blocked (node_modules / vitest binary unavailable).
 *
 * A JSON artifact summarizing the run is written to
 * `artifacts/verify-vitest-discovery-policy.json` (relative to CWD) for
 * evidence purposes. SKIP is not a valid outcome for this probe: the Issue's
 * `Runtime Verification Applicability.skip_conditions` explicitly requires
 * environment-unavailable cases to be treated as `environment blocked`
 * (exit 3), never as a silent pass.
 */

import { mkdirSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(__dirname, '..')

const VITEST_BIN = path.join(repoRoot, 'node_modules', '.bin', 'vitest')
const PACKAGE_JSON_PATH = path.join(repoRoot, 'package.json')
const ARTIFACT_DIR = path.join(repoRoot, 'artifacts')
const ARTIFACT_PATH = path.join(ARTIFACT_DIR, 'verify-vitest-discovery-policy.json')

const CLAUDE_TMP_DIR = path.join(repoRoot, '.claude', 'tmp')
const TMP_DIR = path.join(repoRoot, 'tmp')

function log(message) {
  process.stdout.write(`${message}\n`)
}

function fail(message) {
  process.stderr.write(`${message}\n`)
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

function runVitestList(sentinelName, excludes) {
  const args = ['list', sentinelName]
  for (const glob of excludes) {
    args.push('--exclude', glob)
  }
  return spawnSync(VITEST_BIN, args, {
    cwd: repoRoot,
    encoding: 'utf-8',
    timeout: 120_000,
  })
}

function runPnpmTest() {
  return spawnSync('pnpm', ['test'], {
    cwd: repoRoot,
    encoding: 'utf-8',
    timeout: 600_000,
  })
}

function ensureEnvironment() {
  if (!existsSync(VITEST_BIN)) {
    fail(
      `environment blocked: ${VITEST_BIN} not found. Run "pnpm install" in the repository before executing this probe.`,
    )
    process.exit(3)
  }
}

function main() {
  ensureEnvironment()

  const sentinelName = `zz-discovery-probe-sentinel-${Date.now()}-${process.pid}`
  const sentinelFileName = `${sentinelName}.test.ts`
  const sentinelBody = [
    "import { it, expect } from 'vitest'",
    '',
    `it('${sentinelName} must never be collected from repo-approved temporary workspaces', () => {`,
    '  expect(true).toBe(true)',
    '})',
    '',
  ].join('\n')

  const claudeTmpSentinel = path.join(CLAUDE_TMP_DIR, sentinelFileName)
  const tmpSentinel = path.join(TMP_DIR, sentinelFileName)

  const createdDirs = []
  const artifact = {
    schema: 'verify-vitest-discovery-policy/v1',
    sentinel: sentinelName,
    canonical_excludes: null,
    checks: {},
    cleanup: { removed_files: [], removed_dirs: [] },
    status: 'unknown',
  }

  let exitCode = 0

  try {
    if (!existsSync(CLAUDE_TMP_DIR)) createdDirs.push(CLAUDE_TMP_DIR)
    if (!existsSync(TMP_DIR)) createdDirs.push(TMP_DIR)
    mkdirSync(CLAUDE_TMP_DIR, { recursive: true })
    mkdirSync(TMP_DIR, { recursive: true })
    writeFileSync(claudeTmpSentinel, sentinelBody, 'utf-8')
    writeFileSync(tmpSentinel, sentinelBody, 'utf-8')

    const canonicalExcludes = readCanonicalExcludes()
    artifact.canonical_excludes = canonicalExcludes
    log(`canonical excludes parsed from package.json: ${JSON.stringify(canonicalExcludes)}`)

    // (a) Without the canonical excludes, discovery must find both sentinels.
    const withoutExclude = runVitestList(sentinelName, [])
    const withoutExcludeStdout = withoutExclude.stdout || ''
    const foundClaudeTmpWithoutExclude = withoutExcludeStdout.includes(sentinelFileName) &&
      withoutExcludeStdout.includes('.claude/tmp')
    const foundTmpWithoutExclude = withoutExcludeStdout.includes(sentinelFileName) &&
      withoutExcludeStdout.split('\n').some((line) => line.startsWith(`tmp/${sentinelFileName}`))
    artifact.checks.a_discovered_without_exclude = {
      exit_code: withoutExclude.status,
      stdout: withoutExcludeStdout,
      stderr: withoutExclude.stderr || '',
      claude_tmp_found: foundClaudeTmpWithoutExclude,
      tmp_found: foundTmpWithoutExclude,
    }
    if (!foundClaudeTmpWithoutExclude || !foundTmpWithoutExclude) {
      exitCode = 1
      fail(
        `FAIL (a): sentinel not discovered without excludes (claude_tmp=${foundClaudeTmpWithoutExclude}, tmp=${foundTmpWithoutExclude})`,
      )
    } else {
      log('PASS (a): sentinel discovered in both .claude/tmp/ and tmp/ without excludes')
    }

    // (b) With the canonical excludes, discovery must find neither sentinel.
    const withExclude = runVitestList(sentinelName, canonicalExcludes)
    const withExcludeStdout = withExclude.stdout || ''
    const claudeTmpExcluded = !withExcludeStdout.includes(`.claude/tmp/${sentinelFileName}`)
    const tmpExcluded = !withExcludeStdout.split('\n').some((line) => line.startsWith(`tmp/${sentinelFileName}`))
    artifact.checks.b_excluded_with_canonical = {
      exit_code: withExclude.status,
      stdout: withExcludeStdout,
      stderr: withExclude.stderr || '',
      claude_tmp_excluded: claudeTmpExcluded,
      tmp_excluded: tmpExcluded,
    }
    if (!claudeTmpExcluded || !tmpExcluded) {
      exitCode = 1
      fail(
        `FAIL (b): sentinel still discovered with canonical excludes (claude_tmp_excluded=${claudeTmpExcluded}, tmp_excluded=${tmpExcluded})`,
      )
    } else {
      log('PASS (b): sentinel excluded from discovery in both .claude/tmp/ and tmp/ with canonical excludes')
    }

    // (c) With the sentinel present, the real "pnpm test" script must still succeed.
    const testRun = runPnpmTest()
    const testPassed = testRun.status === 0
    artifact.checks.c_pnpm_test_with_sentinel_present = {
      exit_code: testRun.status,
      stdout_tail: (testRun.stdout || '').slice(-4000),
      stderr_tail: (testRun.stderr || '').slice(-4000),
      passed: testPassed,
    }
    if (!testPassed) {
      exitCode = 1
      fail(`FAIL (c): "pnpm test" did not succeed with sentinel files present (exit=${testRun.status})`)
    } else {
      log('PASS (c): "pnpm test" succeeded with sentinel files present')
    }
  } catch (error) {
    exitCode = 1
    fail(`FAIL: probe raised an exception: ${error && error.stack ? error.stack : String(error)}`)
    artifact.error = String(error)
  } finally {
    // (d) Cleanup must always run, regardless of pass/fail.
    for (const filePath of [claudeTmpSentinel, tmpSentinel]) {
      if (existsSync(filePath)) {
        rmSync(filePath, { force: true })
        artifact.cleanup.removed_files.push(filePath)
      }
    }
    for (const dirPath of createdDirs) {
      if (existsSync(dirPath)) {
        try {
          rmSync(dirPath, { recursive: false })
          artifact.cleanup.removed_dirs.push(dirPath)
        } catch {
          // Directory not empty (pre-existing sibling content) - leave it.
        }
      }
    }
    log(`cleanup: removed_files=${JSON.stringify(artifact.cleanup.removed_files)} removed_dirs=${JSON.stringify(artifact.cleanup.removed_dirs)}`)
  }

  artifact.status = exitCode === 0 ? 'pass' : 'fail'

  try {
    mkdirSync(ARTIFACT_DIR, { recursive: true })
    writeFileSync(ARTIFACT_PATH, JSON.stringify(artifact, null, 2), 'utf-8')
    log(`artifact written to ${ARTIFACT_PATH}`)
  } catch (error) {
    fail(`WARNING: failed to write artifact: ${error}`)
  }

  process.exit(exitCode)
}

main()
