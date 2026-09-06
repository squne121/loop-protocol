import { describe, expect, it, afterAll } from 'vitest'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'

/**
 * Issue #2019 AC5: resolve_visual_impact.py reads BOTH base and head
 * docs/dev/visual-surfaces.yml and treats a head-side producer-mapping
 * DELETION as affected (bypass prevention). This integration test invokes
 * the real Python orchestrator CLI end to end (never re-implements its
 * logic in TypeScript).
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
  errors: string[]
  resolver_fallback_active: boolean
  unsupported_resolution_settings: string[]
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

/**
 * Issue #2525 AC1 (registry union half): when resolve_visual_impact.mjs
 * detects an unsupported resolution setting, EVERY surface in the
 * base/head registry union -- not just the ones whose producer graph the
 * walker could resolve -- becomes affected. Runs the real end-to-end
 * Node + Python subprocess boundary against a synthetic 2-surface registry
 * (base/head identical -- Issue #2019 AC5's own union logic is exercised
 * separately above) and `--repo-root` pointed at the
 * unsupported_resolution fixture, whose package.json/tsconfig.json/
 * tsconfig.base.json/vite.config.ts genuinely configure the three
 * unsupported settings (see resolve-visual-impact-vite-deterministic.test.ts
 * for the mjs-level proof of that).
 */
describe('resolve_visual_impact.py all-registered-surfaces-affected fallback (Issue #2525)', () => {
  const tmpDir = mkdtempSync(resolve(tmpdir(), 'resolve-visual-impact-unsupported-resolution-registry-'))
  const registryPath = resolve(tmpDir, 'registry.yml')
  const surfaceYaml = (surfaceId: string, spec: string, baseline: string) =>
    [
      `  ${surfaceId}:`,
      '    producers:',
      '      modules:',
      '        - entry.ts',
      '      styles: []',
      '      assets: []',
      '      config: []',
      '    contracts:',
      '      runner: vitest-browser-mode',
      `      spec: ${spec}`,
      `      baseline: ${baseline}`,
      '      job: component-vrt-report',
      '      update_command_id: vitest_component_vrt_update',
      '      verify_command_id: vitest_component_vrt_verify',
      '      maturity: provisional',
      '    policy:',
      '      disposition_required: true',
    ].join('\n')
  const registryYaml = [
    'schema_version: 1',
    'global_invalidators: []',
    // `--repo-root` below is the fixture directory itself, so changed paths
    // passed to this registry are relative to IT (e.g. "dependency.ts"),
    // never the real repo-root-relative fixture path.
    'coverage_roots:',
    '  - "**"',
    'surfaces:',
    surfaceYaml('fixture-surface-a', 'fixture-a.vrt.test.ts', 'fixture-a-baseline.png'),
    surfaceYaml('fixture-surface-b', 'fixture-b.vrt.test.ts', 'fixture-b-baseline.png'),
    '',
  ].join('\n')
  writeFileSync(registryPath, registryYaml, 'utf8')

  afterAll(() => {
    rmSync(tmpDir, { recursive: true, force: true })
  })

  it('GIVEN the unsupported_resolution fixture repo_root and a synthetic 2-surface registry WHEN a changed path is resolved THEN BOTH registered surfaces are affected via the fallback (never resolver-fatal errors[])', () => {
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
    expect(result.unsupported_resolution_settings.length).toBeGreaterThan(0)
    const affectedIds = result.affected_surfaces.map((entry) => entry.surface_id).sort()
    expect(affectedIds).toEqual(['fixture-surface-a', 'fixture-surface-b'])
  })
})
