import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

/**
 * Issue #2019 AC5: resolve_visual_impact.py reads BOTH base and head
 * docs/dev/visual-surfaces.yml and treats a head-side producer-mapping
 * DELETION as affected (bypass prevention). This integration test invokes
 * the real Python orchestrator CLI end to end (never re-implements its
 * logic in TypeScript).
 */

const REPO_ROOT = resolve(__dirname, '..', '..')
const RESOLVER_CLI = resolve(REPO_ROOT, 'scripts', 'agent-ops', 'resolve_visual_impact.py')

interface ResolveVisualImpactResult {
  schema: string
  affected_surfaces: Array<{ surface_id: string; reason: string }>
  errors: string[]
}

function runResolver(args: string[]): ResolveVisualImpactResult {
  const stdout = execFileSync('uv', ['run', '--locked', 'python3', RESOLVER_CLI, ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  })
  return JSON.parse(stdout) as ResolveVisualImpactResult
}

describe('resolve_visual_impact.py registry-first base/head union', () => {
  it('GIVEN a src/ui/combatHud.ts change WHEN resolved against the real registry THEN combat-hud-running is affected (real repo producer mapping, not a deletion)', () => {
    const result = runResolver(['--changed-path', 'src/ui/combatHud.ts'])
    expect(result.errors).toEqual([])
    const affectedIds = result.affected_surfaces.map((entry) => entry.surface_id)
    expect(affectedIds).toContain('combat-hud-running')
  })

  it('GIVEN an unrelated file not covered by any registry surface WHEN resolved THEN no surface is affected via producer mapping', () => {
    const result = runResolver(['--changed-path', 'README.md'])
    expect(result.affected_surfaces).toEqual([])
  })
})
