#!/usr/bin/env node

/**
 * check-agent-session-manifests.mjs
 *
 * pnpm manifest:check entrypoint.
 *
 * Validates agent_session_manifest/v1 manifests using:
 *   - scripts/lib/agent-session-manifest-validation.mjs (schema + semantic validator)
 *   - scripts/extract-agent-session-manifest-from-comment.mjs logic (markdown extraction)
 *
 * For JSON files: validates directly via validateManifest()
 * For Markdown files: extracts via extractManifestFromMarkdown() then validates
 *
 * Default targets (when no explicit args given):
 *   - docs/schemas/examples/**\/*.json
 *   - tests/fixtures/ ** /agent-session-manifest*.json and *.md
 *
 * Explicit targets: any glob patterns or file paths passed as CLI args
 * If explicit targets resolve to 0 files: exit 1 (empty-target green is forbidden)
 *
 * Exit codes:
 *   0: All manifests pass validation
 *   1: One or more manifests failed, or 0 targets found for explicit arg
 */

import { readFileSync, existsSync } from 'fs'
import { glob as fsGlob, stat } from 'fs/promises'
import { resolve, dirname, extname } from 'path'
import { fileURLToPath } from 'url'
import { execFileSync } from 'child_process'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(__dirname, '..')

// ============================================================================
// Import shared modules
// ============================================================================

import { validateManifest } from './lib/agent-session-manifest-validation.mjs'

// ============================================================================
// Markdown extraction via scripts/extract-agent-session-manifest-from-comment.mjs
// (subprocess integration — no inline reimplementation)
// ============================================================================

const EXTRACTOR_PATH = resolve(__dirname, 'extract-agent-session-manifest-from-comment.mjs')

function extractManifestFromMarkdown(markdown, filePath) {
  try {
    const stdout = execFileSync(
      process.execPath,
      [EXTRACTOR_PATH],
      {
        input: markdown,
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      }
    )
    try {
      const jsonData = JSON.parse(stdout)
      return { ok: true, manifest: jsonData }
    } catch (err) {
      return {
        ok: false,
        error: {
          field: 'json.parse',
          expected: 'valid JSON from extractor stdout',
          actual: err.message,
        },
      }
    }
  } catch (err) {
    const exitCode = (err && typeof err === 'object' && 'status' in err) ? err.status : 1
    const stderr = (err && typeof err === 'object' && 'stderr' in err) ? String(err.stderr || '') : ''
    // Map extractor exit code / stderr to structured error
    const stderrStr = stderr.trim()
    if (stderrStr.includes('Start marker appears') || stderrStr.includes('start')) {
      return {
        ok: false,
        error: {
          field: 'markers.start',
          expected: '1 start marker',
          actual: stderrStr || `extractor exit ${exitCode}`,
        },
      }
    }
    if (stderrStr.includes('End marker appears') || stderrStr.includes('end marker')) {
      return {
        ok: false,
        error: {
          field: 'markers.end',
          expected: '1 end marker',
          actual: stderrStr || `extractor exit ${exitCode}`,
        },
      }
    }
    if (stderrStr.includes('wrong order') || stderrStr.includes('not found')) {
      return {
        ok: false,
        error: {
          field: 'markers.order',
          expected: 'start marker before end marker',
          actual: stderrStr || `extractor exit ${exitCode}`,
        },
      }
    }
    if (stderrStr.includes('Opening fence') || stderrStr.includes('fence')) {
      return {
        ok: false,
        error: {
          field: 'fence',
          expected: 'valid backtick fence enclosing JSON',
          actual: stderrStr || `extractor exit ${exitCode}`,
        },
      }
    }
    if (stderrStr.includes('parsing JSON') || stderrStr.includes('JSON')) {
      return {
        ok: false,
        error: {
          field: 'json.parse',
          expected: 'valid JSON in code block',
          actual: stderrStr || `extractor exit ${exitCode}`,
        },
      }
    }
    return {
      ok: false,
      error: {
        field: 'extraction',
        expected: 'extractor exit 0',
        actual: stderrStr || `extractor exit ${exitCode}`,
      },
    }
  }
}

// ============================================================================
// Additional deployment-level semantic checks
// (These supplement validateManifest() with deploy-specific rules)
// ============================================================================

function checkDeploymentSemantics(manifest) {
  const errors = []

  // AC7a: redaction.raw_transcript_included: true is not allowed (public safety)
  if (manifest.redaction?.raw_transcript_included === true) {
    errors.push({
      field: 'redaction.raw_transcript_included',
      expected: 'false',
      actual: 'true (raw transcript must not be included)',
    })
  }

  // AC7b: visibility: public_github_comment AND local_paths_included: true
  const hasPublicEvidence = Array.isArray(manifest.evidence) &&
    manifest.evidence.some(e => e.visibility === 'public_github_comment')

  if (hasPublicEvidence && manifest.redaction?.local_paths_included === true) {
    errors.push({
      field: 'redaction.local_paths_included',
      expected: 'false when visibility: public_github_comment evidence is present',
      actual: 'true',
    })
  }

  return errors
}

