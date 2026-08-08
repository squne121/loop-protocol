import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

/**
 * Issue #2019 AC8: a UI-candidate path (under coverage_roots) that maps to
 * NO surface's producers must fail closed as unmapped_visual_candidate --
 * never silently treated as no-impact PASS.
 */

const REPO_ROOT = resolve(__dirname, '..', '..')
const RESOLVER_CLI = resolve(REPO_ROOT, 'scripts', 'agent-ops', 'resolve_visual_impact.py')

interface ResolveVisualImpactResult {
  schema: string
  affected_surfaces: Array<{ surface_id: string; reason: string }>
  unmapped_visual_candidates: string[]
  errors: string[]
}

function runResolver(args: string[]): ResolveVisualImpactResult {
  const stdout = execFileSync('uv', ['run', '--locked', 'python3', RESOLVER_CLI, ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  })
  return JSON.parse(stdout) as ResolveVisualImpactResult
}

describe('resolve_visual_impact.py coverage-boundary fail-closed policy', () => {
  it('GIVEN a src/ui/** path not mapped to any registered surface WHEN resolved THEN it is reported as unmapped_visual_candidate (not silently no-impact)', () => {
    const result = runResolver(['--changed-path', 'src/ui/debugPause.ts'])
    expect(result.unmapped_visual_candidates).toContain('src/ui/debugPause.ts')
    const affectedIds = result.affected_surfaces.map((entry) => entry.surface_id)
    expect(affectedIds).not.toContain('src/ui/debugPause.ts')
  })

  it('GIVEN a path outside coverage_roots entirely WHEN resolved THEN it is neither affected nor flagged as unmapped_visual_candidate', () => {
    const result = runResolver(['--changed-path', 'README.md'])
    expect(result.unmapped_visual_candidates).not.toContain('README.md')
    expect(result.affected_surfaces).toEqual([])
  })

  it('GIVEN a path that IS mapped via a registered producer WHEN resolved THEN it is NOT reported as unmapped_visual_candidate', () => {
    const result = runResolver(['--changed-path', 'src/ui/combatHud.ts'])
    expect(result.unmapped_visual_candidates).not.toContain('src/ui/combatHud.ts')
  })
})
