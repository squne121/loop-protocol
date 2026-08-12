#!/usr/bin/env node
/**
 * verify-e2e-lane-partition.mjs
 *
 * Issue #2119: machine verification that splitting the standard E2E suite
 * into `e2e-core` (LOOP_E2E_LANE=core) and `e2e-responsive-matrix`
 * (LOOP_E2E_LANE=responsive) is a lossless, disjoint partition:
 *
 *   AC1: union(core, responsive) canonical logical IDs == the frozen
 *        baseline captured at split time (tests/ci/fixtures/e2e_lane_partition_baseline_v1.json).
 *   AC2: intersection(core, responsive) canonical logical IDs == empty.
 *
 * "Canonical logical ID" (Issue #2119 AC1 wording): a unique identifier
 * machine-derived from the file path + describe/test title hierarchy that
 * Playwright's own JSON reporter emits (`playwright test --list
 * --reporter=json`). This script deliberately uses the TITLE HIERARCHY ONLY
 * (excluding the leaf spec file name) as the equality key: the entire point
 * of this Issue is to physically relocate the responsive matrix test into
 * its own spec file, which necessarily changes the file-path component of
 * any file-qualified id — matching on title hierarchy alone is what
 * actually proves "no test silently dropped, duplicated, or renamed by the
 * split", which is the real intent behind AC1/AC2.
 *
 * Exit code: 0 = pass, 1 = contract violation, 2 = usage/collection error.
 */
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '..', '..')
const PLAYWRIGHT_BIN = path.join(REPO_ROOT, 'node_modules', '.bin', 'playwright')
const CONFIG = path.join(REPO_ROOT, 'playwright.config.ts')
const BASELINE_PATH = path.join(
  REPO_ROOT,
  'tests',
  'ci',
  'fixtures',
  'e2e_lane_partition_baseline_v1.json',
)

function listLane(lane) {
  if (!existsSync(PLAYWRIGHT_BIN)) {
    const err = new Error(
      `playwright binary not found at ${PLAYWRIGHT_BIN} — run pnpm install first (this is a local/CI environment preflight issue, not a partition contract violation)`,
    )
    err.code = 'PLAYWRIGHT_NOT_INSTALLED'
    throw err
  }
  const stdout = execFileSync(
    PLAYWRIGHT_BIN,
    ['test', '--list', '--reporter=json', `--config=${CONFIG}`],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, LOOP_E2E_LANE: lane, CI: 'true' },
      encoding: 'utf8',
      maxBuffer: 64 * 1024 * 1024,
    },
  )
  return JSON.parse(stdout)
}

function canonicalTitleIds(doc) {
  const ids = []
  function walk(suite, prefix) {
    for (const s of suite.suites ?? []) walk(s, [...prefix, s.title])
    for (const spec of suite.specs ?? []) ids.push([...prefix, spec.title].join(' > '))
  }
  for (const top of doc.suites ?? []) walk(top, [])
  return ids
}

export function computePartitionResult() {
  const coreDoc = listLane('core')
  const responsiveDoc = listLane('responsive')
  const coreIds = canonicalTitleIds(coreDoc)
  const responsiveIds = canonicalTitleIds(responsiveDoc)
  const coreSet = new Set(coreIds)
  const responsiveSet = new Set(responsiveIds)

  const failures = []

  if (coreIds.length !== coreSet.size) {
    failures.push('e2e-core inventory has duplicate canonical ids')
  }
  if (responsiveIds.length !== responsiveSet.size) {
    failures.push('e2e-responsive-matrix inventory has duplicate canonical ids')
  }

  // AC2: intersection must be empty.
  const intersection = [...coreSet].filter((id) => responsiveSet.has(id))
  if (intersection.length > 0) {
    failures.push(`provider inventory intersection non-empty: ${JSON.stringify(intersection)}`)
  }

  const unionSet = new Set([...coreSet, ...responsiveSet])

  // AC1: union must equal the frozen baseline.
  let baselineIds = []
  if (existsSync(BASELINE_PATH)) {
    const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
    baselineIds = baseline.canonical_ids ?? []
  } else {
    failures.push(`missing baseline fixture: ${BASELINE_PATH}`)
  }
  const baselineSet = new Set(baselineIds)
  const missingFromUnion = [...baselineSet].filter((id) => !unionSet.has(id))
  const extraInUnion = [...unionSet].filter((id) => !baselineSet.has(id))
  if (missingFromUnion.length > 0) {
    failures.push(`missing from e2e-core + e2e-responsive-matrix union: ${JSON.stringify(missingFromUnion)}`)
  }
  if (extraInUnion.length > 0) {
    failures.push(`unexpected extra ids in union (not in frozen baseline): ${JSON.stringify(extraInUnion)}`)
  }

  return {
    schema: 'E2E_LANE_PARTITION_CHECK_V1',
    status: failures.length === 0 ? 'pass' : 'fail',
    core_count: coreSet.size,
    responsive_count: responsiveSet.size,
    union_count: unionSet.size,
    baseline_count: baselineSet.size,
    intersection,
    missing_from_union: missingFromUnion,
    extra_in_union: extraInUnion,
    failures,
  }
}

function main() {
  let result
  try {
    result = computePartitionResult()
  } catch (err) {
    console.log(JSON.stringify({
      schema: 'E2E_LANE_PARTITION_CHECK_V1',
      status: 'error',
      error: String(err && err.message ? err.message : err),
      error_code: err && err.code ? err.code : null,
    }, null, 2))
    process.exit(2)
  }
  console.log(JSON.stringify(result, null, 2))
  process.exit(result.status === 'pass' ? 0 : 1)
}

// Only run main() when invoked directly (not when imported for tests).
if (import.meta.url === `file://${process.argv[1]}`) {
  main()
}
