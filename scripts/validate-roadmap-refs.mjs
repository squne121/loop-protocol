#!/usr/bin/env node
/**
 * validate-roadmap-refs.mjs
 *
 * Validates fenced YAML roadmaps in docs/product/playable-roadmap.md.
 *
 * Validation policy:
 * - skip markdown frontmatter
 * - parse all ```yaml and ```yml code fences
 * - fail on parse error and duplicate YAML keys
 * - validate spec_prerequisites and spec_destination as string arrays when present
 * - disallow duplicate items in each of these arrays
 * - enforce spec_destination item format: "path — description" (em dash)
 * - validate spec_destination/spec_prerequisites path targets:
 *   - non-empty path
 *   - no URL/fragment
 *   - no absolute path
 *   - no parent traversal ("..")
 *   - no directory
 *   - no symlink
 *   - exists as regular file
 * - deny stale alias targets
 * - for M2 only: spec_prerequisites/destination must match exact expected sets
 */

import { readFileSync, lstatSync, existsSync } from 'node:fs'
import { dirname, resolve, isAbsolute, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'yaml'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const REPO_ROOT = resolve(__dirname, '..')
const DEFAULT_DOC_PATH = 'docs/product/playable-roadmap.md'
const FENCE_RE = /```(?:yaml|yml)\r?\n([\s\S]*?)\r?\n```/g
const FRONTMATTER_RE = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/m
const STALE_ALIAS = new Set([
  'docs/product/features/movement.md',
  'docs/product/features/projectile.md',
])

const EXPECTED_M2_PREREQUISITES = [
  'docs/product/features/movement-projectile.md',
]
const EXPECTED_M2_DESTINATIONS = [
  'docs/product/features/movement-projectile.md',
  'docs/product/features/combat-core.md',
  'docs/product/features/sortie.md',
]

const CLI_USAGE = [
  'Usage: node scripts/validate-roadmap-refs.mjs [--file <path>]',
  'Default target: docs/product/playable-roadmap.md',
].join('\n')

function normalizeArgs(argv) {
  const parsed = { file: DEFAULT_DOC_PATH, unknownArgs: [] }
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--file') {
      if (i + 1 >= argv.length) {
        parsed.unknownArgs.push('--file requires a value')
        break
      }
      parsed.file = argv[i + 1]
      i += 1
      continue
    } else if (arg === '-h' || arg === '--help') {
      parsed.help = true
      continue
    }
    parsed.unknownArgs.push(`unknown argument: ${arg}`)
  }
  return parsed
}

function removeLeadingFrontmatter(text) {
  if (!text.startsWith('---')) {
    return text
  }
  return text.replace(FRONTMATTER_RE, '')
}

function fail(errors, source, message) {
  errors.push(`[FAIL] ${source} ${message}`)
}

