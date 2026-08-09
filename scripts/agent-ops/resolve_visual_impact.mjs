#!/usr/bin/env node
// resolve_visual_impact.mjs (Issue #2019)
//
// TypeScript compiler API 層。static import graph の決定論的解決に限定し、
// registry 読み込み・policy 判定・orchestration は
// scripts/agent-ops/resolve_visual_impact.py に委譲する（二層構成）。
//
// Contract:
//   stdin  : RESOLVE_VISUAL_IMPACT_MJS_REQUEST_V1 (JSON)
//     {
//       "repo_root": ".",
//       "surfaces": { "<surface_id>": { "modules": [...], "styles": [...], "assets": [...] } }
//     }
//   stdout : RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1 (JSON)
//
// Capability matrix (Issue #2019 In Scope / D):
//   deterministic: CSS @import / global CSS, CSS url(), static asset import,
//     `?url`/`?raw`, static `new URL(x, import.meta.url)`.
//   unknown_impact (fail-closed, never silently "no impact"):
//     import.meta.glob (incl. negative globs), variable/unbounded dynamic
//     import, virtual/generated module specifiers, dynamic plugin
//     resolution / unresolvable static relative imports.
//
// tsconfig paths/baseUrl, Vite resolve.alias, package.json imports/exports
// are NOT resolved by this module's bare-import walk (only the
// relative-specifier resolution path below is implemented/exercised). PR
// #2045 OWNER fix_delta P1-1: rather than silently treating a bare import
// as `external` (out of scope, no impact) whenever one of these settings IS
// configured, `detectUnsupportedResolutionSettings()` below scans
// tsconfig.json / vite.config.* / package.json for them and, if present,
// pushes a resolver-fatal entry into `errors[]` -- which
// resolve_visual_impact.py's `resolve()` already surfaces as a
// `resolve_result.errors` entry, and `evaluate_pr_policy()` already fails
// closed on any non-empty `errors` list (never a silent no-impact PASS).
// This repository does not configure any of these settings today, so the
// guard is currently inert; it exists to catch the day one of them is
// introduced.

import ts from 'typescript'
import { readFileSync, existsSync, statSync } from 'node:fs'
import path from 'node:path'
import { Buffer } from 'node:buffer'

const RESOLVER_VERSION = '1'

const ASSET_EXTENSIONS = new Set([
  '.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.ico', '.svg', '.woff', '.woff2', '.ttf', '.mp3', '.wav',
])

const CANDIDATE_TS_EXTENSIONS = ['', '.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs']
const INDEX_SUFFIXES = ['/index.ts', '/index.tsx']

function toPosix(p) {
  return p.split(path.sep).join('/')
}

function readStdin() {
  const chunks = []
  return new Promise((resolve, reject) => {
    process.stdin.on('data', (c) => chunks.push(c))
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    process.stdin.on('error', reject)
  })
}

function isDirectory(p) {
  try {
    return statSync(p).isDirectory()
  } catch {
    return false
  }
}

/** Resolve a relative/absolute specifier to a concrete file that exists on disk. */
function resolveFileCandidate(absNoExt) {
  for (const ext of CANDIDATE_TS_EXTENSIONS) {
    const candidate = absNoExt + ext
    if (existsSync(candidate) && !isDirectory(candidate)) return candidate
  }
  for (const suffix of INDEX_SUFFIXES) {
    const candidate = absNoExt + suffix
    if (existsSync(candidate)) return candidate
  }
  return null
}

function stripAssetSuffix(specifier) {
  if (specifier.endsWith('?url')) return { base: specifier.slice(0, -4), suffix: 'url' }
  if (specifier.endsWith('?raw')) return { base: specifier.slice(0, -4), suffix: 'raw' }
  return { base: specifier, suffix: null }
}

function isRelativeSpecifier(specifier) {
  return specifier.startsWith('./') || specifier.startsWith('../') || specifier.startsWith('/')
}

function isVirtualSpecifier(specifier) {
  // Vite / rollup plugin convention: "virtual:xxx" or scheme-like "xxx:yyy"
  // that is neither relative nor a bare npm package path.
  return /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(specifier) && !specifier.startsWith('node:')
}

