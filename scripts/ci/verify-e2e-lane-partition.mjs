#!/usr/bin/env node
/**
 * verify-e2e-lane-partition.mjs
 *
 * Issue #2119: machine verification that splitting the standard E2E suite
 * into `e2e-core` (LOOP_E2E_LANE=core) and `e2e-responsive-matrix`
 * (LOOP_E2E_LANE=responsive) is a lossless, disjoint partition:
 *
 *   AC1: union(core, responsive) canonical logical IDs, after applying the
 *        explicit relocation map, == the frozen baseline captured
 *        independently from the pre-split base SHA
 *        (tests/ci/fixtures/e2e_lane_partition_baseline_v1.json).
 *   AC2: intersection(core, responsive) canonical logical IDs == empty.
 *
 * "Canonical logical ID" (Issue #2119 AC1 wording): a unique identifier
 * machine-derived from the file path + describe/test title hierarchy that
 * Playwright's own JSON reporter emits (`playwright test --list
 * --reporter=json`). This script uses the FULL FILE-QUALIFIED id
 * (`<file>::<title hierarchy>`), not title-hierarchy-only (PR #2137 human
 * review issuecomment-5273090534 P1 fix: title-only equality could not
 * distinguish same-named tests living in different spec files). Physically
 * relocating a test intentionally changes its file-qualified id, which is
 * why `tests/ci/fixtures/e2e_lane_partition_relocation_map_v1.json` exists:
 * only the SPECIFIC old-id -> new-id pairs listed there are treated as
 * identity-preserved renames when reconciling against the baseline; every
 * other id must match exactly, so drops/duplicates/same-title-different-
 * file collisions are still detected.
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
const RELOCATION_MAP_PATH = path.join(
  REPO_ROOT,
  'tests',
  'ci',
  'fixtures',
  'e2e_lane_partition_relocation_map_v1.json',
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

function canonicalFileQualifiedIds(doc) {
  const ids = []
  function walk(suite, prefix, file) {
    for (const s of suite.suites ?? []) walk(s, [...prefix, s.title], file)
    for (const spec of suite.specs ?? []) {
      ids.push(`${spec.file ?? file}::` + [...prefix, spec.title].join(' > '))
    }
  }
  for (const top of doc.suites ?? []) walk(top, [], top.file)
  return ids
}

function loadRelocationMap() {
  if (!existsSync(RELOCATION_MAP_PATH)) {
    return []
  }
  const doc = JSON.parse(readFileSync(RELOCATION_MAP_PATH, 'utf8'))
  return doc.relocations ?? []
}

/** Applies the explicit old-id -> new-id relocation map to a set of
 * file-qualified ids: any id present in the set that equals a relocation's
 * `new_id` is rewritten to that relocation's `old_id` before comparison
 * against the pre-split baseline. Unmapped ids are left untouched, so this
 * cannot mask a drop/duplicate/collision for any test not explicitly
 * listed as an intentional relocation. */
function applyRelocationMap(ids, relocations) {
  const newToOld = new Map(relocations.map((r) => [r.new_id, r.old_id]))
  return ids.map((id) => newToOld.get(id) ?? id)
}

export function computePartitionResult() {
  const coreDoc = listLane('core')
  const responsiveDoc = listLane('responsive')
  const coreIds = canonicalFileQualifiedIds(coreDoc)
  const responsiveIds = canonicalFileQualifiedIds(responsiveDoc)
  const coreSet = new Set(coreIds)
  const responsiveSet = new Set(responsiveIds)
  const relocations = loadRelocationMap()

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

  // AC1: union (after applying the explicit relocation map, so
  // intentionally-moved tests are not counted as dropped+added) must equal
  // the frozen pre-split baseline.
  const reconciledUnionIds = applyRelocationMap([...unionSet], relocations)
  const reconciledUnionSet = new Set(reconciledUnionIds)

  let baselineIds = []
  if (existsSync(BASELINE_PATH)) {
    const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
    baselineIds = baseline.canonical_ids ?? []
  } else {
    failures.push(`missing baseline fixture: ${BASELINE_PATH}`)
  }
  const baselineSet = new Set(baselineIds)
  const missingFromUnion = [...baselineSet].filter((id) => !reconciledUnionSet.has(id))
  const extraInUnion = [...reconciledUnionSet].filter((id) => !baselineSet.has(id))
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