function parseYamlBlocks(text, source, errors) {
  const results = []
  let match
  let index = 0
  while ((match = FENCE_RE.exec(text)) !== null) {
    index += 1
    const raw = match[1]
    try {
      const parsed = parse(raw, { uniqueKeys: true, strict: true })
      results.push({ index, parsed })
    } catch (error) {
      fail(errors, `block#${index}`, `invalid YAML: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
  if (results.length === 0) {
    fail(errors, 'document', 'no fenced yaml/yml block found')
  }
  return results
}

function collectDuplicateErrors(values, field, context, errors) {
  const seen = new Set()
  for (const value of values) {
    if (seen.has(value)) {
      fail(errors, context, `duplicate item in ${field}: ${value}`)
    } else {
      seen.add(value)
    }
  }
}

function ensureOptionalStringArray(block, blockLabel, field, errors) {
  if (!Object.prototype.hasOwnProperty.call(block, field)) {
    return null
  }
  const value = block[field]
  if (!Array.isArray(value)) {
    fail(errors, blockLabel, `field "${field}" must be an array`)
    return []
  }
  const normalized = []
  for (const item of value) {
    if (typeof item !== 'string') {
      fail(errors, blockLabel, `field "${field}" must contain only strings`)
      continue
    }
    normalized.push(item)
  }
  collectDuplicateErrors(normalized, field, blockLabel, errors)
  return normalized
}

function extractDestinationPath(item, blockLabel, errors) {
  const emdashMatch = item.match(/^(.+?)\s—\s*(.*?)$/)
  if (emdashMatch) {
    const path = emdashMatch[1].trim()
    const description = emdashMatch[2].trim()
    if (!path) {
      fail(errors, blockLabel, `empty destination path in "${item}"`)
      return null
    }
    if (!description) {
      fail(errors, blockLabel, `empty destination description in "${item}"`)
      return null
    }
    return path
  }

  const asciiDelimiter = ' - '
  if (item.includes(asciiDelimiter)) {
    fail(errors, blockLabel, `destination must use em dash separator " — ", got ASCII hyphen: "${item}"`)
    return null
  }

  const dashLike = /[\u2010-\u2015]/.test(item)
  if (dashLike) {
    fail(errors, blockLabel, `destination separator must use em dash " — "`)
  } else {
    fail(errors, blockLabel, `destination must follow "path — description": "${item}"`)
  }
  return null
}

function ensureSafePath(path, baseDir, blockLabel, errors, options = {}) {
  const { requireRegularFile = true } = options
  if (!path) {
    fail(errors, blockLabel, 'empty path is not allowed')
    return
  }
  if (path.includes('\\')) {
    fail(errors, blockLabel, `invalid path separator in: ${path}`)
    return
  }
  if (isAbsolute(path)) {
    fail(errors, blockLabel, `absolute path is not allowed: ${path}`)
    return
  }
  if (path.split('/').includes('..')) {
    fail(errors, blockLabel, `path traversal is not allowed: ${path}`)
    return
  }
  if (path.includes('#')) {
    fail(errors, blockLabel, `URL fragment is not allowed in path: ${path}`)
    return
  }
  if (/^[a-zA-Z][a-zA-Z\d+.-]*:\/\//.test(path) || path.includes('://')) {
    fail(errors, blockLabel, `URL is not allowed: ${path}`)
    return
  }
  if (STALE_ALIAS.has(path)) {
    fail(errors, blockLabel, `stale alias is denied: ${path}`)
    return
  }

  const candidate = resolve(baseDir, path)
  const repoRelative = relative(REPO_ROOT, candidate)
  if (repoRelative === '..' || repoRelative.startsWith(`..${sep}`)) {
    fail(errors, blockLabel, `path escapes repository root: ${path}`)
    return
  }
  if (path === '') {
    fail(errors, blockLabel, 'empty path is not allowed')
    return
  }

  if (!existsSync(candidate)) {
    if (requireRegularFile) {
      fail(errors, blockLabel, `target does not exist: ${path}`)
    }
    return
  }

  const parts = path.split('/')
  let walk = REPO_ROOT
  for (const part of parts) {
    walk = resolve(walk, part)
    try {
      const st = lstatSync(walk)
      if (st.isSymbolicLink()) {
        fail(errors, blockLabel, `symlink path component is not allowed: ${path}`)
        return
      }
    } catch {
      return
    }
  }

  const stat = lstatSync(candidate)
  if (stat.isSymbolicLink()) {
    fail(errors, blockLabel, `symlink path component is not allowed: ${path}`)
    return
  }
  if (stat.isDirectory()) {
    fail(errors, blockLabel, `must point to a regular file: ${path}`)
    return
  }
  if (requireRegularFile && !stat.isFile()) {
    fail(errors, blockLabel, `must point to a regular file: ${path}`)
  }
}

function arraysExactlyMatchAsSet(actual, expected, label, blockLabel, errors) {
  const normalizedActual = [...actual].sort()
  const normalizedExpected = [...expected].sort()
  if (normalizedActual.length !== normalizedExpected.length) {
    fail(
      errors,
      blockLabel,
      `${label} count mismatch: expected ${normalizedExpected.length}, got ${normalizedActual.length}`,
    )
    return
  }
  for (let i = 0; i < normalizedExpected.length; i += 1) {
    if (normalizedActual[i] !== normalizedExpected[i]) {
      fail(
        errors,
        blockLabel,
        `${label} mismatch: expected ${normalizedExpected.join(', ')}, got ${normalizedActual.join(', ')}`,
      )
      return
    }
  }
}

function validateBlocks(blocks, baseDir, errors) {
  for (const { index, parsed } of blocks) {
    const blockLabel = `block#${index}`
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      fail(errors, blockLabel, 'parsed block must be a YAML mapping')
      continue
    }

    const prereqs = ensureOptionalStringArray(parsed, blockLabel, 'spec_prerequisites', errors)
    const requireStrictExistence = parsed.milestone_id === 'M2'
    for (const p of prereqs ?? []) {
      ensureSafePath(p, baseDir, `${blockLabel}: spec_prerequisites`, errors, {
        requireRegularFile: requireStrictExistence,
      })
    }

    const destinations = ensureOptionalStringArray(parsed, blockLabel, 'spec_destination', errors)
    const parsedDestinations = []
    for (const item of destinations ?? []) {
      const path = extractDestinationPath(item, `${blockLabel}: spec_destination`, errors)
      if (path) {
        parsedDestinations.push(path)
        ensureSafePath(path, baseDir, `${blockLabel}: spec_destination`, errors, {
          requireRegularFile: requireStrictExistence,
        })
      }
    }
    collectDuplicateErrors(parsedDestinations, 'spec_destination', `${blockLabel}: spec_destination`, errors)

    if (requireStrictExistence) {
      if (!prereqs) {
        fail(errors, blockLabel, 'M2 requires spec_prerequisites')
      } else {
        arraysExactlyMatchAsSet(prereqs, EXPECTED_M2_PREREQUISITES, 'spec_prerequisites', blockLabel, errors)
      }

      if (!destinations) {
        fail(errors, blockLabel, 'M2 requires spec_destination')
      }
      arraysExactlyMatchAsSet(parsedDestinations, EXPECTED_M2_DESTINATIONS, 'spec_destination', blockLabel, errors)
    }
  }
}

function main() {
  const args = normalizeArgs(process.argv.slice(2))
  if (args.help) {
    process.stdout.write(`${CLI_USAGE}\n`)
    return 0
  }
  if (args.unknownArgs.length > 0) {
    for (const line of args.unknownArgs) {
      process.stderr.write(`[ERR] ${line}\n`)
    }
    process.stderr.write(`${CLI_USAGE}\n`)
    return 2
  }

  const targetPath = resolve(process.cwd(), args.file)
  let sourceText
  try {
    sourceText = readFileSync(targetPath, 'utf-8')
  } catch (error) {
    process.stderr.write(`[ERR] failed to read ${targetPath}: ${(error && error.message) || 'unknown error'}\n`)
    return 1
  }

  const errors = []
  const withoutFrontmatter = removeLeadingFrontmatter(sourceText)
  const blocks = parseYamlBlocks(withoutFrontmatter, targetPath, errors)
  validateBlocks(blocks, REPO_ROOT, errors)

  if (errors.length === 0) {
    process.stdout.write(
      '[OK] roadmap fenced YAML reference checks passed\n'
      + 'checked: YAML parse, duplicate keys, field types, path grammar, M2 expected regular-file references\n'
      + 'not checked: product-spec lifecycle status, normative authority, semantic consistency\n',
    )
    return 0
  }

  for (const err of errors) {
    process.stderr.write(`${err}\n`)
  }
  process.stderr.write(`[ERR] roadmap reference validation failed (${errors.length} issue(s)): ${targetPath}\n`)
  return 1
}

process.exit(main())