class Resolver {
  constructor(repoRoot) {
    this.repoRoot = repoRoot
    this.visited = new Set()
    this.reachable = new Set()
    this.unknownImpact = []
  }

  relPath(absPath) {
    return toPosix(path.relative(this.repoRoot, absPath))
  }

  addUnknown(file, kind, detail) {
    this.unknownImpact.push({ file: this.relPath(file), kind, detail })
  }

  /** containingFileAbs is always included so unresolved/virtual specifiers
   * are attributed to the file that referenced them (never a bogus path
   * derived from the unresolved specifier itself). */
  resolveModuleSpecifier(specifier, containingFileAbs) {
    const containingDir = path.dirname(containingFileAbs)
    const { base, suffix } = stripAssetSuffix(specifier)

    if (isVirtualSpecifier(base)) {
      return { kind: 'unknown', unknownKind: 'virtual_module', detail: specifier, containingFileAbs }
    }
    if (!isRelativeSpecifier(base)) {
      // Bare package import (node_modules) — not part of this repository's
      // visual producer graph. Not an impact source.
      return { kind: 'external' }
    }

    const absNoExt = path.resolve(containingDir, base)
    const ext = path.extname(base).toLowerCase()

    if (suffix) {
      // `?url` / `?raw` — deterministic asset reference, do not descend further.
      const resolved = existsSync(absNoExt) ? absNoExt : resolveFileCandidate(absNoExt)
      if (!resolved) {
        return { kind: 'unknown', unknownKind: 'unresolvable_static_import', detail: specifier, containingFileAbs }
      }
      return { kind: 'asset', file: resolved }
    }

    if (ext === '.css') {
      const resolved = existsSync(absNoExt) ? absNoExt : null
      if (!resolved) {
        return { kind: 'unknown', unknownKind: 'unresolvable_static_import', detail: specifier, containingFileAbs }
      }
      return { kind: 'css', file: resolved }
    }

    if (ASSET_EXTENSIONS.has(ext)) {
      const resolved = existsSync(absNoExt) ? absNoExt : null
      if (!resolved) {
        return { kind: 'unknown', unknownKind: 'unresolvable_static_import', detail: specifier, containingFileAbs }
      }
      return { kind: 'asset', file: resolved }
    }

    const resolved = resolveFileCandidate(absNoExt)
    if (!resolved) {
      return { kind: 'unknown', unknownKind: 'unresolvable_static_import', detail: specifier, containingFileAbs }
    }
    return { kind: 'module', file: resolved }
  }

