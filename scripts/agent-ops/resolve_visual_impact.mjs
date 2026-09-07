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
// tsconfig paths/baseUrl (including inherited via `extends`), Vite
// resolve.alias (including indirection through an imported/spread config
// object), package.json imports/exports (including the `exports` string
// shorthand) are NOT resolved by this module's bare-import walk (only the
// relative-specifier resolution path below is implemented/exercised). PR
// #2045 OWNER fix_delta P1-1 originally pushed a resolver-fatal entry into
// `errors[]` whenever one of these settings was detected -- which
// unconditionally blocked the PR regardless of whether any changed path
// was actually affected by the gap. Issue #2525 (OWNER anchor comment
// 2026-09-06, REQUEST_CHANGES on Issue #2019's own fix_delta): this
// analysis-incompleteness signal is emitted below as
// `unsupported_resolution_settings` (a diagnostic, never in `errors[]`)
// instead. resolve_visual_impact.py's `resolve()` connects a non-empty
// `unsupported_resolution_settings` list to the SAME all-registered-
// surfaces-affected fallback as `global_invalidators`, so the PR still gets
// full disposition/VRT evidence evaluation rather than an unconditional
// resolver-fatal stop. This repository does not configure any of these
// settings at its own top level today, so the guard is currently inert
// there; it exists to catch the day one of them is introduced (and is
// exercised directly against the
// scripts/agent-ops/tests/fixtures/visual_impact/unsupported_resolution/
// fixture, whose `repo_root` IS one of these configurations, by
// tests/agent-ops/resolve-visual-impact-vite-deterministic.test.ts).

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

// --- Issue #2551: CSS bounded lexical scanner helpers -----------------------
//
// CSS `@import`/`url()` references follow different rules than JS/TS module
// specifiers (`isRelativeSpecifier()` above, which is untouched and still
// governs JS/TS module resolution only): a bare specifier with no leading
// `./`/`../` (e.g. `icon.svg`) is STILL a repo-local relative reference in
// CSS -- there is no bare-package-import convention for CSS -- and a single
// leading `/` is a Vite `public/` root-absolute reference, never an OS-root
// absolute filesystem path.

/** Classify a CSS `@import`/`url()` reference (already stripped of any
 * trailing fragment/query suffix). Returns one of: 'fragment' (`#...`),
 * 'network-path' (`//host/...`), 'scheme' (`data:`/`http:`/`https:`/etc.),
 * 'root-absolute' (a single leading `/`), or 'relative' (everything else,
 * `./`/`../`-prefixed or bare). */
function classifyCssReference(spec) {
  if (spec.startsWith('#')) return 'fragment'
  if (spec.startsWith('//')) return 'network-path'
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(spec)) return 'scheme'
  if (spec.startsWith('/')) return 'root-absolute'
  return 'relative'
}

/** Strip a trailing fragment/query suffix from a CSS reference (e.g.
 * `icon.svg#view` or `icon.svg?raw`) before filesystem resolution. */
function stripCssReferenceSuffix(spec) {
  return spec.split('#')[0].split('?')[0]
}

// `@import "x.css"` / `@import 'x.css'` / `@import url("x.css")` /
// `@import url('x.css')` / `@import url(x.css)` -- quoted/unquoted, with or
// without a leading `./`. Two alternatives: the `url(...)` form (capture
// groups 1/2) and the bare quoted-string form (capture groups 3/4).
const CSS_IMPORT_RE_SOURCE = "@import\\s+(?:url\\(\\s*(['\"]?)([^'\")]+)\\1\\s*\\)|(['\"])([^'\"]+)\\3)"

// An ordinary `url(...)` reference (quoted or unquoted). Applied to CSS text
// AFTER `@import` statements have already been stripped out, so an
// `@import url(...)` is never double-processed as a plain `url()` asset
// reference below.
const CSS_URL_RE_SOURCE = "url\\(\\s*(['\"]?)([^'\")]+)\\1\\s*\\)"

