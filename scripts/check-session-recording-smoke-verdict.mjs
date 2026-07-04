#!/usr/bin/env node

/**
 * check-session-recording-smoke-verdict.mjs
 *
 * pnpm smoke-verdict:check entrypoint.
 *
 * Validates session_recording_smoke_verdict/v1 JSON against
 * docs/schemas/session-recording-smoke-verdict.schema.json using Ajv 2020-12.
 *
 * Usage:
 *   node scripts/check-session-recording-smoke-verdict.mjs [file-or-dir ...]
 *   cat verdict.json | node scripts/check-session-recording-smoke-verdict.mjs -
 *
 * Default targets (when no explicit args given):
 *   - tests/fixtures/session-recording-smoke-verdict/valid-*.json
 *
 * A single "-" argument reads a single verdict JSON from stdin instead of
 * using file/glob targets.
 *
 * Explicit file targets: any file paths passed as CLI args.
 * If explicit targets resolve to 0 files: exit 1 (empty-target green is forbidden).
 *
 * Exit codes:
 *   0: all verdicts pass validation (or 0 files found via default targets)
 *   1: one or more verdicts failed validation, or 0 files found for explicit target
 *   2: usage error
 */

import { readFileSync, existsSync, statSync } from 'fs'
import { glob as fsGlob } from 'fs/promises'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(__dirname, '..')
const SCHEMA_PATH = resolve(REPO_ROOT, 'docs/schemas/session-recording-smoke-verdict.schema.json')

let Ajv2020, ajvFormats

async function loadAjv() {
  if (Ajv2020) return
  try {
    const ajv2020Module = await import('ajv/dist/2020.js')
    Ajv2020 = ajv2020Module.default
    const formatsModule = await import('ajv-formats')
    ajvFormats = formatsModule.default
  } catch (err) {
    console.error('Error: ajv and ajv-formats must be installed as devDependencies')
    process.exit(2)
  }
}

function loadSchema() {
  const content = readFileSync(SCHEMA_PATH, 'utf-8')
  return JSON.parse(content)
}

export async function validateVerdict(json) {
  await loadAjv()
  const schema = loadSchema()
  const ajv = new Ajv2020({ strict: true, allErrors: true })
  ajvFormats(ajv)

  let validate
  try {
    validate = ajv.compile(schema)
  } catch (err) {
    return {
      valid: false,
      errors: [{ path: 'schema', message: `Schema compilation error: ${err.message}` }],
    }
  }

  const ok = validate(json)
  const errors = ok
    ? []
    : (validate.errors || []).map((err) => ({ path: err.instancePath || 'root', message: err.message }))

  return { valid: errors.length === 0, errors }
}

// ============================================================================
// File / glob expansion
// ============================================================================

async function expandGlobs(patterns) {
  const files = []
  for (const pattern of patterns) {
    const absPattern = resolve(REPO_ROOT, pattern)
    if (existsSync(absPattern) && statSync(absPattern).isFile()) {
      files.push(absPattern)
      continue
    }
    try {
      const matches = await Array.fromAsync(fsGlob(absPattern))
      files.push(...matches.map((f) => resolve(f)))
    } catch (_err) {
      // fsGlob unavailable or pattern matched nothing — skip.
    }
  }
  return [...new Set(files)]
}

// ============================================================================
// Main
// ============================================================================

async function loadJsonFromStdin() {
  process.stdin.setEncoding('utf-8')
  let content = ''
  for await (const chunk of process.stdin) {
    content += chunk
  }
  return JSON.parse(content)
}

async function main() {
  const rawArgs = process.argv.slice(2)

  if (rawArgs.includes('--help')) {
    process.stdout.write(
      'Usage: check-session-recording-smoke-verdict.mjs [file...]\n' +
        'Validates session_recording_smoke_verdict/v1 JSON files.\n' +
        'If no arguments given, uses default target patterns.\n' +
        'A single "-" argument reads one verdict JSON from stdin.\n',
    )
    process.exit(0)
  }

  if (rawArgs.length === 1 && rawArgs[0] === '-') {
    let json
    try {
      json = await loadJsonFromStdin()
    } catch (err) {
      console.error(`Error parsing JSON from stdin: ${err.message}`)
      process.exit(2)
    }
    const result = await validateVerdict(json)
    if (!result.valid) {
      console.error('Validation failed for <stdin>:')
      for (const err of result.errors) {
        console.error(`  - ${err.path}: ${err.message}`)
      }
      process.exit(1)
    }
    console.log('  PASS  <stdin>')
    process.exit(0)
  }

  const isExplicit = rawArgs.length > 0
  const defaultPatterns = ['tests/fixtures/session-recording-smoke-verdict/valid-*.json']
  const patterns = isExplicit ? rawArgs : defaultPatterns
  const files = await expandGlobs(patterns)

  if (isExplicit && files.length === 0) {
    process.stderr.write(
      `smoke-verdict:check: error: no files found matching: ${rawArgs.join(', ')}\n` +
        '  field: target, expected: >= 1 file, actual: 0 files\n',
    )
    process.exit(1)
  }

  if (files.length === 0) {
    process.stdout.write('smoke-verdict:check: no verdict files found (default targets) — skipping\n')
    process.exit(0)
  }

  let passed = 0
  let failed = 0

  for (const filePath of files.sort()) {
    const shortPath = filePath.replace(REPO_ROOT + '/', '')
    let json
    try {
      json = JSON.parse(readFileSync(filePath, 'utf-8'))
    } catch (err) {
      failed++
      process.stderr.write(`  FAIL  ${shortPath}\n         field: file\n         message: ${err.message}\n`)
      continue
    }

    const result = await validateVerdict(json)
    if (result.valid) {
      passed++
      process.stdout.write(`  PASS  ${shortPath}\n`)
    } else {
      failed++
      process.stderr.write(`  FAIL  ${shortPath}\n`)
      for (const err of result.errors) {
        process.stderr.write(`         path: ${err.path}\n`)
        process.stderr.write(`         message: ${err.message}\n`)
      }
    }
  }

  const total = passed + failed
  process.stdout.write(`\nsmoke-verdict:check: ${passed}/${total} passed\n`)

  process.exit(failed > 0 ? 1 : 0)
}

// Only run main() when executed directly (not when imported for tests).
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(`smoke-verdict:check: unexpected error: ${err.message}`)
    process.exit(2)
  })
}