  visitCss(fileAbs) {
    if (this.visited.has(fileAbs)) return
    this.visited.add(fileAbs)
    this.reachable.add(this.relPath(fileAbs))
    let text
    try {
      text = readFileSync(fileAbs, 'utf8')
    } catch (err) {
      // PR #2045 OWNER fix_delta P1-1: a CSS @import/url() read failure
      // (permission error, race with a concurrent delete, etc.) previously
      // vanished silently -- any `@import`/`url()` targets reachable only
      // through this file were then never walked, which could hide a real
      // affected-surface producer. Fail closed via the existing
      // unknown_impact plumbing instead.
      this.addUnknown(fileAbs, 'css_read_failure', `${fileAbs}: ${String(err)}`)
      return
    }
    const dir = path.dirname(fileAbs)
    const importRe = /@import\s+(?:url\()?['"]([^'"]+)['"]\)?/g
    let m
    while ((m = importRe.exec(text))) {
      const spec = m[1]
      if (!isRelativeSpecifier(spec)) continue
      const abs = path.resolve(dir, spec)
      if (existsSync(abs)) this.visitCss(abs)
    }
    const urlRe = /url\(\s*(['"]?)([^'")]+)\1\s*\)/g
    while ((m = urlRe.exec(text))) {
      const spec = m[2]
      if (spec.startsWith('data:') || /^https?:\/\//.test(spec) || spec.startsWith('#')) continue
      if (!isRelativeSpecifier(spec)) continue
      const abs = path.resolve(dir, spec.split('#')[0].split('?')[0])
      if (existsSync(abs)) {
        this.visited.add(abs)
        this.reachable.add(this.relPath(abs))
      }
    }
  }

  visitModule(fileAbs) {
    if (this.visited.has(fileAbs)) return
    this.visited.add(fileAbs)
    this.reachable.add(this.relPath(fileAbs))
    let text
    try {
      text = readFileSync(fileAbs, 'utf8')
    } catch (err) {
      // PR #2045 OWNER fix_delta P1-1: same fail-closed treatment as the CSS
      // read-failure fix above -- a module read failure must never silently
      // truncate the import graph walk.
      this.addUnknown(fileAbs, 'module_read_failure', `${fileAbs}: ${String(err)}`)
      return
    }
    const sourceFile = ts.createSourceFile(fileAbs, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TSX)

    const handleSpecifierNode = (node) => {
      const result = this.resolveModuleSpecifier(node.text, fileAbs)
      this.dispatch(result)
    }

    const visit = (node) => {
      if (
        (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
        node.moduleSpecifier &&
        ts.isStringLiteral(node.moduleSpecifier)
      ) {
        handleSpecifierNode(node.moduleSpecifier)
      } else if (ts.isCallExpression(node)) {
        // import.meta.glob(...)
        if (
          ts.isPropertyAccessExpression(node.expression) &&
          node.expression.name.text === 'glob' &&
          ts.isMetaProperty(node.expression.expression) &&
          node.expression.expression.name.text === 'meta'
        ) {
          this.addUnknown(fileAbs, 'import_meta_glob', node.getText(sourceFile))
        } else if (node.expression.kind === ts.SyntaxKind.ImportKeyword) {
          // dynamic import(...)
          const arg = node.arguments[0]
          if (arg && ts.isStringLiteral(arg)) {
            handleSpecifierNode(arg)
          } else if (arg) {
            this.addUnknown(fileAbs, 'dynamic_variable_import', node.getText(sourceFile))
          }
        }
      } else if (ts.isNewExpression(node) && node.expression.getText(sourceFile) === 'URL') {
        const args = node.arguments ?? []
        const secondArgText = args[1] ? args[1].getText(sourceFile) : ''
        if (secondArgText.includes('import.meta.url')) {
          const first = args[0]
          if (first && ts.isStringLiteral(first)) {
            const abs = path.resolve(path.dirname(fileAbs), first.text)
            if (existsSync(abs)) {
              this.visited.add(abs)
              this.reachable.add(this.relPath(abs))
            } else {
              this.addUnknown(fileAbs, 'unresolvable_static_import', first.text)
            }
          } else {
            this.addUnknown(fileAbs, 'dynamic_new_url', node.getText(sourceFile))
          }
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(sourceFile)
  }

  dispatch(result) {
    if (!result || result.kind === 'external') return
    if (result.kind === 'module') this.visitModule(result.file)
    else if (result.kind === 'css') this.visitCss(result.file)
    else if (result.kind === 'asset') {
      this.visited.add(result.file)
      this.reachable.add(this.relPath(result.file))
    } else if (result.kind === 'unknown') {
      this.addUnknown(result.containingFileAbs, result.unknownKind, result.detail)
    }
  }

  run(entryFiles) {
    for (const entry of entryFiles) {
      const abs = path.resolve(this.repoRoot, entry)
      const ext = path.extname(abs).toLowerCase()
      if (ext === '.css') this.visitCss(abs)
      else this.visitModule(abs)
    }
    return {
      reachable_files: Array.from(this.reachable).sort(),
      unknown_impact: this.unknownImpact,
    }
  }
}

/** PR #2045 OWNER fix_delta P1-1: detect tsconfig `paths`/`baseUrl`, Vite
 * `resolve.alias`, and package.json `imports`/`exports` -- none of which
 * this module's bare-import resolution understands. A repository that
 * configures any of these could have bare specifiers silently resolve to a
 * different file than this walker assumes (or not resolve at all), which
 * would make the affected-surface determination wrong without ever
 * reporting an error. Returns a list of human-readable problem strings
 * (empty when nothing unsupported is configured). This never EXECUTES
 * tsconfig.json / vite.config.* / package.json -- text/JSON parsing only. */
function detectUnsupportedResolutionSettings(repoRoot) {
  const problems = []

  const tsconfigPath = path.join(repoRoot, 'tsconfig.json')
  if (existsSync(tsconfigPath)) {
    try {
      const raw = readFileSync(tsconfigPath, 'utf8')
      const parsed = ts.parseConfigFileTextToJson(tsconfigPath, raw)
      if (parsed.error) {
        problems.push(
          `tsconfig.json failed to parse (${ts.flattenDiagnosticMessageText(parsed.error.messageText, ' ')}) -- refusing to assume no paths/baseUrl are configured`,
        )
      } else {
        const compilerOptions = (parsed.config && parsed.config.compilerOptions) || {}
        if (compilerOptions.paths && Object.keys(compilerOptions.paths).length > 0) {
          problems.push('tsconfig.json compilerOptions.paths is configured but not supported by this bare-import resolver')
        }
        if (typeof compilerOptions.baseUrl === 'string' && compilerOptions.baseUrl !== '') {
          problems.push('tsconfig.json compilerOptions.baseUrl is configured but not supported by this bare-import resolver')
        }
      }
    } catch (err) {
      problems.push(`tsconfig.json read/parse failure: ${String(err)}`)
    }
  }

  for (const candidate of ['vite.config.ts', 'vite.config.js', 'vite.config.mjs', 'vite.config.mts']) {
    const viteConfigPath = path.join(repoRoot, candidate)
    if (!existsSync(viteConfigPath)) continue
    let viteText
    try {
      viteText = readFileSync(viteConfigPath, 'utf8')
    } catch (err) {
      problems.push(`${candidate} read failure: ${String(err)}`)
      continue
    }
    // Conservative text scan only (never executes the config module): a
    // false positive here is fail-closed (acceptable -- Runtime
    // Verification Applicability fallback_policy), a false negative would
    // defeat the entire point of this guard.
    if (/\bresolve\s*:\s*\{[^}]*\balias\s*:/s.test(viteText) || /\balias\s*:\s*(\{|\[)/.test(viteText)) {
      problems.push(`${candidate} appears to configure resolve.alias, which is not supported by this bare-import resolver`)
    }
  }

  const packageJsonPath = path.join(repoRoot, 'package.json')
  if (existsSync(packageJsonPath)) {
    try {
      const pkg = JSON.parse(readFileSync(packageJsonPath, 'utf8'))
      if (pkg.imports && typeof pkg.imports === 'object' && Object.keys(pkg.imports).length > 0) {
        problems.push('package.json "imports" subpath mapping is configured but not supported by this bare-import resolver')
      }
      if (pkg.exports && typeof pkg.exports === 'object' && Object.keys(pkg.exports).length > 0) {
        problems.push('package.json "exports" is configured but not supported by this bare-import resolver')
      }
    } catch (err) {
      problems.push(`package.json read/parse failure: ${String(err)}`)
    }
  }

  return problems
}

async function main() {
  const raw = await readStdin()
  let request
  try {
    request = JSON.parse(raw)
  } catch (err) {
    process.stdout.write(JSON.stringify({
      schema: 'RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1',
      resolver_version: RESOLVER_VERSION,
      surfaces: {},
      errors: [`invalid_json_input: ${String(err)}`],
    }))
    process.exitCode = 1
    return
  }

  const repoRoot = path.resolve(request.repo_root || '.')
  const surfaces = request.surfaces || {}
  const output = {}
  const errors = []

  for (const problem of detectUnsupportedResolutionSettings(repoRoot)) {
    errors.push(`unsupported_resolution_setting: ${problem}`)
  }

  for (const [surfaceId, def] of Object.entries(surfaces)) {
    const entries = [
      ...(def.modules || []),
      ...(def.styles || []),
      ...(def.assets || []),
      ...(def.config || []),
    ]
    const resolver = new Resolver(repoRoot)
    try {
      output[surfaceId] = resolver.run(entries)
    } catch (err) {
      errors.push(`surface ${surfaceId}: ${String(err && err.stack ? err.stack : err)}`)
      output[surfaceId] = { reachable_files: [], unknown_impact: [] }
    }
  }

  process.stdout.write(JSON.stringify({
    schema: 'RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1',
    resolver_version: RESOLVER_VERSION,
    surfaces: output,
    errors,
  }, null, 2))
  process.exitCode = errors.length > 0 ? 1 : 0
}

main()
