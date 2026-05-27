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

const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(__dirname, '..')

// ============================================================================
// Import shared modules
// ============================================================================

import { validateManifest } from './lib/agent-session-manifest-validation.mjs'

// ============================================================================
// Markdown extraction (re-implemented inline to avoid subprocess overhead,
// mirrors logic from extract-agent-session-manifest-from-comment.mjs)
// ============================================================================

function extractManifestFromMarkdown(markdown, filePath) {
  const startMarker = '<!-- agent_session_manifest:v1 start -->'
  const endMarker = '<!-- agent_session_manifest:v1 end -->'

  const lines = markdown.split('\n')

  let startMarkerLine = -1
  let endMarkerLine = -1
  let startMarkerCount = 0
  let endMarkerCount = 0

  for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes(startMarker)) {
      startMarkerLine = i
      startMarkerCount++
    }
    if (lines[i].includes(endMarker)) {
      endMarkerLine = i
      endMarkerCount++
    }
  }

  if (startMarkerCount !== 1) {
    return {
      ok: false,
      error: {
        field: 'markers.start',
        expected: '1 start marker',
        actual: `${startMarkerCount} start markers`,
      },
    }
  }
  if (endMarkerCount !== 1) {
    return {
      ok: false,
      error: {
        field: 'markers.end',
        expected: '1 end marker',
        actual: `${endMarkerCount} end markers`,
      },
    }
  }

  if (startMarkerLine === -1 || endMarkerLine === -1 || startMarkerLine >= endMarkerLine) {
    return {
      ok: false,
      error: {
        field: 'markers.order',
        expected: 'start marker before end marker',
        actual: `startLine=${startMarkerLine} endLine=${endMarkerLine}`,
      },
    }
  }

  // Find opening fence
  let openingFenceLine = -1
  let openingFenceLength = 0
  for (let i = startMarkerLine + 1; i < endMarkerLine; i++) {
    const match = lines[i].match(/^(`+)/)
    if (match) {
      openingFenceLine = i
      openingFenceLength = match[1].length
      break
    }
  }

  if (openingFenceLine === -1) {
    return {
      ok: false,
      error: {
        field: 'fence.opening',
        expected: 'backtick fence after start marker',
        actual: 'not found',
      },
    }
  }

  // Find closing fence (matching length)
  let closingFenceLine = -1
  for (let i = openingFenceLine + 1; i < endMarkerLine; i++) {
    const match = lines[i].match(/^(`+)$/)
    if (match && match[1].length === openingFenceLength) {
      closingFenceLine = i
      break
    }
  }

  if (closingFenceLine === -1) {
    return {
      ok: false,
      error: {
        field: 'fence.closing',
        expected: `closing fence with ${openingFenceLength} backticks`,
        actual: 'not found',
      },
    }
  }

  const jsonLines = lines.slice(openingFenceLine + 1, closingFenceLine)
  const jsonStr = jsonLines.join('\n')

  try {
    const jsonData = JSON.parse(jsonStr)
    return { ok: true, manifest: jsonData }
  } catch (err) {
    return {
      ok: false,
      error: {
        field: 'json.parse',
        expected: 'valid JSON in code block',
        actual: err.message,
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
