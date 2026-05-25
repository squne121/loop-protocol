#!/usr/bin/env node

/**
 * extract-agent-session-manifest-from-comment.mjs
 *
 * Extracts agent_session_manifest/v1 JSON from GitHub comment markdown.
 *
 * Searches for HTML comment markers:
 *   <!-- agent_session_manifest:v1 start -->
 *   ```json
 *   {...}
 *   ```
 *   <!-- agent_session_manifest:v1 end -->
 *
 * Outputs extracted JSON to stdout.
 *
 * Usage:
 *   node scripts/extract-agent-session-manifest-from-comment.mjs comment.md
 *   cat comment.md | node scripts/extract-agent-session-manifest-from-comment.mjs
 *
 * Exit codes:
 *   0: Extraction successful
 *   1: Markers or code block not found
 */

import { readFileSync } from 'fs'

// ============================================================================
// Input Loading
// ============================================================================

function loadMarkdownFromArgsOrStdin() {
  const args = process.argv.slice(2)

  if (args.length > 0 && !args[0].startsWith('--')) {
    // Load from file
    try {
      return readFileSync(args[0], 'utf-8')
    } catch (err) {
      console.error(`Error reading file:`, err.message)
      process.exit(1)
    }
  } else if (process.stdin.isTTY) {
    console.error('Usage: extract-agent-session-manifest-from-comment.mjs <file>')
    console.error('   or: cat comment.md | extract-agent-session-manifest-from-comment.mjs')
    process.exit(1)
  } else {
    // Read from stdin
    return readFileSync(0, 'utf-8')
  }
}

// ============================================================================
// Extraction (B6: Line-based parser with fence length matching)
// ============================================================================

function extractManifestFromMarkdown(markdown) {
  const startMarker = '<!-- agent_session_manifest:v1 start -->'
  const endMarker = '<!-- agent_session_manifest:v1 end -->'

  const lines = markdown.split('\n')

  // Find marker lines
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

  // B6: Validate marker uniqueness
  if (startMarkerCount !== 1) {
    console.error(`Error: Start marker appears ${startMarkerCount} times (expected 1)`)
    process.exit(1)
  }
  if (endMarkerCount !== 1) {
    console.error(`Error: End marker appears ${endMarkerCount} times (expected 1)`)
    process.exit(1)
  }

  if (startMarkerLine === -1 || endMarkerLine === -1 || startMarkerLine >= endMarkerLine) {
    console.error('Error: Manifest markers not found or in wrong order')
    console.error(`Expected: <!-- agent_session_manifest:v1 start --> ... <!-- agent_session_manifest:v1 end -->`)
    process.exit(1)
  }

  // Find opening fence (first line after start marker with backticks)
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
    console.error('Error: Opening fence not found')
    process.exit(1)
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
    console.error(`Error: Closing fence not found (expected ${openingFenceLength} backticks)`)
    process.exit(1)
  }

  // Extract JSON between fences
  const jsonLines = lines.slice(openingFenceLine + 1, closingFenceLine)
  const jsonStr = jsonLines.join('\n')

  try {
    const jsonData = JSON.parse(jsonStr)
    return jsonData
  } catch (err) {
    console.error('Error parsing JSON from code block:', err.message)
    process.exit(1)
  }
}

// ============================================================================
// Main
// ============================================================================

try {
  const markdown = loadMarkdownFromArgsOrStdin()
  const manifest = extractManifestFromMarkdown(markdown)
  console.log(JSON.stringify(manifest, null, 2))
  process.exit(0)
} catch (err) {
  console.error('Unexpected error:', err.message)
  process.exit(1)
}
