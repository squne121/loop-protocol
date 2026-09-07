import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'

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

/**
 * Issue #2525 fix_delta (P1-A/P1-B/P1-C, OWNER REQUEST_CHANGES on PR #2548,
 * 2026-09-06): regression coverage for the AST-based (never regex/comment-
 * stripping-based) structural analysis of vite.config.* and tsconfig.json
 * `extends` chains. Each scenario writes a throwaway `repo_root` under the
 * OS tmpdir (never a new checked-in fixture file, staying within this
 * Issue's frozen Allowed Paths list) and invokes the real .mjs subprocess
 * directly against it.
 */
function writeTmpRepo(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), 'rvi-fixdelta-'))
  for (const [relPath, content] of Object.entries(files)) {
    const abs = join(dir, relPath)
    mkdirSync(dirname(abs), { recursive: true })
    writeFileSync(abs, content, 'utf8')
  }
  return dir
}

describe('resolve_visual_impact.mjs P1-A: AST-based (never regex-based) Vite config comment/string handling (Issue #2525 fix_delta)', () => {
  it('GIVEN resolve.alias on the SAME line as a string literal containing "//" (a URL) WHEN resolved THEN resolve.alias is still detected (not silently truncated by comment-stripping)', () => {
    const dir = writeTmpRepo({
      'vite.config.ts': [
        "import { defineConfig } from 'vite'",
        '',
        "export default defineConfig({ base: 'https://example.test/', resolve: { alias: { '@app': '/tmp/src/model' } } })",
        '',
      ].join('\n'),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/vite\.config\.ts.*resolve\.alias/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN the ONLY occurrence of "resolve:" is inside a `//` comment WHEN resolved THEN it is NOT falsely detected as a real resolve config', () => {
    const dir = writeTmpRepo({
      'vite.config.ts': [
        "// resolve: { alias: { '@app': '/x' } } -- this is only a comment",
        "import { defineConfig } from 'vite'",
        '',
        "export default defineConfig({ base: '/x' })",
        '',
      ].join('\n'),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      expect(result.unsupported_resolution_settings).toEqual([])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

describe('resolve_visual_impact.mjs P1-B: structural (never regex) detection of ordinary Vite object syntax (Issue #2525 fix_delta)', () => {
  it('GIVEN a shorthand `{ resolve }` property (imported from another file, no literal "resolve:" text) WHEN resolved THEN it is reported as an unsupported-resolution diagnostic', () => {
    const dir = writeTmpRepo({
      'shared.ts': "export const resolve = { alias: { '@app': '/absolute/path/to/src/model' } }\n",
      'vite.config.ts': [
        "import { defineConfig } from 'vite'",
        "import { resolve } from './shared'",
        '',
        'export default defineConfig({ resolve })',
        '',
      ].join('\n'),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/vite\.config\.ts.*"resolve".*shorthand/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN a top-level spread `export default { ...sharedConfig }` (no literal "resolve" key visible) WHEN resolved THEN it is reported as an unsupported-resolution diagnostic (cannot prove absence of resolve)', () => {
    const dir = writeTmpRepo({
      'shared-config.ts': "export const sharedConfig = { base: '/x' }\n",
      'vite.config.ts': [
        "import { sharedConfig } from './shared-config'",
        '',
        'export default { ...sharedConfig }',
        '',
      ].join('\n'),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/vite\.config\.ts config object spreads an externally-defined value/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN a quoted property name `{ \'resolve\': sharedResolve }` referencing an external identifier WHEN resolved THEN it is reported as an unsupported-resolution diagnostic', () => {
    const dir = writeTmpRepo({
      'shared-resolve.ts': "export const sharedResolve = { alias: { '@app': '/x' } }\n",
      'vite.config.ts': [
        "import { sharedResolve } from './shared-resolve'",
        '',
        "export default { 'resolve': sharedResolve }",
        '',
      ].join('\n'),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/vite\.config\.ts.*resolve.*externally-defined/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN `resolve: { ... sharedResolve }` (a spread WITH a space after "...") WHEN resolved THEN it is still reported as an unsupported-resolution diagnostic', () => {
    const dir = writeTmpRepo({
      'shared-resolve.ts': "export const sharedResolve = { alias: { '@app': '/x' } }\n",
      'vite.config.ts': [
        "import { defineConfig } from 'vite'",
        "import { sharedResolve } from './shared-resolve'",
        '',
        'export default defineConfig({ resolve: { ... sharedResolve } })',
        '',
      ].join('\n'),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/vite\.config\.ts "resolve" field spreads an externally-defined value/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

describe('resolve_visual_impact.mjs P1-C: tsconfig `extends` array + search-depth exhaustion (Issue #2525 fix_delta)', () => {
  it('GIVEN array-form `extends` WHERE only the referenced target carries `paths` WHEN resolved THEN it is reported as an unsupported-resolution diagnostic', () => {
    const dir = writeTmpRepo({
      'tsconfig.json': JSON.stringify({ extends: ['./tsconfig.a.json', './tsconfig.b.json'] }),
      'tsconfig.a.json': JSON.stringify({}),
      'tsconfig.b.json': JSON.stringify({ compilerOptions: { paths: { '@x/*': ['./*'] } } }),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/tsconfig\.b\.json compilerOptions\.paths/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN array-form `extends` WHERE only the referenced target carries `baseUrl` WHEN resolved THEN it is reported as an unsupported-resolution diagnostic', () => {
    const dir = writeTmpRepo({
      'tsconfig.json': JSON.stringify({ extends: ['./tsconfig.a.json', './tsconfig.b.json'] }),
      'tsconfig.a.json': JSON.stringify({}),
      'tsconfig.b.json': JSON.stringify({ compilerOptions: { baseUrl: '.' } }),
    })
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/tsconfig\.b\.json compilerOptions\.baseUrl/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN an unsupported setting exists ONLY past the current MAX_EXTENDS_DEPTH search-depth limit (a 9-level-deep chain) WHEN resolved THEN a "could not be fully explored" diagnostic fires -- never a clean pass', () => {
    // tsconfig.json (depth 0) -> level1.json (depth 1) -> ... -> level8.json
    // (depth 8). MAX_EXTENDS_DEPTH is 8 (depths 0..7 are read; depth 8 is
    // never visited) -- level8.json is the ONLY config with `paths` set, so
    // a clean pass here would prove the truncation guard is not wired up.
    const DEPTH = 8
    const files: Record<string, string> = {
      'tsconfig.json': JSON.stringify({ extends: './level1.json' }),
    }
    for (let i = 1; i < DEPTH; i += 1) {
      files[`level${i}.json`] = JSON.stringify({ extends: `./level${i + 1}.json` })
    }
    files[`level${DEPTH}.json`] = JSON.stringify({ compilerOptions: { paths: { '@x/*': ['./*'] } } })

    const dir = writeTmpRepo(files)
    try {
      const result = runMjs([], dir)
      expect(result.errors).toEqual([])
      const joined = result.unsupported_resolution_settings.join('\n')
      // The truncation diagnostic must fire (never a silent clean pass)...
      expect(joined).toMatch(/could not be fully explored within the search-depth limit/)
      // ...and the deepest config's own `paths` (beyond the search limit)
      // must NOT be the source of a clean-looking pass either.
      expect(joined).not.toMatch(new RegExp(`level${DEPTH}\\.json compilerOptions\\.paths`))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

/**
 * Issue #2551 AC1-AC7: bounded CSS lexical scanner/extractor regression
 * coverage for resolve_visual_impact.mjs's `visitCss()`. Each scenario
 * writes a throwaway `repo_root` under the OS tmpdir via `writeTmpRepo()`
 * (never a new checked-in fixture file, staying within this Issue's frozen
 * Allowed Paths list) and invokes the real .mjs subprocess directly against
 * it -- never a reimplementation of the CSS scanner under test.
 */
describe('resolve_visual_impact.mjs CSS bounded lexical scanner (Issue #2551)', () => {
  it('GIVEN url("icon.svg") with no "./" prefix WHEN resolved THEN it is reachable as a repo-local relative reference (AC1)', () => {
    const dir = writeTmpRepo({
      'style.css': '.a { background: url("icon.svg"); }\n',
      'icon.svg': '<svg></svg>\n',
    })
    try {
      const result = runMjs(['style.css'], dir)
      expect(result.errors).toEqual([])
      expect(result.surfaces.fixture.reachable_files).toContain('icon.svg')
      expect(result.surfaces.fixture.unknown_impact).toEqual([])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN every @import syntax variant (quoted/unquoted string, quoted/unquoted url(), "./" optional) WHEN resolved THEN every import target is recognized as a local dependency and is reachable (AC2)', () => {
    const dir = writeTmpRepo({
      'a.css': '@import "b.css";\n',
      'b.css': "@import 'c.css';\n",
      'c.css': '@import url("d.css");\n',
      'd.css': "@import url('e.css');\n",
      'e.css': '@import url(f.css);\n',
      'f.css': '.leaf { color: blue; }\n',
    })
    try {
      const result = runMjs(['a.css'], dir)
      expect(result.errors).toEqual([])
      const reachable = result.surfaces.fixture.reachable_files
      for (const file of ['a.css', 'b.css', 'c.css', 'd.css', 'e.css', 'f.css']) {
        expect(reachable).toContain(file)
      }
      expect(result.surfaces.fixture.unknown_impact).toEqual([])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN a local @import chain whose imported CSS has a nested relative url() WHEN resolved THEN the nested asset is transitively reachable, and is never itself recursively walked as CSS (AC3)', () => {
    const dir = writeTmpRepo({
      'root.css': '@import "./nested/inner.css";\n',
      'nested/inner.css': '.b { background: url("./deep.svg"); }\n',
      'nested/deep.svg': '<svg></svg>\n',
    })
    try {
      const result = runMjs(['root.css'], dir)
      expect(result.errors).toEqual([])
      const reachable = result.surfaces.fixture.reachable_files
      expect(reachable).toContain('nested/inner.css')
      expect(reachable).toContain('nested/deep.svg')
      // the asset itself is never treated as a CSS graph root/traversed
      expect(reachable).not.toContain('nested/deep.svg.css')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN a CSS comment containing a dead url() reference to a nonexistent file WHEN resolved THEN it is never treated as a dependency and never triggers unknown_impact (AC4)', () => {
    const dir = writeTmpRepo({
      'style.css': ['/* old: background: url("./deleted.png"); */', '.a { color: red; }', ''].join('\n'),
    })
    try {
      const result = runMjs(['style.css'], dir)
      expect(result.errors).toEqual([])
      expect(result.surfaces.fixture.reachable_files).toEqual(['style.css'])
      expect(result.surfaces.fixture.unknown_impact).toEqual([])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN fragment-only, URI-scheme, and network-path url() references WHEN resolved THEN none are classified as a missing local reference (AC5)', () => {
    const dir = writeTmpRepo({
      'style.css': [
        '.a { background: url(#fragment-ref); }',
        '.b { background: url("data:image/png;base64,AAAA"); }',
        '.c { background: url("https://example.test/img.png"); }',
        '.d { background: url("//cdn.example.test/img.png"); }',
        '',
      ].join('\n'),
    })
    try {
      const result = runMjs(['style.css'], dir)
      expect(result.errors).toEqual([])
      expect(result.surfaces.fixture.unknown_impact).toEqual([])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN url("./missing.svg") referencing a nonexistent local file WHEN resolved THEN it is recorded in unknown_impact (AC6)', () => {
    const dir = writeTmpRepo({
      'style.css': '.a { background: url("./missing.svg"); }\n',
    })
    try {
      const result = runMjs(['style.css'], dir)
      expect(result.errors).toEqual([])
      const unknown = result.surfaces.fixture.unknown_impact as Array<{ file: string; kind: string; detail: string }>
      expect(unknown.length).toBeGreaterThanOrEqual(1)
      expect(unknown.some((entry) => entry.file === 'style.css' && entry.detail === './missing.svg')).toBe(true)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN url("/logo.svg") (Vite public/ root-absolute reference) under the repo\'s default Vite root/publicDir semantics WHEN resolved THEN it resolves to <root>/public/logo.svg, never the OS filesystem root (AC7)', () => {
    const dir = writeTmpRepo({
      'style.css': '.a { background: url("/logo.svg"); }\n',
      'public/logo.svg': '<svg></svg>\n',
    })
    try {
      const result = runMjs(['style.css'], dir)
      expect(result.errors).toEqual([])
      expect(result.surfaces.fixture.reachable_files).toContain('public/logo.svg')
      expect(result.surfaces.fixture.unknown_impact).toEqual([])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('GIVEN a non-default Vite "root" in vite.config.ts WHEN a CSS root-absolute url() is present THEN root-absolute resolution is skipped (never silently mis-resolved against the OS filesystem root) and the non-default root is reported as an unsupported-resolution diagnostic (AC7 fallback path)', () => {
    const dir = writeTmpRepo({
      'vite.config.ts': [
        "import { defineConfig } from 'vite'",
        '',
        "export default defineConfig({ root: './app' })",
        '',
      ].join('\n'),
      'style.css': '.a { background: url("/logo.svg"); }\n',
    })
    try {
      const result = runMjs(['style.css'], dir)
      expect(result.errors).toEqual([])
      expect(result.surfaces.fixture.reachable_files).not.toContain('public/logo.svg')
      const joined = result.unsupported_resolution_settings.join('\n')
      expect(joined).toMatch(/vite\.config\.ts.*"root"/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})