// ============================================================================
// Glob expansion (Node.js 22 fs/promises.glob)
// ============================================================================

async function expandGlobs(patterns) {
  const files = []
  for (const pattern of patterns) {
    // Resolve relative to repo root
    const absPattern = resolve(REPO_ROOT, pattern)

    // Try as literal file first
    if (existsSync(absPattern)) {
      const s = await stat(absPattern)
      if (s.isFile()) {
        files.push(absPattern)
        continue
      }
    }

    // Use fs/promises glob (Node 22+)
    try {
      const matches = await Array.fromAsync(fsGlob(absPattern))
      files.push(...matches.map(f => resolve(f)))
    } catch (_err) {
      // fsGlob not available — skip (handled gracefully)
    }
  }

  // Deduplicate
  return [...new Set(files)]
}

// ============================================================================
// Validate a single file
// ============================================================================

function validateFile(filePath) {
  const ext = extname(filePath).toLowerCase()
  const shortPath = filePath.replace(REPO_ROOT + '/', '')

  if (ext === '.json') {
    let json
    try {
      const content = readFileSync(filePath, 'utf-8')
      json = JSON.parse(content)
    } catch (err) {
      return {
        file: shortPath,
        ok: false,
        errors: [{ field: 'file', expected: 'valid JSON file', actual: err.message }],
      }
    }

    const result = validateManifest(json)
    const deployErrors = checkDeploymentSemantics(json)

    const allErrors = [
      ...result.errors.map(e => ({ field: e.path, expected: 'valid', actual: e.message })),
      ...deployErrors,
    ]

    return {
      file: shortPath,
      ok: allErrors.length === 0,
      errors: allErrors,
    }

  } else if (ext === '.md') {
    let markdown
    try {
      markdown = readFileSync(filePath, 'utf-8')
    } catch (err) {
      return {
        file: shortPath,
        ok: false,
        errors: [{ field: 'file', expected: 'readable markdown file', actual: err.message }],
      }
    }

    const extractResult = extractManifestFromMarkdown(markdown, filePath)
    if (!extractResult.ok) {
      return {
        file: shortPath,
        ok: false,
        errors: [extractResult.error],
      }
    }

    const json = extractResult.manifest
    const result = validateManifest(json)
    const deployErrors = checkDeploymentSemantics(json)

    const allErrors = [
      ...result.errors.map(e => ({ field: e.path, expected: 'valid', actual: e.message })),
      ...deployErrors,
    ]

    return {
      file: shortPath,
      ok: allErrors.length === 0,
      errors: allErrors,
    }

  } else {
    // Skip non-JSON, non-MD files
    return null
  }
}

// ============================================================================
// Main
// ============================================================================

async function main() {
  const args = process.argv.slice(2).filter(a => !a.startsWith('--'))
  const isExplicit = args.length > 0

  const defaultPatterns = [
    'docs/schemas/examples/**/*.json',
    'tests/fixtures/agent-session-manifest/valid-*.json',
    'tests/fixtures/agent-session-manifest/valid-*.md',
  ]

  const patterns = isExplicit ? args : defaultPatterns

  const files = await expandGlobs(patterns)

  if (isExplicit && files.length === 0) {
    process.stderr.write(
      `manifest:check: error: no files found matching: ${args.join(', ')}\n` +
      `  field: target, expected: >= 1 file, actual: 0 files\n`
    )
    process.exit(1)
  }

  if (files.length === 0) {
    // Default patterns with 0 files is not an error (no manifests in repo yet)
    process.stdout.write('manifest:check: no manifest files found (default targets) — skipping\n')
    process.exit(0)
  }

  let passed = 0
  let failed = 0

  for (const filePath of files.sort()) {
    const result = validateFile(filePath)
    if (result === null) continue // skipped (non-manifest extension)

    if (result.ok) {
      passed++
      process.stdout.write(`  PASS  ${result.file}\n`)
    } else {
      failed++
      process.stderr.write(`  FAIL  ${result.file}\n`)
      for (const err of result.errors) {
        process.stderr.write(`         field: ${err.field}\n`)
        process.stderr.write(`         expected: ${err.expected}\n`)
        process.stderr.write(`         actual: ${err.actual}\n`)
      }
    }
  }

  const total = passed + failed
  process.stdout.write(`\nmanifest:check: ${passed}/${total} passed\n`)

  if (failed > 0) {
    process.exit(1)
  }
  process.exit(0)
}

main().catch(err => {
  process.stderr.write(`manifest:check: unexpected error: ${err.message}\n`)
  process.exit(1)
})
