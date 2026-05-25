#!/usr/bin/env node

/**
 * validate-agent-session-manifest.mjs
 *
 * Validates agent_session_manifest/v1 JSON against
 * docs/schemas/agent-session-manifest.schema.json using Ajv 2020-12.
 *
 * Performs:
 * - JSON Schema validation (Draft 2020-12)
 * - Semantic validation (token_usage null semantics, visibility constraints, etc.)
 *
 * Does NOT enforce producer-specific contract (actor.type/evidence.source_kind subset).
 * Producer contract is enforced by scripts/generate-session-manifest.mjs only.
 *
 * Usage:
 *   node scripts/validate-agent-session-manifest.mjs manifest.json
 *   cat manifest.json | node scripts/validate-agent-session-manifest.mjs
 *
 * Exit codes:
 *   0: JSON is valid per schema + semantic rules
 *   1: JSON is invalid
 */

import { readFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'
import { validateManifest } from './lib/agent-session-manifest-validation.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ============================================================================
// JSON Loading
// ============================================================================

function loadJsonFromArgsOrStdin() {
  const args = process.argv.slice(2)
  let jsonContent

  if (args.length > 0 && !args[0].startsWith('--')) {
    // Load from file
    const filePath = args[0]
    try {
      jsonContent = readFileSync(filePath, 'utf-8')
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
    return JSON.parse(jsonContent)
  } catch (err) {
    console.error('Error parsing JSON:', err.message)
    process.exit(1)
  }
}

// ============================================================================
// Main
// ============================================================================

async function main() {
  const jsonData = loadJsonFromArgsOrStdin()

  // Validate using common module
  const result = validateManifest(jsonData)

  if (!result.valid) {
    console.error('Validation failed with errors:')
    for (const error of result.errors) {
      console.error(`  - ${error.path}: ${error.message}`)
    }
    process.exit(1)
  } else {
    // Validation passed
    process.exit(0)
  }
}

main().catch((err) => {
  console.error('Unexpected error:', err.message)
  process.exit(1)
})
