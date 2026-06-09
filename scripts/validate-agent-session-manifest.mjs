#!/usr/bin/env node

/**
 * validate-agent-session-manifest.mjs
 *
 * Validates agent_session_manifest/v1 JSON against
 * docs/schemas/agent-session-manifest.schema.json using Ajv 2020-12.
 *
 * Performs:
 * - JSON Schema validation (Draft 2020-12)
 * - Semantic validation (token_usage null semantics, visibility constraints, producer provenance semantics, producer metadata safety)
 *
 * Enforces producer provenance semantics when producer is present:
 * - actor.type / evidence.source_kind subset for deterministic producers
 * - producer.kind ↔ evidence.source_kind mapping
 * - producer.command / producer.source_ref secret and unsafe local path boundary
 *
 * Usage:
 *   node scripts/validate-agent-session-manifest.mjs manifest.json
 *   cat manifest.json | node scripts/validate-agent-session-manifest.mjs
 *
 * Exit codes:
 *   0: JSON is valid per schema + semantic rules
 *   1: JSON is invalid
 */

import { readFileSync, readdirSync, statSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'
import { validateManifest } from './lib/agent-session-manifest-validation.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ============================================================================
// JSON Loading
// ============================================================================

function collectJsonFiles(targetPath) {
  const stat = statSync(targetPath)
  if (stat.isDirectory()) {
    return readdirSync(targetPath)
      .filter((entry) => entry.endsWith('.json'))
      .map((entry) => resolve(targetPath, entry))
      .sort()
  }
  return [targetPath]
}

function loadJsonFromArgsOrStdin() {
  const args = process.argv.slice(2)
  let jsonContent

  if (args.length > 0 && !args[0].startsWith('--')) {
    const filePath = args[0]
    try {
      const files = collectJsonFiles(filePath)
      return files.map((candidate) => ({
        filePath: candidate,
        json: JSON.parse(readFileSync(candidate, 'utf-8')),
      }))
    } catch (err) {
      console.error(`Error reading file ${filePath}:`, err.message)
      process.exit(1)
    }
  } else if (process.stdin.isTTY) {
    // No file argument and no stdin
    console.error('Usage: validate-agent-session-manifest.mjs <file>')
    console.error('   or: cat manifest.json | validate-agent-session-manifest.mjs')
    process.exit(1)
  } else {
    // Read from stdin
    jsonContent = readFileSync(0, 'utf-8')
  }

  try {
    return [{ filePath: '<stdin>', json: JSON.parse(jsonContent) }]
  } catch (err) {
    console.error('Error parsing JSON:', err.message)
    process.exit(1)
  }
}

// ============================================================================
// Main
// ============================================================================

async function main() {
  const jsonFiles = loadJsonFromArgsOrStdin()

  for (const { filePath, json } of jsonFiles) {
    const result = validateManifest(json)

    if (!result.valid) {
      console.error(`Validation failed for ${filePath}:`)
      for (const error of result.errors) {
        console.error(`  - ${error.path}: ${error.message}`)
      }
      process.exit(1)
    }
  }
  process.exit(0)
}

main().catch((err) => {
  console.error('Unexpected error:', err.message)
  process.exit(1)
})
