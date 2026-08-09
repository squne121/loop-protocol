import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

/**
 * Issue #2019 AC7: constructs the resolver cannot fully solve
 * (import.meta.glob incl. negative globs, variable/unbounded dynamic
 * import, virtual/generated module, dynamic new URL()) must be reported as
 * unknown_impact -- never silently "no impact".
 */

const REPO_ROOT = resolve(__dirname, '..', '..')
const MJS_PATH = resolve(REPO_ROOT, 'scripts', 'agent-ops', 'resolve_visual_impact.mjs')
const FIXTURE_ENTRY = 'scripts/agent-ops/tests/fixtures/visual_impact/unknown_impact/entry.ts'

interface UnknownImpactEntry {
  file: string
  kind: string
  detail: string
}

interface MjsResult {
  schema: string
  surfaces: Record<string, { reachable_files: string[]; unknown_impact: UnknownImpactEntry[] }>
  errors: string[]
}

function runMjs(entries: string[]): MjsResult {
  const request = JSON.stringify({ repo_root: REPO_ROOT, surfaces: { fixture: { modules: entries } } })
  const stdout = execFileSync('node', [MJS_PATH], { input: request, encoding: 'utf8' })
  return JSON.parse(stdout) as MjsResult
}

describe('resolve_visual_impact.mjs unknown_impact fail-closed constructs', () => {
  it('GIVEN import.meta.glob, variable dynamic import, virtual module, and dynamic new URL() WHEN resolved THEN each is reported as unknown_impact, never dropped', () => {
    const result = runMjs([FIXTURE_ENTRY])
    const kinds = result.surfaces.fixture.unknown_impact.map((entry) => entry.kind).sort()
    expect(kinds).toEqual(
      ['dynamic_new_url', 'dynamic_variable_import', 'import_meta_glob', 'virtual_module'].sort(),
    )
  })

  it('GIVEN unknown_impact entries WHEN inspected THEN each carries the referencing file (never a bogus/empty attribution)', () => {
    const result = runMjs([FIXTURE_ENTRY])
    for (const entry of result.surfaces.fixture.unknown_impact) {
      expect(entry.file).toBe(FIXTURE_ENTRY)
      expect(entry.detail.length).toBeGreaterThan(0)
    }
  })
})