/** CSS comments are replaced with equal-length whitespace (never simply
 * deleted), so token adjacency across a removed comment can never
 * accidentally fuse two otherwise-unrelated tokens. A dead reference inside
 * a comment must never be treated as a real dependency (Issue #2551 AC4).
 * This is a bounded lexical pass, not a full CSS tokenizer -- it does not
 * attempt to distinguish a comment-opening sequence that happens to appear
 * inside a quoted string literal (not a real-world concern for this
 * repository's own stylesheets). */
function stripCssComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, (match) => ' '.repeat(match.length))
}

class Resolver {
  constructor(repoRoot, cssOptions = {}) {
    this.repoRoot = repoRoot
    this.visited = new Set()
    this.reachable = new Set()
    this.unknownImpact = []
    // Issue #2551 AC7: Vite `public/` root-absolute CSS `url()` resolution
    // is only attempted when the repo's own vite.config.* has been
    // confirmed (by detectViteRootOrPublicDirProblems() in main()) to leave
    // `root`/`publicDir` at their Vite defaults. When that cannot be
    // confirmed, root-absolute references are left unresolved here -- the
    // caller already routes the underlying diagnostic into the SAME
    // all-registered-surfaces-affected fallback as any other unsupported
    // resolution setting, so this must never silently mis-resolve against
    // the OS filesystem root instead.
    this.viteRootPublicDirIsDefault = cssOptions.viteRootPublicDirIsDefault !== false
    this.publicDirAbs = cssOptions.publicDirAbs || path.join(repoRoot, 'public')
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
    // Issue #2551: bounded lexical scanner. CSS comments are stripped first
    // (never scanned as dependencies -- AC4); `@import` targets are
    // extracted and, when local, recursively walked (AC2/AC3) BEFORE the
    // remaining text is scanned for ordinary `url(...)` references, so an
    // `@import url(...)` form is never also double-processed as a plain
    // asset url() below.
    const withoutComments = stripCssComments(text)
    const importRe = new RegExp(CSS_IMPORT_RE_SOURCE, 'g')
    let m
    while ((m = importRe.exec(withoutComments))) {
      const rawSpec = m[2] !== undefined ? m[2] : m[4]
      this.handleCssImportReference(rawSpec, dir, fileAbs)
    }
    const withoutImports = withoutComments.replace(new RegExp(CSS_IMPORT_RE_SOURCE, 'g'), (match) => ' '.repeat(match.length))
    const urlRe = new RegExp(CSS_URL_RE_SOURCE, 'g')
    while ((m = urlRe.exec(withoutImports))) {
      this.handleCssUrlReference(m[2], dir, fileAbs)
    }
  }

  /** `@import` is recursively walked ONLY for a repo-local relative target
   * (Issue #2551 AC2/AC3: `./`/`../`/bare-no-prefix, quoted or unquoted
   * url()). A missing local `@import` target is recorded as unknown_impact
   * (fail-closed, matching this module's existing read-failure philosophy)
   * rather than silently dropped. */
  handleCssImportReference(rawSpec, dir, containingFileAbs) {
    // Classification MUST run on the raw (unstripped) specifier -- e.g.
    // `#frag` is a fragment-only reference in its entirety, whereas
    // `./foo.css#frag` is a relative reference carrying a fragment suffix.
    // Stripping the suffix first would misclassify the former as an empty
    // (and therefore trivially "relative") specifier.
    if (classifyCssReference(rawSpec) !== 'relative') return
    const spec = stripCssReferenceSuffix(rawSpec)
    const abs = path.resolve(dir, spec)
    if (existsSync(abs)) {
      this.visitCss(abs)
    } else {
      this.addUnknown(containingFileAbs, 'css_missing_local_reference', rawSpec)
    }
  }

