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
const UNSUPPORTED_RESOLUTION_FIXTURE_DIR = resolve(
  REPO_ROOT,
  'scripts',
  'agent-ops',
  'tests',
  'fixtures',
  'visual_impact',
  'unsupported_resolution',
)

interface MjsResult {
  schema: string
  surfaces: Record<string, { reachable_files: string[]; unknown_impact: unknown[] }>
  errors: string[]
  unsupported_resolution_settings: string[]
}

function runMjs(entries: string[], repoRoot: string = REPO_ROOT): MjsResult {
  const request = JSON.stringify({ repo_root: repoRoot, surfaces: { fixture: { modules: entries } } })
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
    expect(result.unsupported_resolution_settings).toEqual([])
  })
})

/**
 * Issue #2525 AC1/AC4: detectUnsupportedResolutionSettings() extended to
 * detect (a) package.json `exports` string shorthand, (b) tsconfig `paths`/
 * `baseUrl` inherited via `extends`, and (c) a Vite `resolve` config that
 * cannot be statically confirmed to be alias-free (imported from another
 * file). Invoked directly against
 * scripts/agent-ops/tests/fixtures/visual_impact/unsupported_resolution/
 * (its `repo_root`) so the REAL detector runs against real config files --
 * never a reimplementation. None of this is ever pushed into
 * `errors[]` (resolver-fatal, PR block) -- only into the diagnostic
 * `unsupported_resolution_settings` field.
 */
describe('resolve_visual_impact.mjs detectUnsupportedResolutionSettings() (Issue #2525)', () => {
  it('GIVEN the unsupported_resolution fixture (exports string shorthand + inherited tsconfig paths/baseUrl + indirect Vite resolve) WHEN resolved THEN all three are reported as diagnostics, never as errors[]', () => {
    const result = runMjs(['entry.ts'], UNSUPPORTED_RESOLUTION_FIXTURE_DIR)
    expect(result.errors).toEqual([])
    expect(result.unsupported_resolution_settings.length).toBeGreaterThanOrEqual(4)

    const joined = result.unsupported_resolution_settings.join('\n')
    // (a) package.json exports string shorthand
    expect(joined).toMatch(/package\.json "exports" string shorthand/)
    // (b) tsconfig paths/baseUrl inherited via extends (tsconfig.json itself
    // has no compilerOptions -- only tsconfig.base.json does)
    expect(joined).toMatch(/tsconfig\.base\.json compilerOptions\.paths/)
    expect(joined).toMatch(/tsconfig\.base\.json compilerOptions\.baseUrl/)
    // (c) Vite resolve field referencing an externally-defined value
    expect(joined).toMatch(/vite\.config\.ts.*resolve.*externally-defined/)
  })

  it('GIVEN entry.ts imports dependency.ts only through the unsupported tsconfig path alias WHEN resolved THEN the walker treats it as external (never silently reachable) -- the exact gap detectUnsupportedResolutionSettings() reports', () => {
    const result = runMjs(['entry.ts'], UNSUPPORTED_RESOLUTION_FIXTURE_DIR)
    expect(result.surfaces.fixture.reachable_files).toEqual(['entry.ts'])
    expect(result.surfaces.fixture.reachable_files).not.toContain('dependency.ts')
  })

  it('GIVEN the real top-level repo config (no unsupported resolution settings configured) WHEN resolved THEN unsupported_resolution_settings stays empty (no false positive regression)', () => {
    const result = runMjs([FIXTURE_ENTRY])
    expect(result.unsupported_resolution_settings).toEqual([])
  })
})
