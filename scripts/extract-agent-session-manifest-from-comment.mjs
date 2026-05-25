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
// Extraction
// ============================================================================

function extractManifestFromMarkdown(markdown) {
  const startMarker = '<!-- agent_session_manifest:v1 start -->'
  const endMarker = '<!-- agent_session_manifest:v1 end -->'

  const startIdx = markdown.indexOf(startMarker)
  const endIdx = markdown.indexOf(endMarker)

  if (startIdx === -1 || endIdx === -1 || startIdx >= endIdx) {
    console.error('Error: Manifest markers not found')
    console.error(
      `Expected: <!-- agent_session_manifest:v1 start --> ... <!-- agent_session_manifest:v1 end -->`,
    )
    process.exit(1)
  }

  const contentBetweenMarkers = markdown.substring(startIdx + startMarker.length, endIdx)

  // Extract content between backticks (support both 3 and 4 backticks)
  const codeBlockMatch = contentBetweenMarkers.match(/`{3,4}(?:json)?\s*\n([\s\S]*?)\n`{3,4}/)
  if (!codeBlockMatch) {
    console.error('Error: Code block with JSON not found between markers')
    console.error('Expected: ```json\n{...}\n```')
    process.exit(1)
  }

  const jsonStr = codeBlockMatch[1]

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
