import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

/**
 * Issue #2019 AC6: resolve_visual_impact.mjs (TypeScript compiler API)
 * deterministically resolves CSS @import/url(), static asset import,
 * `?url`/`?raw`, and static `new URL(x, import.meta.url)`.
 *
 * Invokes the real .mjs directly (stdin/stdout contract) against the
 * checked-in fixture at
 * scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/.
 */

const REPO_ROOT = resolve(__dirname, '..', '..')
const MJS_PATH = resolve(REPO_ROOT, 'scripts', 'agent-ops', 'resolve_visual_impact.mjs')
const FIXTURE_ENTRY = 'scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/entry.ts'

interface MjsResult {
  schema: string
  surfaces: Record<string, { reachable_files: string[]; unknown_impact: unknown[] }>
  errors: string[]
}

function runMjs(entries: string[]): MjsResult {
  const request = JSON.stringify({ repo_root: REPO_ROOT, surfaces: { fixture: { modules: entries } } })
  const stdout = execFileSync('node', [MJS_PATH], { input: request, encoding: 'utf8' })
  return JSON.parse(stdout) as MjsResult
}

describe('resolve_visual_impact.mjs deterministic Vite-specific resolution', () => {
  it('GIVEN a fixture entry importing CSS @import/url(), ?url, ?raw, and new URL(import.meta.url) WHEN resolved THEN all four are deterministically reachable', () => {
    const result = runMjs([FIXTURE_ENTRY])
    expect(result.errors).toEqual([])
    const reachable = result.surfaces.fixture.reachable_files
    expect(reachable).toContain(FIXTURE_ENTRY)
    expect(reachable).toContain('scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/styles/global.css')
    expect(reachable).toContain('scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/styles/base.css')
    expect(reachable).toContain('scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/media/bg.png')
    expect(reachable).toContain('scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/logo.png')
    expect(reachable).toContain('scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/data.txt')
    expect(reachable).toContain('scripts/agent-ops/tests/fixtures/visual_impact/vite_deterministic/media/icon.svg')
    expect(result.surfaces.fixture.unknown_impact).toEqual([])
  })
})