  /** An ordinary `url(...)` reference is NEVER recursively walked as CSS
   * (Issue #2551 AC3) -- a resolved local target is added to `reachable` as
   * an asset dependency only. Fragment-only (`#...`), URI-scheme
   * (`data:`/`http:`/`https:`/etc.), and network-path (`//host/...`)
   * references are never local and must never be misclassified as a missing
   * local reference (AC5). A Vite `public/` root-absolute reference
   * (`/logo.svg`) resolves against `<root>/public/...` (AC7) only when the
   * repo's Vite `root`/`publicDir` configuration has been confirmed to be
   * left at its defaults. */
  handleCssUrlReference(rawSpec, dir, containingFileAbs) {
    // Classification MUST run on the raw (unstripped) specifier -- see the
    // matching comment in handleCssImportReference() above.
    const kind = classifyCssReference(rawSpec)
    if (kind === 'fragment' || kind === 'scheme' || kind === 'network-path') return
    const spec = stripCssReferenceSuffix(rawSpec)

    let abs
    if (kind === 'root-absolute') {
      if (!this.viteRootPublicDirIsDefault) return
      abs = path.resolve(this.publicDirAbs, spec.slice(1))
    } else {
      abs = path.resolve(dir, spec)
    }

    if (existsSync(abs)) {
      this.visited.add(abs)
      this.reachable.add(this.relPath(abs))
    } else {
      this.addUnknown(containingFileAbs, 'css_missing_local_reference', rawSpec)
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

/** Resolve a tsconfig `extends` specifier relative to `baseDir`. Only
 * relative specifiers ("./foo", "../foo") are resolved deterministically,
 * matching this module's existing relative-import philosophy elsewhere.
 * Returns `null` for a non-relative `extends` (e.g. a bare npm package
 * specifier like `@tsconfig/node20`) -- the caller treats that as its own
 * unsupported-resolution problem rather than silently skipping it, since
 * this walker cannot safely resolve npm package resolution semantics
 * either. */
function resolveTsconfigExtendsCandidate(baseDir, extendsSpecifier) {
  if (!isRelativeSpecifier(extendsSpecifier)) return null
  let candidate = path.resolve(baseDir, extendsSpecifier)
  if (path.extname(candidate) !== '.json') candidate += '.json'
  return candidate
}

/** `tsconfig.json`'s `extends` can be a single string OR (TypeScript >=5.0)
 * an array of strings; both are walked identically. Non-string / empty
 * entries are dropped defensively (malformed input is caught separately by
 * `ts.parseConfigFileTextToJson`'s own diagnostics upstream, not here). */
function normalizeExtendsList(extendsValue) {
  if (typeof extendsValue === 'string' && extendsValue !== '') return [extendsValue]
  if (Array.isArray(extendsValue)) return extendsValue.filter((v) => typeof v === 'string' && v !== '')
  return []
}

/** PR #2045 OWNER fix_delta P1-1 originally checked ONLY the root
 * tsconfig.json's own `compilerOptions.paths`/`baseUrl`. Issue #2525 P1-C:
 * `extends` (single string OR array, TS >=5.0) is walked as a DFS with an
 * explicit ancestry stack per branch -- a true cycle (a config that
 * transitively extends itself along ONE chain) is detected and terminated
 * safely; a config reached a second time via a DIFFERENT branch (diamond
 * inheritance, e.g. two array entries that both extend a shared base) is
 * simply skipped the second time (its paths/baseUrl were already reported
 * on first visit) rather than misreported as a cycle. The walk is bounded
 * by `MAX_EXTENDS_DEPTH` per branch; when that bound is hit while a branch
 * still has an unexplored config to visit, this is reported as its OWN
 * diagnostic (never silently treated as "search completed, no problems") --
 * a config beyond the bound could configure paths/baseUrl this walker never
 * saw. A non-relative / otherwise unresolvable `extends` target (bare
 * package-name style extends, or a path that does not resolve to a file) is
 * likewise reported rather than silently skipped. Never executes any config
 * file -- text/JSON parsing only. */
function detectTsconfigChainProblems(repoRoot) {
  const problems = []
  const rootPath = path.join(repoRoot, 'tsconfig.json')
  if (!existsSync(rootPath)) return problems

  const MAX_EXTENDS_DEPTH = 8
  // Files already fully processed via ANY branch -- prevents duplicate
  // diagnostics (and mis-detected "cycles") on diamond inheritance shapes.
  const globallyVisited = new Set()

  function walk(currentPath, depth, ancestryStack) {
    if (ancestryStack.includes(currentPath)) {
      problems.push(
        `tsconfig extends chain starting at ${toPosix(path.relative(repoRoot, rootPath))} contains a cycle at ${currentPath} -- refusing to assume no paths/baseUrl are configured`,
      )
      return
    }
    if (depth >= MAX_EXTENDS_DEPTH) {
      problems.push(
        `tsconfig extends chain starting at ${toPosix(path.relative(repoRoot, rootPath))} could not be fully explored within the search-depth limit (${MAX_EXTENDS_DEPTH}) -- ${currentPath} (and any further inheritance from it) was never visited -- refusing to assume no paths/baseUrl are configured`,
      )
      return
    }
    if (globallyVisited.has(currentPath)) return
    globallyVisited.add(currentPath)

    if (!existsSync(currentPath)) {
      problems.push(`tsconfig extends chain references a file that does not exist: ${currentPath}`)
      return
    }

    let raw
    try {
      raw = readFileSync(currentPath, 'utf8')
    } catch (err) {
      problems.push(`${currentPath} read failure: ${String(err)}`)
      return
    }

    const parsed = ts.parseConfigFileTextToJson(currentPath, raw)
    if (parsed.error) {
      problems.push(
        `${currentPath} failed to parse (${ts.flattenDiagnosticMessageText(parsed.error.messageText, ' ')}) -- refusing to assume no paths/baseUrl are configured`,
      )
      return
    }

    const config = parsed.config || {}
    const compilerOptions = config.compilerOptions || {}
    const relCurrent = toPosix(path.relative(repoRoot, currentPath))
    if (compilerOptions.paths && Object.keys(compilerOptions.paths).length > 0) {
      problems.push(`${relCurrent} compilerOptions.paths is configured but not supported by this bare-import resolver`)
    }
    if (typeof compilerOptions.baseUrl === 'string' && compilerOptions.baseUrl !== '') {
      problems.push(`${relCurrent} compilerOptions.baseUrl is configured but not supported by this bare-import resolver`)
    }

    const nextAncestryStack = [...ancestryStack, currentPath]
    for (const extendsSpecifier of normalizeExtendsList(config.extends)) {
      const nextPath = resolveTsconfigExtendsCandidate(path.dirname(currentPath), extendsSpecifier)
      if (!nextPath) {
        problems.push(
          `${relCurrent} extends a non-relative specifier (${extendsSpecifier}) that this bare-import resolver cannot safely resolve`,
        )
        continue
      }
      walk(nextPath, depth + 1, nextAncestryStack)
    }
  }

  walk(rootPath, 0, [])
  return problems
}

/** Issue #2525 P1-A/P1-B fix_delta (OWNER REQUEST_CHANGES on PR #2548,
 * 2026-09-06): the previous implementation stripped `//`/`/* *\/` comments
 * with a regex, then scanned the remaining text with a `resolve\s*:` regex.
 * This does not distinguish string-literal content from real code -- a
 * config with e.g. `base: 'https://example.test/', resolve: { alias: ... }`
 * on one line had everything after `https:` deleted by the `//.*$` removal,
 * silently destroying a real `resolve.alias` before it could be scanned
 * (a regression versus the pre-comment-stripping detector). It also could
 * not structurally recognize ordinary object syntax such as a shorthand
 * property (`{ resolve }`), a quoted property name (`{ 'resolve': ... }`),
 * or a top-level spread (`{ ...sharedConfig }`) that might itself carry a
 * `resolve` key.
 *
 * Both functions below instead parse the config source with the same
 * TypeScript compiler API used elsewhere in this file (`ts.createSourceFile`
 * + AST traversal) and walk the exported config object EXPRESSION
 * structurally. Comments are parser trivia and are never visited by
 * `sourceFile.statements/properties` traversal, so a comment containing the
 * literal text "resolve:" can never produce a match, and a string literal
 * (however it is punctuated) is tokenized correctly by the real parser
 * instead of a regex.
 *
 * This is still not a Vite config evaluator: the exported config's shape is
 * inspected structurally, but a property's VALUE is never executed/
 * resolved beyond direct object-literal literals. Whenever that structural
 * walk cannot PROVE the `resolve` field (or the top-level config object
 * itself) is free of alias/paths-affecting configuration -- an externally
 * defined identifier, a spread, a computed property name, a non-analyzable
 * default export shape, etc. -- it is reported as its own
 * unsupported-resolution problem (routed to the SAME all-registered-
 * surfaces-affected fallback as a directly-detected `resolve.alias`), never
 * silently treated as "no config present". A false positive here is
 * fail-closed (acceptable -- Runtime Verification Applicability
 * fallback_policy); a false negative would defeat the entire point of this
 * guard. */
function tsPropertyNameText(name) {
  if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name)) return name.text
  if (ts.isStringLiteral(name) || ts.isNumericLiteral(name)) return name.text
  return null // ComputedPropertyName or another non-statically-readable name
}

function unwrapExpression(expr) {
  let current = expr
  while (
    ts.isAsExpression(current) ||
    ts.isParenthesizedExpression(current) ||
    ts.isSatisfiesExpression(current) ||
    ts.isNonNullExpression(current)
  ) {
    current = current.expression
  }
  return current
}

/** Structural analysis of a `resolve` (or `resolve.alias`/`resolve.dedupe`)
 * field's VALUE expression. Never executes the config module. */
function collectResolveValueProblems(valueNode, candidateRelPath, fieldLabel) {
  const problems = []
  if (ts.isObjectLiteralExpression(valueNode)) {
    for (const prop of valueNode.properties) {
      if (ts.isSpreadAssignment(prop)) {
        problems.push(
          `${candidateRelPath} "${fieldLabel}" field spreads an externally-defined value (${prop.expression.getText()}) that cannot be statically confirmed to be free of alias/paths configuration`,
        )
        continue
      }
      if (ts.isShorthandPropertyAssignment(prop)) {
        if (prop.name.text === 'alias' || prop.name.text === 'dedupe') {
          problems.push(
            `${candidateRelPath} "${fieldLabel}.${prop.name.text}" is configured but not supported by this bare-import resolver`,
          )
        }
        continue
      }
      if (
        ts.isPropertyAssignment(prop) ||
        ts.isMethodDeclaration(prop) ||
        ts.isGetAccessor(prop) ||
        ts.isSetAccessor(prop)
      ) {
        const name = tsPropertyNameText(prop.name)
        if (name === null) {
          problems.push(
            `${candidateRelPath} "${fieldLabel}" field has a computed/dynamic property name that cannot be statically confirmed not to be "alias"/"dedupe"`,
          )
          continue
        }
        if (name === 'alias' || name === 'dedupe') {
          problems.push(`${candidateRelPath} "${fieldLabel}.${name}" is configured but not supported by this bare-import resolver`)
        }
      }
    }
    return problems
  }
  // Non-object-literal value (identifier, call expression, conditional,
  // etc.) -- an externally-defined or computed value this module never
  // evaluates.
  problems.push(
    `${candidateRelPath} "${fieldLabel}" field references an externally-defined or computed value (${JSON.stringify(unwrapExpression(valueNode).getText())}) that cannot be statically confirmed to be free of alias/paths configuration`,
  )
  return problems
}

/** Structural analysis of the exported Vite config object literal's
 * top-level properties, looking for a `resolve` field by PropertyAssignment
 * / ShorthandPropertyAssignment / quoted-name PropertyAssignment, and
 * flagging a top-level SpreadAssignment or computed property name as
 * undecidable (either could carry a `resolve` key this walker cannot see).
 * Never executes the config module. */
function collectConfigObjectResolveProblems(objectLiteral, candidateRelPath) {
  const problems = []
  for (const prop of objectLiteral.properties) {
    if (ts.isSpreadAssignment(prop)) {
      problems.push(
        `${candidateRelPath} config object spreads an externally-defined value (${prop.expression.getText()}) that cannot be statically confirmed to be free of a "resolve" field`,
      )
      continue
    }
    if (ts.isShorthandPropertyAssignment(prop)) {
      if (prop.name.text === 'resolve') {
        problems.push(
          `${candidateRelPath} "resolve" field is a shorthand property referencing an externally-defined value that cannot be statically confirmed to be free of alias/paths configuration`,
        )
      }
      continue
    }
    if (ts.isPropertyAssignment(prop)) {
      const name = tsPropertyNameText(prop.name)
      if (name === null) {
        problems.push(
          `${candidateRelPath} config object has a computed/dynamic property name that cannot be statically confirmed not to be "resolve"`,
        )
        continue
      }
      if (name !== 'resolve') continue
      problems.push(...collectResolveValueProblems(prop.initializer, candidateRelPath, 'resolve'))
    }
  }
  return problems
}

/** Locate `export default {...}` / `export default defineConfig({...})` (or
 * any other single-call wrapper whose first argument is a static object
 * literal) and run `collectConfigObjectResolveProblems()` over it. When the
 * default export's shape cannot be statically reduced to an inspectable
 * object literal at all (no default export, a bare identifier, a call whose
 * argument is not itself an object literal, a function-form
 * `defineConfig((env) => ({...}))`, etc.), this is undecidable -- reported
 * as its own problem rather than silently treated as "no resolve config
 * present" (this module does not implement a full Vite config evaluator).
 * Never executes the config module. */
function detectViteConfigProblems(viteSourceText, candidateRelPath) {
  const sourceFile = ts.createSourceFile(candidateRelPath, viteSourceText, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS)

  let exportExpr = null
  for (const stmt of sourceFile.statements) {
    if (ts.isExportAssignment(stmt) && !stmt.isExportEquals) {
      exportExpr = stmt.expression
      break
    }
  }
  if (!exportExpr) {
    return [
      `${candidateRelPath} has no statically-analyzable "export default" -- cannot confirm absence of a "resolve" field`,
    ]
  }

  const expr = unwrapExpression(exportExpr)
  let configObject = null
  if (ts.isObjectLiteralExpression(expr)) {
    configObject = expr
  } else if (ts.isCallExpression(expr)) {
    const firstArg = expr.arguments[0]
    const unwrappedArg = firstArg ? unwrapExpression(firstArg) : null
    if (unwrappedArg && ts.isObjectLiteralExpression(unwrappedArg)) {
      configObject = unwrappedArg
    }
  }
  if (!configObject) {
    return [
      `${candidateRelPath} "export default" is not a statically-analyzable object literal or defineConfig(...)-style call -- cannot confirm absence of a "resolve" field`,
    ]
  }

  return collectConfigObjectResolveProblems(configObject, candidateRelPath)
}

/** Issue #2551 AC7: `url("/logo.svg")` (Vite `public/` root-absolute
 * reference) must resolve to `<root>/public/logo.svg` under the CURRENT
 * repo's Vite default semantics (`root` = project root, `publicDir` =
 * "public") -- never silently mis-resolved against the OS filesystem root,
 * and never assumed safe when `root`/`publicDir` cannot be statically
 * confirmed to still be at their defaults. This locates the same exported
 * config object literal shape `detectViteConfigProblems()` above locates,
 * but is kept as a fully separate function (rather than a refactor of that
 * `resolve`-focused walk) so that walk's existing diagnostic message text
 * (asserted on verbatim by existing tests) never changes. Never executes
 * the config module. */
function locateViteConfigObjectLiteralForRootPublicDir(viteSourceText, candidateRelPath) {
  const sourceFile = ts.createSourceFile(candidateRelPath, viteSourceText, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS)
  let exportExpr = null
  for (const stmt of sourceFile.statements) {
    if (ts.isExportAssignment(stmt) && !stmt.isExportEquals) {
      exportExpr = stmt.expression
      break
    }
  }
  if (!exportExpr) return null
  const expr = unwrapExpression(exportExpr)
  if (ts.isObjectLiteralExpression(expr)) return expr
  if (ts.isCallExpression(expr)) {
    const firstArg = expr.arguments[0]
    const unwrappedArg = firstArg ? unwrapExpression(firstArg) : null
    if (unwrappedArg && ts.isObjectLiteralExpression(unwrappedArg)) return unwrappedArg
  }
  return null
}

const VITE_ROOT_PUBLIC_DIR_DEFAULTS = { root: new Set(['.', './']), publicDir: new Set(['public']) }

/** Structural analysis of the exported Vite config object literal's
 * top-level properties, looking for `root` / `publicDir` fields whose value
 * is anything other than the Vite default literal. A top-level spread,
 * computed property name, or shorthand property referencing `root`/
 * `publicDir` is undecidable and is reported the same way (fail-closed --
 * never assumed to be the default). */
function collectRootOrPublicDirProblems(objectLiteral, candidateRelPath) {
  const problems = []
  for (const prop of objectLiteral.properties) {
    if (ts.isSpreadAssignment(prop)) {
      problems.push(
        `${candidateRelPath} config object spreads an externally-defined value (${prop.expression.getText()}) that cannot be statically confirmed to leave "root"/"publicDir" at their Vite defaults`,
      )
      continue
    }
    if (ts.isShorthandPropertyAssignment(prop)) {
      if (prop.name.text === 'root' || prop.name.text === 'publicDir') {
        problems.push(
          `${candidateRelPath} "${prop.name.text}" field is a shorthand property referencing an externally-defined value that cannot be statically confirmed to be the Vite default`,
        )
      }
      continue
    }
    if (ts.isPropertyAssignment(prop)) {
      const name = tsPropertyNameText(prop.name)
      if (name === null) {
        problems.push(
          `${candidateRelPath} config object has a computed/dynamic property name that cannot be statically confirmed not to be "root"/"publicDir"`,
        )
        continue
      }
      if (name !== 'root' && name !== 'publicDir') continue
      const valueNode = unwrapExpression(prop.initializer)
      if (ts.isStringLiteral(valueNode) && VITE_ROOT_PUBLIC_DIR_DEFAULTS[name].has(valueNode.text)) continue
      problems.push(
        `${candidateRelPath} "${name}" is configured to a non-default value (or a value that cannot be statically confirmed to be the Vite default) -- Vite "public/" root-absolute CSS url() resolution requires root=project root, publicDir="public"`,
      )
    }
  }
  return problems
}

/** Returns diagnostic strings (empty when `root`/`publicDir` are confirmed
 * left at their Vite defaults across every discovered vite.config.*
 * candidate). The caller (main()) both merges these into the shared
 * `unsupported_resolution_settings` diagnostic list AND uses
 * `.length === 0` to decide whether per-surface CSS root-absolute `url()`
 * resolution (Issue #2551 AC7) may be attempted at all. */
function detectViteRootOrPublicDirProblems(repoRoot) {
  const problems = []
  for (const candidate of ['vite.config.ts', 'vite.config.js', 'vite.config.mjs', 'vite.config.mts']) {
    const viteConfigPath = path.join(repoRoot, candidate)
    if (!existsSync(viteConfigPath)) continue
    let viteTextRaw
    try {
      viteTextRaw = readFileSync(viteConfigPath, 'utf8')
    } catch (err) {
      problems.push(`${candidate} read failure: ${String(err)}`)
      continue
    }
    const configObject = locateViteConfigObjectLiteralForRootPublicDir(viteTextRaw, candidate)
    if (!configObject) {
      problems.push(
        `${candidate} "export default" is not a statically-analyzable object literal or defineConfig(...)-style call -- cannot confirm Vite "root"/"publicDir" are left at their defaults`,
      )
      continue
    }
    problems.push(...collectRootOrPublicDirProblems(configObject, candidate))
  }
  return problems
}

/** PR #2045 OWNER fix_delta P1-1 / Issue #2525: detect tsconfig
 * `paths`/`baseUrl` (including inherited via `extends`), Vite
 * `resolve.alias` (direct or indirected through an imported/spread
 * config), and package.json `imports`/`exports` (including the `exports`
 * string shorthand) -- none of which this module's bare-import resolution
 * understands. A repository that configures any of these could have bare
 * specifiers silently resolve to a different file than this walker assumes
 * (or not resolve at all), which would make the affected-surface
 * determination wrong without ever reporting it. Returns a list of
 * human-readable problem strings (empty when nothing unsupported is
 * configured) -- the caller surfaces these as a diagnostic
 * (`unsupported_resolution_settings`), never as a resolver-fatal
 * `errors[]` entry. This never EXECUTES tsconfig.json / vite.config.* /
 * package.json -- text/JSON parsing only. */
function detectUnsupportedResolutionSettings(repoRoot) {
  const problems = []

  problems.push(...detectTsconfigChainProblems(repoRoot))

  for (const candidate of ['vite.config.ts', 'vite.config.js', 'vite.config.mjs', 'vite.config.mts']) {
    const viteConfigPath = path.join(repoRoot, candidate)
    if (!existsSync(viteConfigPath)) continue
    let viteTextRaw
    try {
      viteTextRaw = readFileSync(viteConfigPath, 'utf8')
    } catch (err) {
      problems.push(`${candidate} read failure: ${String(err)}`)
      continue
    }
    // Issue #2525 P1-A/P1-B: structural AST-based analysis (see
    // `detectViteConfigProblems` above) -- never a regex/comment-stripping
    // text scan (comments are parser trivia and are simply never visited).
    problems.push(...detectViteConfigProblems(viteTextRaw, candidate))
  }

  const packageJsonPath = path.join(repoRoot, 'package.json')
  if (existsSync(packageJsonPath)) {
    try {
      const pkg = JSON.parse(readFileSync(packageJsonPath, 'utf8'))
      if (pkg.imports && typeof pkg.imports === 'object' && Object.keys(pkg.imports).length > 0) {
        problems.push('package.json "imports" subpath mapping is configured but not supported by this bare-import resolver')
      }
      if (typeof pkg.exports === 'string' && pkg.exports !== '') {
        problems.push(
          `package.json "exports" string shorthand (${JSON.stringify(pkg.exports)}) is configured but not supported by this bare-import resolver`,
        )
      } else if (pkg.exports && typeof pkg.exports === 'object' && Object.keys(pkg.exports).length > 0) {
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

  // Issue #2525: diagnostic only -- NEVER pushed into `errors[]` (which is
  // resolver-fatal, PR block). resolve_visual_impact.py's `resolve()`
  // connects a non-empty list here to the same all-registered-
  // surfaces-affected fallback as `global_invalidators`.
  const unsupportedResolutionSettings = detectUnsupportedResolutionSettings(repoRoot)

  // Issue #2551 AC7: tracked separately so a per-surface Resolver can decide
  // whether Vite `public/` root-absolute CSS url() resolution may be
  // attempted at all -- but any problem found here is ALSO merged into the
  // same diagnostic list above, connecting to the SAME all-registered-
  // surfaces-affected fallback (never a resolver-fatal errors[] entry, never
  // silent mis-resolution against the OS filesystem root).
  const rootPublicDirProblems = detectViteRootOrPublicDirProblems(repoRoot)
  unsupportedResolutionSettings.push(...rootPublicDirProblems)
  const cssOptions = {
    viteRootPublicDirIsDefault: rootPublicDirProblems.length === 0,
    publicDirAbs: path.join(repoRoot, 'public'),
  }

  for (const [surfaceId, def] of Object.entries(surfaces)) {
    const entries = [
      ...(def.modules || []),
      ...(def.styles || []),
      ...(def.assets || []),
      ...(def.config || []),
    ]
    const resolver = new Resolver(repoRoot, cssOptions)
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
    unsupported_resolution_settings: unsupportedResolutionSettings,
  }, null, 2))
  process.exitCode = errors.length > 0 ? 1 : 0
}

main()
