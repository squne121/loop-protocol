import { describe, expect, it, afterAll } from 'vitest'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'

/**
 * Issue #2019 AC8: a UI-candidate path (under coverage_roots) that maps to
 * NO surface's producers must fail closed as unmapped_visual_candidate --
 * never silently treated as no-impact PASS.
 */

const REPO_ROOT = resolve(__dirname, '..', '..')
const RESOLVER_CLI = resolve(REPO_ROOT, 'scripts', 'agent-ops', 'resolve_visual_impact.py')
const UNSUPPORTED_RESOLUTION_FIXTURE_DIR = resolve(
  REPO_ROOT,
  'scripts',
  'agent-ops',
  'tests',
  'fixtures',
  'visual_impact',
  'unsupported_resolution',
)

interface ResolveVisualImpactResult {
  schema: string
  affected_surfaces: Array<{ surface_id: string; reason: string }>
  unmapped_visual_candidates: string[]
  errors: string[]
  resolver_fallback_active: boolean
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

/**
 * Issue #2525 AC1/AC2 (unmapped half): the unsupported_resolution fixture's
 * entry.ts reaches dependency.ts only through a tsconfig path alias this
 * walker cannot follow -- WITHOUT the fallback, `dependency.ts` would be
 * `unmapped_visual_candidate` (no surface's producers/reachable graph
 * covers it). With the fallback active (the fixture's config genuinely
 * configures unsupported settings), the diagnostic is still retained
 * (never dropped) -- AC2's "policy blocking" half is verified separately,
 * Node-independently, in
 * scripts/agent-ops/tests/test_resolve_visual_impact_fallback_policy.py
 * (which asserts `evaluate_pr_policy()` does not double-count it).
 */
describe('resolve_visual_impact.py unmapped_visual_candidates diagnostic retained under fallback (Issue #2525)', () => {
  const tmpDir = mkdtempSync(resolve(tmpdir(), 'resolve-visual-impact-unsupported-resolution-coverage-'))
  const registryPath = resolve(tmpDir, 'registry.yml')
  // Deliberately only ONE registered surface, whose producer is entry.ts --
  // dependency.ts is never listed as a producer of anything, so absent the
  // fallback it would be unmapped.
  const registryYaml = [
    'schema_version: 1',
    'global_invalidators: []',
    'coverage_roots:',
    '  - "**"',
    'surfaces:',
    '  fixture-surface-a:',
    '    producers:',
    '      modules:',
    '        - entry.ts',
    '      styles: []',
    '      assets: []',
    '      config: []',
    '    contracts:',
    '      runner: vitest-browser-mode',
    '      spec: fixture-a.vrt.test.ts',
    '      baseline: fixture-a-baseline.png',
    '      job: component-vrt-report',
    '      update_command_id: vitest_component_vrt_update',
    '      verify_command_id: vitest_component_vrt_verify',
    '      maturity: provisional',
    '    policy:',
    '      disposition_required: true',
    '',
  ].join('\n')
  writeFileSync(registryPath, registryYaml, 'utf8')

  afterAll(() => {
    rmSync(tmpDir, { recursive: true, force: true })
  })

  it('GIVEN dependency.ts changes (reachable only via an unsupported tsconfig path alias) WHEN resolved THEN it is still reported as unmapped_visual_candidate (diagnostic retained) AND resolver_fallback_active is true AND the registered surface is affected via the fallback', () => {
    const result = runResolver([
      '--repo-root',
      UNSUPPORTED_RESOLUTION_FIXTURE_DIR,
      '--registry',
      registryPath,
      '--schema',
      resolve(REPO_ROOT, 'docs', 'dev', 'visual-surfaces.schema.json'),
      '--changed-path',
      'dependency.ts',
    ])
    expect(result.errors).toEqual([])
    expect(result.resolver_fallback_active).toBe(true)
    expect(result.unmapped_visual_candidates).toContain('dependency.ts')
    const affectedIds = result.affected_surfaces.map((entry) => entry.surface_id)
    expect(affectedIds).toContain('fixture-surface-a')
  })
})
