#!/usr/bin/env node

/**
 * validate-agent-session-manifest.mjs
 *
 * Validates agent_session_manifest/v1 JSON against
 * docs/schemas/agent-session-manifest.schema.json using Ajv 2020-12.
 *
 * Usage:
 *   node scripts/validate-agent-session-manifest.mjs manifest.json
 *   cat manifest.json | node scripts/validate-agent-session-manifest.mjs
 *
 * Exit codes:
 *   0: JSON is valid
 *   1: JSON is invalid
 */

import { readFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))

// Dynamic import of Ajv (require it as devDependency)
let Ajv, ajvFormats

try {
  const ajvModule = await import('ajv')
  Ajv = ajvModule.default
  const formatsModule = await import('ajv-formats')
  ajvFormats = formatsModule.default
} catch (err) {
  console.error('Error: ajv and ajv-formats must be installed as devDependencies')
  console.error('Run: pnpm install')
  process.exit(1)
}

// ============================================================================
// Schema Loading
// ============================================================================

function loadSchema() {
  const schemaPath = resolve(__dirname, '../docs/schemas/agent-session-manifest.schema.json')
  try {
    const schemaContent = readFileSync(schemaPath, 'utf-8')
    const schema = JSON.parse(schemaContent)
    // Remove $schema reference to avoid meta-schema fetch
    delete schema['$schema']
    return schema
  } catch (err) {
    console.error(`Error loading schema from ${schemaPath}:`, err.message)
    process.exit(1)
  }
}

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
// Validation
// ============================================================================

async function validateManifest() {
  const schema = loadSchema()
  const jsonData = loadJsonFromArgsOrStdin()

  // Create Ajv instance with 2020-12 spec
  // spec: draft2020-12 is the default for Ajv 8.x
  const ajv = new Ajv()
  ajvFormats(ajv)

  // Compile schema
  let validate
  try {
    validate = ajv.compile(schema)
  } catch (err) {
    console.error('Error compiling schema:', err.message)
    process.exit(1)
  }

  // Validate
  const valid = validate(jsonData)

  if (!valid) {
    console.error('Validation failed with errors:')
    for (const error of validate.errors || []) {
      console.error(`  - ${error.instancePath || 'root'}: ${error.message}`)
      if (error.params) {
        console.error(`    params: ${JSON.stringify(error.params)}`)
      }
    }
    process.exit(1)
  } else {
    // Validation passed
    process.exit(0)
  }
}

// ============================================================================
// Main
// ============================================================================

validateManifest().catch((err) => {
  console.error('Unexpected error:', err.message)
  process.exit(1)
})
